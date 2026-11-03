"""Docker Compose operations, bundle loading, and bundle safety validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import SCHEMA_VERSION
from .errors import DockerError, InputError
from .readiness import ContainerState, Probe, ReadyOutcome, wait_for_ready
from .runner import Runner, base_env

MIN_COMPOSE = (2, 24, 0)  # first release with env_file `required: false`
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
REQUIRED_FILES = ("plan.json", "compose.yaml", "Dockerfile")
BUILD_TIMEOUT = 1800
QUICK_TIMEOUT = 60


# ---- bundle and plan ----------------------------------------------------------------------


@dataclass
class Bundle:
    path: Path
    plan: dict
    project: str
    image: str | None
    port: int
    readiness_path: str
    framework: str | None
    compose_file: Path = field(init=False)

    def __post_init__(self) -> None:
        self.compose_file = self.path / "compose.yaml"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_bundle(directory: str | Path, *, need_files: bool = True) -> Bundle:
    """Load plan.json from a bundle directory and validate everything we will rely on."""
    path = Path(directory).expanduser()
    if not path.is_dir():
        raise InputError(f"bundle directory not found: {directory}")
    path = path.resolve()
    plan_file = path / "plan.json"
    if not plan_file.is_file():
        raise DockerError(f"{path} is not an r2t bundle: plan.json is missing")
    try:
        plan = json.loads(plan_file.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise DockerError(f"plan.json could not be read: {exc}") from exc
    if not isinstance(plan, dict) or not isinstance(plan.get("schema_version"), int):
        raise DockerError("plan.json is not a valid r2t plan")
    if plan["schema_version"] > SCHEMA_VERSION:
        raise DockerError(
            f"plan.json uses schema_version {plan['schema_version']}, newer than this r2t "
            f"understands ({SCHEMA_VERSION})",
            "upgrade r2t",
        )
    if plan.get("status") != "supported":
        raise DockerError(
            f"this bundle's status is {plan.get('status')!r}, not 'supported'; "
            "no Dockerfile or compose.yaml was generated",
            "read REPORT.md for the blockers and flags that resolve them",
        )
    project = plan.get("compose_project")
    if not isinstance(project, str) or not _PROJECT_RE.match(project):
        raise DockerError("plan.json has an invalid compose_project")
    port = plan.get("container_port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise DockerError("plan.json has an invalid container_port")
    rpath = plan.get("readiness_path", "/")
    if not isinstance(rpath, str) or not re.fullmatch(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*", rpath):
        raise DockerError("plan.json has an invalid readiness_path")
    image = plan.get("image")
    bundle = Bundle(
        path,
        plan,
        project,
        image if isinstance(image, str) else None,
        port,
        rpath,
        plan.get("framework"),
    )
    if need_files:
        missing = [n for n in REQUIRED_FILES if not (path / n).is_file()]
        if not (path / "source").is_dir():
            missing.append("source/")
        if missing:
            raise DockerError(f"bundle is missing required files: {', '.join(missing)}")
    return bundle


def modified_files(bundle: Bundle) -> list[str]:
    """Names of generated files whose hash no longer matches plan.json."""
    files = bundle.plan.get("files") or {}
    changed = []
    for name, key in (("Dockerfile", "dockerfile_sha256"), ("compose.yaml", "compose_sha256")):
        recorded = files.get(key)
        path = bundle.path / name
        if recorded and path.is_file() and sha256_file(path) != recorded:
            changed.append(name)
    return changed


# ---- compose commands ---------------------------------------------------------------------


def compose_argv(bundle: Bundle, *args: str) -> list[str]:
    """Every compose call is exactly: docker compose -p NAME -f DIR/compose.yaml ..."""
    return ["docker", "compose", "-p", bundle.project, "-f", str(bundle.compose_file), *args]


def compose_env(host_port: int | None = None) -> dict[str, str]:
    extra = {"R2T_HOST_PORT": str(host_port)} if host_port is not None else {}
    return base_env(extra)


def check_docker(runner: Runner) -> str:
    """Daemon reachable and Compose v2 new enough. Returns the Compose version string."""
    info = runner(
        ["docker", "info", "--format", "{{.ServerVersion}}"], env=base_env(), timeout=QUICK_TIMEOUT
    )
    if info.returncode == 127:
        raise DockerError("the `docker` command was not found", "install Docker and try again")
    if not info.ok:
        raise DockerError(
            "the Docker daemon is not reachable",
            "start Docker (Docker Desktop, or `systemctl start docker` on Linux) and retry",
        )
    ver = runner(["docker", "compose", "version", "--short"], env=base_env(), timeout=QUICK_TIMEOUT)
    if not ver.ok:
        raise DockerError(
            "Docker Compose v2 (`docker compose`) is not available",
            "install the Docker Compose plugin (v2.24.0 or newer)",
        )
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", ver.stdout)
    if not m:
        raise DockerError(f"could not parse the Docker Compose version: {ver.stdout.strip()!r}")
    version = tuple(int(x) for x in m.groups())
    if version < MIN_COMPOSE:
        need = ".".join(map(str, MIN_COMPOSE))
        raise DockerError(
            f"Docker Compose {m.group(0)} is too old: r2t bundles use `env_file` with "
            f"`required: false`, which needs Compose {need} or newer",
            "upgrade Docker Compose",
        )
    return m.group(0)


# ---- bundle validation --------------------------------------------------------------------

_HOST_NS = ("pid", "ipc", "uts", "cgroup", "userns_mode")


def _inside(path: str | Path, root: Path) -> bool:
    """Is path inside root once symlinks and `..` are resolved?"""
    try:
        resolved = Path(os.path.realpath(path))
        return resolved.is_relative_to(Path(os.path.realpath(root)))
    except (OSError, ValueError):
        return False


def _is_root_user(value: object) -> bool:
    name = str(value).strip().split(":", 1)[0].strip()
    return name == "root" or (name.isdigit() and int(name) == 0)


_HEREDOC = re.compile(r"<<(-?)\s*(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z_][\w.-]*))")


def _heredocs(rest: str) -> list[tuple[str, str]]:
    """(strip_tabs, delimiter) for each real heredoc operator in a RUN/COPY/ADD line.

    `<<` inside single or double quotes, or escaped with a backslash, is a literal string and
    opens nothing. Quoting that does not balance cannot be interpreted safely, so it raises
    DockerfileSyntaxError when the line mentions `<<` at all.
    """
    found: list[tuple[str, str]] = []
    quote = ""
    i = 0
    while i < len(rest):
        ch = rest[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(rest):
                i += 1
            elif ch == quote:
                quote = ""
        elif ch == "\\":
            i += 1
        elif ch in "\"'":
            quote = ch
        elif rest.startswith("<<", i):
            m = _HEREDOC.match(rest, i)
            if m:
                found.append((m.group(1), m.group(2) or m.group(3) or m.group(4)))
                i = m.end()
                continue
            i += 2
            continue
        i += 1
    if quote and "<<" in rest:
        raise DockerfileSyntaxError("quoting around a possible heredoc is unbalanced")
    return found


class DockerfileSyntaxError(ValueError):
    """Dockerfile text whose instruction boundaries cannot be established."""


def _instructions(text: str) -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs: comments dropped, backslash continuations joined.

    Instruction names are split on any whitespace. Heredoc bodies (`RUN <<EOF`, `COPY <<EOF`)
    belong to the instruction that opened them and are skipped, so text inside them is never
    mistaken for an instruction. An unterminated heredoc raises DockerfileSyntaxError.
    """
    out: list[tuple[str, str]] = []
    pending = ""
    lines = iter(text.splitlines())
    for raw in lines:
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.startswith("#"):  # comment line inside a continued instruction
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        full = (pending + line).strip()
        pending = ""
        parts = full.split(None, 1)
        name, rest = parts[0].upper(), (parts[1].strip() if len(parts) > 1 else "")
        out.append((name, rest))
        if name in ("RUN", "COPY", "ADD"):
            for strip_tabs, delimiter in _heredocs(rest):
                for body in lines:
                    if (body.lstrip("\t") if strip_tabs else body) == delimiter:
                        break
                else:
                    raise DockerfileSyntaxError(f"a heredoc ({delimiter}) is never terminated")
    if pending.strip():
        parts = pending.split(None, 1)
        out.append((parts[0].upper(), parts[1].strip() if len(parts) > 1 else ""))
    return out


