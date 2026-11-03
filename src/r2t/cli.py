"""Typer commands and Rich presentation only."""

from __future__ import annotations

import functools
import json
import os
import shlex
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from . import __version__
from .analyze import analyze
from .docker import (
    LaunchResult,
    check_docker,
    compose_argv,
    compose_env,
    launch,
    load_bundle,
    modified_files,
    port_is_free,
    validate_bundle,
)
from .errors import (
    EXIT_BUILD,
    EXIT_INTERNAL,
    EXIT_INTERRUPTED,
    EXIT_NOT_SUPPORTED,
    EXIT_RUNTIME,
    EXIT_USAGE,
    DockerError,
    InputError,
    R2TError,
)
from .models import NEEDS_INPUT, SUPPORTED, Inspection, Options
from .readiness import Probe, http_probe
from .render import write_bundle
from .runner import Runner, SubprocessRunner
from .source import acquire
from .text import clean

out = Console(soft_wrap=True, highlight=False)
err = Console(stderr=True, soft_wrap=True, highlight=False)

app = typer.Typer(
    name="r2t",
    help="Found a cool repo? Try it locally.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


@dataclass
class Services:
    """Everything with side effects, injectable so tests never need monkeypatching."""

    runner: Runner = field(default_factory=SubprocessRunner)
    probe: Probe = http_probe
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    is_tty: Callable[[], bool] = lambda: sys.stdin.isatty()
    port_free: Callable[[int], bool] = port_is_free


class PackageManager(StrEnum):
    npm = "npm"
    pnpm = "pnpm"


def services_of(ctx: typer.Context) -> Services:
    if not isinstance(ctx.obj, Services):
        ctx.obj = Services()
    return ctx.obj


def esc(value: object) -> str:
    """Make repo-derived text safe for the terminal and for Rich markup."""
    return escape(clean(value, 600))


def guarded(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except typer.Exit:
            raise
        except R2TError as exc:
            first, *rest = exc.message.splitlines() or [""]
            err.print(f"[red]error:[/red] {esc(first)}")
            for line in rest:
                err.print(esc(line))
            if exc.hint:
                err.print(f"[dim]hint:[/dim] {esc(exc.hint)}")
            raise typer.Exit(exc.exit_code) from None
        except KeyboardInterrupt:
            err.print("interrupted")
            raise typer.Exit(EXIT_INTERRUPTED) from None
        except Exception as exc:
            err.print(
                f"[red]unexpected internal error:[/red] {esc(type(exc).__name__)}: {esc(exc)}"
            )
            if os.environ.get("R2T_DEBUG"):
                err.print(esc(traceback.format_exc()))
            else:
                err.print("[dim]set R2T_DEBUG=1 for a traceback[/dim]")
            raise typer.Exit(EXIT_INTERNAL) from None

    return wrapper


def _version(value: bool) -> None:
    if value:
        typer.echo(f"r2t {__version__}")
        raise typer.Exit(0)


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    pass


# ---- shared option declarations -----------------------------------------------------------

SourceArg = typer.Argument(..., help="Local directory or https://github.com/OWNER/REPO")
SubdirOpt = typer.Option(None, "--subdir", help="App directory inside the repository.")
RefOpt = typer.Option(None, "--ref", help="Branch, tag or commit SHA (remote sources only).")
PmOpt = typer.Option(None, "--package-manager", help="Override the package manager (Node).")
EntryOpt = typer.Option(None, "--entry", help="FastAPI app as MODULE:ATTR, e.g. app.main:app.")
RuntimeOpt = typer.Option(
    None, "--runtime-version", help="Node major (22) or Python minor (3.12) to use."
)
JsonOpt = typer.Option(False, "--json", help="Machine-readable output on stdout.")


def _options(subdir, ref, pm, entry, runtime) -> Options:
    return Options(
        subdir=subdir,
        ref=ref,
        package_manager=pm.value if pm else None,
        entry=entry,
        runtime_version=runtime,
    )


def _analyze(svc: Services, source: str, opts: Options, fn):
    with acquire(source, opts.ref, svc.runner) as acq:
        insp, snap = analyze(acq, opts)
        return fn(insp, snap, acq)


# ---- inspect ------------------------------------------------------------------------------

_STATUS_STYLE = {SUPPORTED: "green", NEEDS_INPUT: "yellow", "unsupported": "red"}


def _refs(evidence) -> str:
    refs = ", ".join(esc(e.ref()) for e in evidence)
    return f" [dim]({refs})[/dim]" if refs else ""


def _print_inspection(insp: Inspection, heading: str) -> None:
    style = _STATUS_STYLE.get(insp.status, "white")
    out.print(f"[bold]{heading}[/bold]")
    out.print(f"Status: [bold {style}]{insp.status}[/bold {style}]")
    src = insp.source
    if src.kind == "remote":
        out.print(f"Source: {esc(src.url)} @ {esc(src.resolved_commit)}")
    else:
        extra = ""
        if src.git_head:
            extra = f" (git {src.git_head[:12]}{', dirty' if src.dirty else ''})"
        out.print(f"Source: {esc(src.path)}{extra}")
    if insp.app_root != ".":
        out.print(f"App root: {esc(insp.app_root)}")
    out.print(f"Snapshot digest: sha256:{src.snapshot_digest}")
    out.print("[dim]Evidence paths are relative to the app root.[/dim]")
    out.print()

    for b in insp.blockers:
        flag = f" -> resolve with [bold]{esc(b.flag)}[/bold]" if b.flag else ""
        colour = "yellow" if b.status == NEEDS_INPUT else "red"
        out.print(f"[{colour}]{b.status}:[/{colour}] {esc(b.reason)}{flag}")
    for c in insp.candidates:
        out.print(f"  candidate: [bold]{esc(c['flag'])}[/bold] [dim]{esc(c.get('kind', ''))}[/dim]")
    if insp.blockers:
        out.print()

    def row(label: str, value: str, evidence=()) -> None:
        out.print(f"{label:<17}{esc(value)}{_refs(evidence)}")

    det = insp.detection
    if insp.stack:
        stack = {"node": "Node.js", "python": "Python"}[insp.stack]
        fw = f" / {insp.framework}" if insp.framework else ""
        d = det("framework") or det("stack")
        row("Stack", stack + fw, d.evidence if d else ())
    if insp.base_image:
        d = det("runtime")
        constraints = (
            f"  constraints: {'; '.join(insp.runtime_constraints)}"
            if insp.runtime_constraints
            else ""
        )
        row("Runtime image", insp.base_image + constraints, d.evidence if d else ())
    if insp.stack == "node" and insp.package_manager:
        pm = insp.package_manager + (
            f"@{insp.package_manager_version}" if insp.package_manager_version else ""
        )
        d = det("package_manager")
        row("Package manager", pm, d.evidence if d else ())
    if insp.lock_status:
        d = det("lockfile") or det("lock_status")
        lock = f"{insp.lockfile}, " if insp.lockfile else ""
        row("Lock status", f"{lock}{insp.lock_status}", d.evidence if d else ())
    if insp.install_argv:
        row("Install", shlex.join(insp.install_argv))
    if insp.start_argv:
        d = det("start_command")
        row("Start", shlex.join(insp.start_argv), d.evidence if d else ())
    if insp.container_port:
        d = det("container_port")
        row(
            "Container port",
            f"{insp.container_port} (readiness path {insp.readiness_path})",
            d.evidence if d else (),
        )

    if insp.lifecycle:
        out.print(
            "\n[bold]Scripts that will execute[/bold] "
            "(dependency install and app start run repo code)"
        )
        for s in insp.lifecycle:
            when = escape(f"[{s.when}]")
            ref = esc(s.evidence.ref())
            out.print(f"  {when} {esc(s.name)}: {esc(s.command)} [dim]({ref})[/dim]")
    if insp.env_names:
        out.print(
            "\n[bold]Environment variable names[/bold] (from example files; values are never read)"
        )
        out.print("  " + ", ".join(esc(e.name) for e in insp.env_names))
    if insp.services:
        out.print("\n[bold]External services or config that may need manual setup[/bold]")
        for s in insp.services:
            out.print(f"  {esc(s.why)} [dim]({esc(s.evidence.ref())})[/dim]")
    if insp.container_files:
        out.print("\n[bold]Existing container files[/bold] (evidence only; r2t does not use them)")
        out.print("  " + ", ".join(esc(e.ref()) for e in insp.container_files))
    if insp.assumptions:
        out.print("\n[bold]Assumptions[/bold]")
        for a in insp.assumptions:
            out.print(f"  - {esc(a)}")
    if insp.skipped_symlinks:
        out.print("\n[bold]Symlinks not followed or copied[/bold]")
        for s in insp.skipped_symlinks:
            out.print(f"  {esc(s['path'])}: {esc(s['reason'])}")
    if insp.notes:
        out.print("\n[bold]Notes[/bold]")
        for n in insp.notes:
            out.print(f"  - {esc(n)}")
    if insp.framework == "fastapi":
        out.print("\n[dim]FastAPI serves interactive docs at /docs once the app is running.[/dim]")


@app.command()
@guarded
def inspect(
    ctx: typer.Context,
    source: str = SourceArg,
    subdir: str | None = SubdirOpt,
    ref: str | None = RefOpt,
    package_manager: PackageManager | None = PmOpt,
    entry: str | None = EntryOpt,
    runtime_version: str | None = RuntimeOpt,
    json_out: bool = JsonOpt,
) -> None:
    """Inspect a repository and explain how it would be launched. Never runs its code."""
    svc = services_of(ctx)
    opts = _options(subdir, ref, package_manager, entry, runtime_version)
    insp = _analyze(svc, source, opts, lambda insp, snap, acq: insp)
    if json_out:
        sys.stdout.write(json.dumps(insp.to_dict(), indent=2) + "\n")
        return
    _print_inspection(insp, f"r2t inspect {clean(source)}")


# ---- plan ---------------------------------------------------------------------------------


@app.command()
@guarded
def plan(
    ctx: typer.Context,
    source: str = SourceArg,
    output: Path = typer.Option(..., "--output", "-o", help="New directory for the bundle."),
    subdir: str | None = SubdirOpt,
    ref: str | None = RefOpt,
    package_manager: PackageManager | None = PmOpt,
    entry: str | None = EntryOpt,
    runtime_version: str | None = RuntimeOpt,
    json_out: bool = JsonOpt,
) -> None:
    """Inspect a repository and write a reviewable Docker launch bundle."""
    svc = services_of(ctx)
    opts = _options(subdir, ref, package_manager, entry, runtime_version)

    def build(insp: Inspection, snap, acq):
        return insp, write_bundle(insp, snap, output, acq.root)

    insp, result = _analyze(svc, source, opts, build)
    if json_out:
        sys.stdout.write((result.path / "plan.json").read_text())
    else:
        _print_inspection(insp, f"r2t plan {clean(source)}")
        out.print()
        out.print(f"Bundle written to [bold]{esc(result.path)}[/bold]")
        if result.status == SUPPORTED:
            out.print("Review these before running: Dockerfile, compose.yaml, REPORT.md")
            out.print(
                "If the app needs environment variables: cp .env.template .env and fill it in"
            )
            out.print(f"Then: [bold]r2t run {esc(shlex.quote(str(result.path)))}[/bold]")
        else:
            out.print(
                "No Dockerfile or compose.yaml was generated. See REPORT.md for the blockers."
            )
    if result.status != SUPPORTED:
        raise typer.Exit(EXIT_NOT_SUPPORTED)


# ---- run ----------------------------------------------------------------------------------


def _cmd(argv: list[str]) -> str:
    return shlex.join(argv)


def _print_followups(bundle_path: Path) -> None:
    d = shlex.quote(str(bundle_path))
    err.print(f"  view logs:  [bold]r2t logs {d}[/bold]")
    err.print(f"  stop:       [bold]r2t stop {d}[/bold]")


@app.command()
@guarded
def run(
    ctx: typer.Context,
    directory: str = typer.Argument(..., help="Bundle directory created by `r2t plan`."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    timeout: float = typer.Option(180.0, "--timeout", min=1, help="Readiness timeout in seconds."),
    host_port: int | None = typer.Option(None, "--host-port", min=1, max=65535),
) -> None:
    """Build the bundle, start it, and check that an HTTP endpoint answers."""
    svc = services_of(ctx)
    bundle = load_bundle(directory)
    if not yes and not svc.is_tty():
        raise InputError(
            "confirmation required but stdin is not a terminal",
            "re-run with --yes to confirm that the build and start may execute repository code",
        )
    check_docker(svc.runner)
    port = host_port if host_port is not None else bundle.port
    changed = modified_files(bundle)
    if changed:
        err.print(
            f"[yellow]warning:[/yellow] {', '.join(changed)} differ from what r2t generated "
            "(plan.json hashes); continuing with your edits"
        )
    _, published = validate_bundle(bundle, svc.runner, port)
    if not svc.port_free(published):
        raise InputError(
            f"port {published} on 127.0.0.1 is already in use",
            f"pick another with: r2t run {shlex.quote(str(bundle.path))} --host-port 8080",
        )
    url = f"http://127.0.0.1:{published}{bundle.readiness_path}"
    build_cmd = _cmd(compose_argv(bundle, "build"))
    up_cmd = _cmd(compose_argv(bundle, "up", "-d"))
    out.print("[bold]r2t will run:[/bold]")
    out.print(f"  R2T_HOST_PORT={published} {esc(build_cmd)}")
    out.print(f"  R2T_HOST_PORT={published} {esc(up_cmd)}")
    out.print(f"Then check: {esc(url)}")
    out.print(
        "\n[yellow]The build installs dependencies from the network, and starting the container "
        "runs the repository's code.[/yellow]\nDocker is an isolation boundary, not a guarantee "
        "that arbitrary code is safe."
    )
    if not yes and not typer.confirm("Continue?", default=False):
        err.print("aborted; nothing was built or started")
        raise typer.Exit(EXIT_USAGE)

    phases = {
        "build": "Building image (this installs dependencies)...",
        "up": "Starting container...",
        "wait": f"Waiting up to {timeout:g}s for an HTTP response from {url} ...",
    }
    result: LaunchResult = launch(
        bundle,
        svc.runner,
        host_port=published,
        timeout=timeout,
        probe=svc.probe,
        clock=svc.clock,
        sleep=svc.sleep,
        say=lambda phase: err.print(phases[phase]),
    )
    raise typer.Exit(_report_launch(bundle, result, port=published, timeout=timeout))


def _print_logs(logs: str) -> None:
    if logs:
        err.print("[bold]Last log lines:[/bold]")
        for line in logs.splitlines()[-50:]:
            err.print(f"  {clean(line, 1000)}", markup=False)


def _report_launch(bundle, result: LaunchResult, *, port: int, timeout: float) -> int:
    out.print()
    if result.outcome == "ready":
        status = result.status or 0
        out.print("Build: [green]ok[/green]")
        out.print("Container: [green]started[/green]")
        note = (
            " [yellow](server error: the app answered but is reporting a failure)[/yellow]"
            if status >= 500
            else ""
        )
        out.print(
            f"Readiness: an HTTP endpoint responded with status {status} at {esc(result.url)}{note}"
        )
        out.print(
            "[dim]This only shows that something answered; "
            "r2t does not check that the app works.[/dim]"
        )
        if bundle.framework == "fastapi":
            out.print(f"FastAPI docs, if enabled: http://127.0.0.1:{port}/docs")
        out.print(f"Open: [bold]{esc(result.url)}[/bold]")
        d = shlex.quote(str(bundle.path))
        out.print(f"Logs: r2t logs {d}    Stop: r2t stop {d}")
        return 0
    if result.outcome == "interrupted_build":
        err.print("[yellow]interrupted during the build[/yellow]; no container was started")
        return EXIT_INTERRUPTED
    if result.outcome == "build_failed":
        err.print(
            "[red]Build failed[/red] (docker compose build exited with "
            f"{result.exit_code}). Output is shown above; the last lines:"
        )
        for line in [x for x in result.build_tail.splitlines() if x.strip()][-20:]:
            err.print(f"  {clean(line, 1000)}", markup=False)
        return EXIT_BUILD
    if result.outcome == "up_failed":
        err.print(f"[red]The container could not be started[/red] (exit {result.exit_code}).")
        _print_logs(result.logs)
        _print_followups(bundle.path)
        return EXIT_RUNTIME
    if result.outcome == "interrupted_wait":
        err.print("[yellow]interrupted[/yellow]; containers were left as they are")
        _print_logs(result.logs)
        _print_followups(bundle.path)
        return EXIT_INTERRUPTED
    if result.outcome == "exited":
        err.print(
            f"[red]The container exited[/red] before answering (exit code {result.exit_code})."
        )
    elif result.outcome == "missing":
        err.print(
            "[red]The container was not found[/red] while waiting for it; an HTTP response, "
            "if any, was not attributed to the app."
        )
    else:
        extra = f" Last probe error: {esc(result.last_error)}." if result.last_error else ""
        err.print(
            f"[red]Readiness timed out[/red] after {timeout:g}s waiting for "
            f"{esc(result.url)}.{extra}"
        )
    _print_logs(result.logs)
    _print_followups(bundle.path)
    return EXIT_RUNTIME


# ---- logs and stop ------------------------------------------------------------------------


@app.command()
@guarded
def logs(
    ctx: typer.Context,
    directory: str = typer.Argument(..., help="Bundle directory."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output."),
    tail: int = typer.Option(100, "--tail", min=1, help="Number of lines to show."),
) -> None:
    """Show container logs for a bundle."""
    svc = services_of(ctx)
    bundle = load_bundle(directory)
    argv = compose_argv(bundle, "logs", "--no-color", "--tail", str(tail))
    if follow:
        argv.insert(len(argv) - 2, "--follow")
        result = svc.runner(
            argv, cwd=bundle.path, env=compose_env(), timeout=24 * 3600, stream="inherit"
        )
        raise typer.Exit(0 if result.ok or result.returncode in (130, -2) else EXIT_RUNTIME)
    result = svc.runner(argv, cwd=bundle.path, env=compose_env(), timeout=120)
    if result.stdout:
        sys.stdout.write(result.stdout if result.stdout.endswith("\n") else result.stdout + "\n")
    if not result.ok:
        err.print(esc(result.stderr.strip() or "docker compose logs failed"))
        raise typer.Exit(EXIT_RUNTIME)


@app.command()
@guarded
def stop(
    ctx: typer.Context,
    directory: str = typer.Argument(..., help="Bundle directory."),
) -> None:
    """Stop and remove this bundle's containers. Images and volumes are left alone."""
    svc = services_of(ctx)
    bundle = load_bundle(directory)
    result = svc.runner(
        compose_argv(bundle, "down"), cwd=bundle.path, env=compose_env(), timeout=300
    )
    if not result.ok:
        err.print(esc(result.stderr.strip() or "docker compose down failed"))
        raise DockerError(f"`docker compose down` failed for project {bundle.project}")
    out.print(f"Stopped project [bold]{esc(bundle.project)}[/bold].")
    image = bundle.image or f"r2t/{bundle.project}:latest"
    out.print(
        "The built image was kept. To remove it: "
        f"[bold]docker image rm {esc(shlex.quote(image))}[/bold]"
    )


def main() -> None:
    app()
