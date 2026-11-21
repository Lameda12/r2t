"""`r2t bench`: how long from a fresh checkout until the app serves HTTP?

Each trial checks out the committed HEAD of a local Git repository into a temporary
directory, plans a bundle, builds it without Docker's layer cache, starts it, and stops the
clock when the existing readiness check reports an HTTP response. Trials run sequentially.
Everything a trial creates (temp directory, Compose project, image) is removed afterwards,
including after a timeout or an interrupt, and nothing else is touched.
"""

from __future__ import annotations

import os
import platform
import shutil
import statistics
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .analyze import analyze
from .docker import (
    LaunchResult,
    compose_argv,
    compose_env,
    launch,
    load_bundle,
    validate_bundle,
)
from .errors import DockerError, InputError, R2TError
from .models import SUPPORTED, Inspection, Options, SourceIdentity
from .readiness import Probe
from .render import write_bundle
from .runner import Runner, base_env
from .source import GIT_QUICK_TIMEOUT, GIT_TIMEOUT, Acquired, git_env, local_identity
from .text import clean, code_span

BUILD_FLAGS = ("--no-cache",)

# Local clone of the user's own repository: the `file` transport must be allowed here (the
# remote-clone hardening blocks it), but hooks and fsmonitor stay off and the clone never
# reads the source repository's config, so its filter drivers cannot run.
_LOCAL_GIT = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")

# stage -> process exit code, for the first failed trial
STAGE_EXIT = {
    "checkout": 2,
    "plan": 3,
    "validate": 4,
    "build": 5,
    "start": 6,
    "readiness": 6,
    "interrupted": 130,
}


@dataclass
class Trial:
    index: int
    ok: bool
    elapsed: float
    http_status: int | None = None
    stage: str | None = None
    reason: str | None = None
    cleanup_error: str | None = None


@dataclass
class BenchReport:
    repo: str
    commit: str
    dirty: bool | None
    requested_runs: int
    timeout: float
    trials: list[Trial] = field(default_factory=list)
    environment: list[str] = field(default_factory=list)

    @property
    def successes(self) -> list[Trial]:
        return [t for t in self.trials if t.ok]

    @property
    def median(self) -> float | None:
        times = [t.elapsed for t in self.successes]
        return statistics.median(times) if times else None

    @property
    def interrupted(self) -> bool:
        return any(t.stage == "interrupted" for t in self.trials)

    @property
    def exit_code(self) -> int:
        for t in self.trials:
            if not t.ok:
                return STAGE_EXIT.get(t.stage or "", 1)
        return 0


class TrialFailure(Exception):
    def __init__(self, stage: str, reason: str) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


# ---- the repository under test ------------------------------------------------------------


def resolve_repo(path: str, runner: Runner) -> tuple[Path, SourceIdentity]:
    """The repository root and its committed HEAD. Read-only Git commands only."""
    if path.startswith("-"):
        raise InputError(f"invalid repository path {path!r}: must not start with '-'")
    repo = Path(path).expanduser()
    if not repo.is_dir():
        raise InputError(f"not a directory: {path}")
    repo = repo.resolve()
    identity = local_identity(repo, runner)
    if not identity.git_head:
        raise InputError(
            f"{repo} is not a Git repository with at least one commit",
            "bench measures the committed HEAD; run `git init` and commit first",
        )
    top = runner(
        [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(repo),
            "rev-parse",
            "--show-toplevel",
        ],
        env=git_env(),
        timeout=GIT_QUICK_TIMEOUT,
    )
    if top.ok and Path(top.stdout.strip()).resolve() != repo:
        raise InputError(
            f"{repo} is inside a larger Git repository ({top.stdout.strip()})",
            "pass the repository root and select the app with --subdir",
        )
    return repo, identity


def checkout_head(runner: Runner, repo: Path, sha: str, dest: Path) -> None:
    """Fresh checkout of exactly `sha` into `dest` (never touches `repo`)."""
    env = {**git_env(), "GIT_ALLOW_PROTOCOL": "file"}
    steps = [
        (
            "clone",
            [
                "git",
                *_LOCAL_GIT,
                "-c",
                "protocol.file.allow=always",
                "clone",
                "--quiet",
                "--no-checkout",
                "--no-hardlinks",
                "--",
                str(repo),
                str(dest),
            ],
        ),
        (
            "checkout",
            ["git", *_LOCAL_GIT, "-C", str(dest), "checkout", "--quiet", "--detach", sha],
        ),
    ]
    for name, argv in steps:
        result = runner(argv, env=env, timeout=GIT_TIMEOUT)
        if not result.ok:
            detail = (result.stderr or result.stdout).strip().splitlines()
            why = "timed out" if result.timed_out else (detail[-1] if detail else "no output")
            raise TrialFailure("checkout", f"git {name} failed: {clean(why, 200)}")