def dockerfile_user(text: str, target: str | None = None) -> tuple[str | None, str | None]:
    """The user a Dockerfile's selected build stage ends with.

    Returns (user, error). user None with no error means root (no USER, external base image).
    A stage built FROM an earlier stage inherits that stage's user. Variables in FROM or
    USER cannot be resolved here, so they are an error rather than a guess.
    `target` selects a named stage (case-insensitive) instead of the last one.
    """
    stages: list[dict] = []  # {"name", "user", "unresolved"}
    by_name: dict[str, dict] = {}
    try:
        instructions = _instructions(text)
    except DockerfileSyntaxError as exc:
        return None, str(exc)
    for name, rest in instructions:
        if name == "FROM":
            tokens = [t for t in rest.split() if not t.startswith("--")]
            if not tokens:
                return None, "a FROM instruction has no image"
            base = tokens[0]
            stage = {"name": None, "user": None, "unresolved": "$" in base}
            if len(tokens) >= 3 and tokens[1].upper() == "AS":
                stage["name"] = tokens[2].lower()
            parent = by_name.get(base.lower())
            if parent is not None:
                stage["user"], stage["unresolved"] = parent["user"], parent["unresolved"]
            stages.append(stage)
            if stage["name"]:
                by_name[stage["name"]] = stage
        elif name == "USER" and stages:
            value = rest.split()[0] if rest.split() else ""
            if "$" in value:
                stages[-1]["unresolved"] = True
            else:
                stages[-1]["user"], stages[-1]["unresolved"] = value, False
    if not stages:
        return None, "the Dockerfile has no FROM instruction"
    if target:
        chosen = by_name.get(target.lower())
        if chosen is None:
            return None, f"build target {target!r} is not a stage of the Dockerfile"
    else:
        chosen = stages[-1]
    if chosen["unresolved"]:
        return None, "the user depends on a variable (ARG) that r2t cannot resolve"
    return chosen["user"], None


def boundary_problems(bundle_dir: Path) -> list[str]:
    """Bundle entries that are, or point through symlinks to, locations outside the bundle."""
    problems = []
    for name in ("Dockerfile", "compose.yaml", "plan.json", "source", ".env"):
        path = bundle_dir / name
        if os.path.lexists(path) and not _inside(path, bundle_dir):
            problems.append(f"{name} resolves (through a symlink) to a location outside the bundle")
    return problems


def safety_problems(config: dict, bundle_dir: Path) -> list[str]:
    """Inspect a *resolved* Compose config and list everything r2t refuses to run."""
    problems: list[str] = []
    services = config.get("services") or {}
    if not services:
        return ["the Compose file defines no services"]
    top_volumes = config.get("volumes") or {}
    if "docker.sock" in json.dumps(config):
        problems.append("it references the Docker socket (docker.sock)")
    for name, svc in services.items():
        tag = f"service {name!r}"
        if svc.get("privileged"):
            problems.append(f"{tag} is privileged")
        if svc.get("cap_add"):
            problems.append(f"{tag} adds capabilities (cap_add: {svc['cap_add']})")
        mode = str(svc.get("network_mode") or "")
        if mode == "host" or mode.startswith(("container:", "service:")):
            problems.append(f"{tag} uses network_mode {mode!r}")
        for key in _HOST_NS:
            if str(svc.get(key) or "") == "host":
                problems.append(f"{tag} uses the host namespace ({key}: host)")
        if svc.get("devices") or svc.get("device_cgroup_rules"):
            problems.append(f"{tag} maps host devices")
        if svc.get("volumes_from"):
            problems.append(f"{tag} uses volumes_from")
        if svc.get("secrets") or svc.get("configs"):
            problems.append(f"{tag} mounts secrets or configs from the host")
        for opt in svc.get("security_opt") or []:
            if "unconfined" in str(opt) or str(opt).replace(":", "=") == "label=disable":
                problems.append(f"{tag} disables a security profile ({opt})")
        for vol in svc.get("volumes") or []:
            if not isinstance(vol, dict):
                problems.append(f"{tag} has an unrecognized volume entry")
                continue
            vtype = vol.get("type")
            if vtype == "bind":
                problems.append(f"{tag} bind-mounts a host path ({vol.get('source')})")
            elif vtype == "volume":
                source = vol.get("source") or ""
                spec = top_volumes.get(source) or {}
                if spec.get("external"):
                    problems.append(
                        f"{tag} mounts external volume {source!r}, which this bundle did not create"
                    )
                elif spec.get("driver") not in (None, "", "local") or spec.get("driver_opts"):
                    problems.append(
                        f"{tag} uses volume {source!r} with a custom driver or driver options "
                        "(it can map a host path or remote storage)"
                    )
            elif vtype not in {"tmpfs"}:
                problems.append(f"{tag} has a volume of type {vtype!r}")
        for port in svc.get("ports") or []:
            if not isinstance(port, dict):
                problems.append(f"{tag} has an unrecognized port entry")
            elif port.get("published") and port.get("host_ip") != "127.0.0.1":
                problems.append(
                    f"{tag} publishes port {port.get('published')} on "
                    f"{port.get('host_ip') or 'all interfaces'}, not 127.0.0.1"
                )
        build = svc.get("build")
        user = svc.get("user")
        if user is not None and _is_root_user(user):
            problems.append(f"{tag} runs as root (user: {str(user).strip()!r})")
        if isinstance(build, dict):
            ctx = build.get("context")
            ctx_path = None
            if ctx:
                ctx_path = Path(ctx) if os.path.isabs(ctx) else bundle_dir / ctx
                if not _inside(ctx_path, bundle_dir):
                    problems.append(f"{tag} builds from a context outside the bundle ({ctx})")
            if build.get("additional_contexts") or build.get("ssh") or build.get("secrets"):
                problems.append(f"{tag} uses build additional_contexts, ssh or secrets")
            if build.get("network") == "host":
                problems.append(f"{tag} builds with the host network")
            if ctx_path is not None:
                inline = build.get("dockerfile_inline")
                df = build.get("dockerfile") or "Dockerfile"
                df_path = Path(df) if os.path.isabs(df) else ctx_path / df
                text = None
                if inline:
                    text = inline
                elif not _inside(df_path, bundle_dir):
                    problems.append(f"{tag} uses a Dockerfile outside the bundle ({df})")
                elif df_path.is_file():
                    text = df_path.read_text(errors="replace")
                if text is not None and user is None:
                    final, error = dockerfile_user(text, build.get("target"))
                    stage = f" (target {build['target']!r})" if build.get("target") else ""
                    if error:
                        problems.append(
                            f"{tag}: cannot verify that the container runs as non-root{stage}: "
                            f"{error}. Set `user:` in compose.yaml or simplify the Dockerfile"
                        )
                    elif final is None or _is_root_user(final):
                        problems.append(
                            f"{tag} would run as root{stage}: the selected Dockerfile stage "
                            "does not end with a non-root USER and compose.yaml sets no user"
                        )
        elif build is not None:
            problems.append(f"{tag} has an unrecognized build section")
    return problems