def preflight(runner: Runner, repo: Path, sha: str, opts: Options) -> Inspection:
    """Inspect a throwaway checkout so the user can review the plan before any timing starts."""
    tmp = Path(tempfile.mkdtemp(prefix="r2t-bench-review-"))
    try:
        try:
            checkout_head(runner, repo, sha, tmp / "src")
        except TrialFailure as exc:
            raise InputError(exc.reason) from None
        insp, _ = _analyze_checkout(runner, tmp / "src", sha, opts)
        insp.source.path = str(repo)  # the review copy is throwaway; show the real repository
        return insp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _analyze_checkout(runner: Runner, src: Path, sha: str, opts: Options):
    del runner  # analysis never runs anything; the identity is already known
    identity = SourceIdentity(kind="local", path=str(src), git_head=sha, dirty=False)
    return analyze(Acquired(src, identity), opts)


# ---- one trial ----------------------------------------------------------------------------


def _last_line(text: str, prefer: tuple[str, ...] = ()) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if any(word in ln for word in prefer):
            return clean(ln, 220)
    return clean(lines[-1], 220) if lines else "no output"


def _failure_from(result: LaunchResult, timeout: float) -> TrialFailure | None:
    """Map the existing launch outcome to a (stage, reason); None means success."""
    o = result.outcome
    if o == "ready":
        return None
    if o == "build_failed":
        why = _last_line(result.build_tail, ("ERROR", "error", "failed"))
        return TrialFailure("build", f"docker compose build exited {result.exit_code}: {why}")
    if o == "up_failed":
        why = _last_line(result.build_tail or result.logs)
        return TrialFailure("start", f"docker compose up exited {result.exit_code}: {why}")
    if o == "exited":
        tail = f"; last log line: {_last_line(result.logs)}" if result.logs else ""
        return TrialFailure(
            "readiness", f"container exited with code {result.exit_code} before answering{tail}"
        )
    if o == "missing":
        return TrialFailure("readiness", "the container was not found while waiting for it")
    if o == "timeout":
        extra = f" (last probe error: {clean(result.last_error, 120)})" if result.last_error else ""
        return TrialFailure("readiness", f"no HTTP response within {timeout:g}s{extra}")
    return TrialFailure("interrupted", "interrupted by the user")


def cleanup(runner: Runner, bundle) -> str | None:
    """Remove exactly what this trial created: its Compose project and its image.

    The project name and image tag carry a random per-trial id, so nothing else matches.
    Never uses -v, --rmi, prune, or any name pattern. Returns an error note, or None.
    """
    if bundle is None:
        return None
    notes = []
    down = runner(compose_argv(bundle, "down"), cwd=bundle.path, env=compose_env(), timeout=300)
    if not down.ok:
        notes.append(f"docker compose down failed for {bundle.project}")
    if bundle.image and bundle.image.startswith(f"r2t/{bundle.project}"):
        rm = runner(["docker", "image", "rm", bundle.image], env=base_env(), timeout=120)
        if not rm.ok and "No such image" not in (rm.stderr + rm.stdout):
            notes.append(f"could not remove image {bundle.image}")
    return "; ".join(notes) or None


def run_trial(
    index: int,
    *,
    repo: Path,
    sha: str,
    opts: Options,
    runner: Runner,
    probe: Probe,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    port_free: Callable[[int], bool],
    timeout: float,
    host_port: int | None,
    say: Callable[[str], None],
) -> Trial:
    started = clock()  # before the temp dir and checkout exist
    tmp = Path(tempfile.mkdtemp(prefix="r2t-bench-"))
    bundle = None
    try:
        say("checkout")
        checkout_head(runner, repo, sha, tmp / "src")
        say("plan")
        try:
            insp, snap = _analyze_checkout(runner, tmp / "src", sha, opts)
            if insp.status != SUPPORTED:
                why = "; ".join(b.reason for b in insp.blockers) or insp.status
                raise TrialFailure("plan", f"target is {insp.status}: {clean(why, 220)}")
            written = write_bundle(insp, snap, tmp / "bundle", tmp / "src")
        except R2TError as exc:
            raise TrialFailure("plan", clean(exc.message, 220)) from None
        bundle = load_bundle(written.path)
        try:
            _, published = validate_bundle(bundle, runner, host_port or bundle.port)
        except DockerError as exc:
            raise TrialFailure("validate", clean(exc.message.splitlines()[0], 220)) from None
        if not port_free(published):
            raise TrialFailure("validate", f"port {published} on 127.0.0.1 is already in use")
        result = launch(
            bundle,
            runner,
            host_port=published,
            timeout=timeout,
            probe=probe,
            clock=clock,
            sleep=sleep,
            say=say,
            build_args=BUILD_FLAGS,
        )
        elapsed = clock() - started  # the readiness check has just passed (or failed)
        failure = _failure_from(result, timeout)
        if failure:
            raise TrialFailure(failure.stage, failure.reason)
        trial = Trial(index, True, elapsed, http_status=result.status)
    except TrialFailure as exc:
        trial = Trial(index, False, clock() - started, stage=exc.stage, reason=exc.reason)
    except KeyboardInterrupt:
        trial = Trial(
            index, False, clock() - started, stage="interrupted", reason="interrupted by the user"
        )
    finally:
        note = None
        try:
            note = cleanup(runner, bundle)
        except KeyboardInterrupt:
            note = "interrupted during cleanup; check `docker ps -a` for this trial's project"
        shutil.rmtree(tmp, ignore_errors=True)
    trial.cleanup_error = note
    return trial