def _env_file_problems(bundle: Bundle, runner: Runner, host_port: int) -> list[str]:
    """env_file entries outside the bundle.

    Compose inlines env files into `environment` in the normal resolved config, so the paths
    are only visible with `--no-env-resolution` (Compose 2.36+). Older versions skip this check.
    """
    result = runner(
        compose_argv(bundle, "config", "--no-env-resolution", "--format", "json"),
        cwd=bundle.path,
        env=compose_env(host_port),
        timeout=QUICK_TIMEOUT,
    )
    if not result.ok:
        return []
    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    problems = []
    for name, svc in (config.get("services") or {}).items():
        entries = svc.get("env_file") or []
        if isinstance(entries, str | dict):
            entries = [entries]
        for entry in entries:
            path = entry.get("path") if isinstance(entry, dict) else entry
            if not path:
                continue
            full = Path(path) if os.path.isabs(path) else bundle.path / path
            if not _inside(full, bundle.path):
                problems.append(f"service {name!r} reads env_file {path!r}, outside the bundle")
    return problems


def validate_bundle(bundle: Bundle, runner: Runner, host_port: int) -> tuple[dict, int]:
    """Run `docker compose config` and enforce the safety rules. Returns (config, host port)."""
    result = runner(
        compose_argv(bundle, "config", "--format", "json"),
        cwd=bundle.path,
        env=compose_env(host_port),
        timeout=QUICK_TIMEOUT,
    )
    if not result.ok:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise DockerError(
            "`docker compose config` rejected the bundle: "
            + (detail[-1] if detail else "no output"),
            "fix compose.yaml or regenerate the bundle with `r2t plan`",
        )
    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DockerError(f"could not parse `docker compose config` output: {exc}") from exc
    problems = boundary_problems(bundle.path) + safety_problems(config, bundle.path)
    problems += _env_file_problems(bundle, runner, host_port)
    if problems:
        raise DockerError(
            "refusing to run an unsafe bundle:\n  - " + "\n  - ".join(problems),
            "r2t bundles must stay unprivileged, publish only on 127.0.0.1 and mount nothing",
        )
    published = None
    for svc in (config.get("services") or {}).values():
        for port in svc.get("ports") or []:
            if not port.get("published"):
                continue
            if published is None or str(port.get("target")) == str(bundle.port):
                published = int(port["published"])
    if published is None:
        raise DockerError("the Compose file publishes no port, so there is nothing to check")
    return config, published


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