# ---- runner details and report ------------------------------------------------------------


def environment_details(runner: Runner, compose_version: str) -> list[str]:
    info = runner(
        ["docker", "info", "--format", "{{.ServerVersion}}\t{{.NCPU}}\t{{.MemTotal}}"],
        env=base_env(),
        timeout=60,
    )
    engine = "unknown"
    if info.ok and info.stdout.strip():
        parts = info.stdout.strip().split("\t")
        engine = f"Docker Engine {clean(parts[0], 40)}"
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            engine += f" ({parts[1]} CPUs, {int(parts[2]) / 2**30:.1f} GiB visible to Docker)"
    return [
        f"r2t {__version__}, Python {platform.python_version()}",
        f"Host: {clean(platform.platform(), 120)}, {os.cpu_count()} CPUs",
        f"{engine}, Docker Compose {clean(compose_version, 20)}",
        f"Measured {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
    ]


def _cell(text: object) -> str:
    return clean(text, 300).replace("|", "\\|")


def _secs(value: float) -> str:
    return f"{value:.1f}s"


def render_markdown(report: BenchReport) -> str:
    n = len(report.trials)
    ok = len(report.successes)
    median = report.median
    out = [
        "## r2t bench",
        "",
        "| Repo | Commit | Successful runs | Median time to HTTP |",
        "|---|---|---|---|",
        f"| {_cell(report.repo)} | `{report.commit[:12]}` | {ok}/{n} | "
        f"{_secs(median) if median is not None else 'n/a (no successful run)'} |",
        "",
    ]
    failed = [t for t in report.trials if not t.ok]
    out.append("### Failures")
    if failed:
        out += [
            f"- Run {t.index}: stage `{t.stage}`: {code_span(t.reason or 'unknown')}"
            for t in failed
        ]
    else:
        out.append("None.")
    out.append("")
    out += ["### Runs", "", "| Run | Result | Elapsed | HTTP status |", "|---|---|---|---|"]
    for t in report.trials:
        result = "ok" if t.ok else f"failed at {t.stage}"
        status = str(t.http_status) if t.http_status is not None else "n/a"
        out.append(f"| {t.index} | {result} | {_secs(t.elapsed)} | {status} |")
    out.append("")
    out.append("### How to read these numbers")
    out += [
        f"- Tested commit: `{report.commit}` (committed HEAD"
        + ("; uncommitted changes in the working tree were ignored" if report.dirty else "")
        + ").",
        "- Timer: starts before the temporary checkout is created and stops when r2t's HTTP "
        "readiness check passes (any HTTP status, container confirmed running). It covers "
        "checkout, planning, bundle validation, the image build, container start and the wait. "
        "It excludes the review prompt and cleanup.",
        "- Median is over successful runs only; failed runs are listed above and never averaged "
        "in.",
        "- Cache policy: every trial uses a new temporary checkout, a new Compose project and "
        "image name, and `docker compose build --no-cache`, so no project layer or dependency "
        "install is reused from another trial or an earlier build. Base images are not pulled "
        "or cleared per trial and may be pre-pulled. Not controlled: registry, proxy or CDN "
        "caches, Docker's cache for base-image layers, the OS page cache, network speed.",
        f"- Trials ran sequentially ({n} of {report.requested_runs} requested"
        + (", stopped by an interrupt" if report.interrupted else "")
        + f"); readiness timeout {report.timeout:g}s per run.",
        "- Cleanup: after each trial r2t ran `docker compose down` for that trial's project and "
        "removed that trial's image and temporary directory; nothing else was touched.",
    ]
    out.append("- Runner:")
    out += [f"  - {line}" for line in report.environment]
    notes = [t for t in report.trials if t.cleanup_error]
    if notes:
        out.append("")
        out.append("### Cleanup warnings")
        out += [f"- Run {t.index}: {_cell(t.cleanup_error)}" for t in notes]
    return "\n".join(out) + "\n"