# ---- running ------------------------------------------------------------------------------


def parse_ps(stdout: str) -> list[dict]:
    text = stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        rows = []
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows


def container_state(bundle: Bundle, runner: Runner, host_port: int) -> ContainerState:
    result = runner(
        compose_argv(bundle, "ps", "--all", "--format", "json"),
        cwd=bundle.path,
        env=compose_env(host_port),
        timeout=QUICK_TIMEOUT,
    )
    if not result.ok:
        return ContainerState("unknown")  # could not ask; not the same as "no container"
    rows = parse_ps(result.stdout)
    if not rows:
        return ContainerState("missing")
    for row in rows:
        state = str(row.get("State", "")).lower()
        if state in {"exited", "dead"}:
            code = row.get("ExitCode")
            return ContainerState(state, code if isinstance(code, int) else None)
    return ContainerState(str(rows[0].get("State", "unknown")).lower())


def tail_logs(bundle: Bundle, runner: Runner, host_port: int, lines: int = 50) -> str:
    result = runner(
        compose_argv(bundle, "logs", "--no-color", "--tail", str(lines)),
        cwd=bundle.path,
        env=compose_env(host_port),
        timeout=QUICK_TIMEOUT,
    )
    return (result.stdout + result.stderr).strip()


@dataclass
class LaunchResult:
    outcome: str  # ready, build_failed, up_failed, exited, missing, timeout, interrupted_*
    url: str
    status: int | None = None
    exit_code: int | None = None
    build_tail: str = ""
    logs: str = ""
    last_error: str | None = None


def launch(
    bundle: Bundle,
    runner: Runner,
    *,
    host_port: int,
    timeout: float,
    probe: Probe,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    say: Callable[[str], None],
) -> LaunchResult:
    """build -> up -d -> poll readiness. Containers are always left running for inspection."""
    url = f"http://127.0.0.1:{host_port}{bundle.readiness_path}"
    env = compose_env(host_port)
    say("build")
    try:
        build = runner(
            compose_argv(bundle, "build"),
            cwd=bundle.path,
            env=env,
            timeout=BUILD_TIMEOUT,
            stream=True,
        )
    except KeyboardInterrupt:
        return LaunchResult("interrupted_build", url)
    if not build.ok:
        return LaunchResult(
            "build_failed", url, exit_code=build.returncode, build_tail=build.stdout
        )
    say("up")
    try:
        up = runner(
            compose_argv(bundle, "up", "-d"),
            cwd=bundle.path,
            env=env,
            timeout=BUILD_TIMEOUT,
            stream=True,
        )
    except KeyboardInterrupt:
        return LaunchResult("interrupted_wait", url)
    if not up.ok:
        logs = tail_logs(bundle, runner, host_port)
        return LaunchResult(
            "up_failed", url, exit_code=up.returncode, build_tail=up.stdout, logs=logs
        )
    say("wait")
    try:
        outcome: ReadyOutcome = wait_for_ready(
            url,
            timeout,
            probe=probe,
            container_state=lambda: container_state(bundle, runner, host_port),
            clock=clock,
            sleep=sleep,
        )
    except KeyboardInterrupt:
        return LaunchResult("interrupted_wait", url, logs=tail_logs(bundle, runner, host_port))
    if outcome.kind == "responded":
        return LaunchResult("ready", url, status=outcome.status)
    logs = tail_logs(bundle, runner, host_port)
    return LaunchResult(
        outcome.kind, url, exit_code=outcome.exit_code, logs=logs, last_error=outcome.last_error
    )
