"""Node.js detection: Vite and Next.js dev servers with npm or pnpm."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from ..errors import InputError
from ..models import (
    NEEDS_INPUT,
    UNSUPPORTED,
    Evidence,
    Inspection,
    LifecycleScript,
    Options,
    ServiceHint,
    file_evidence,
    override_evidence,
)
from ..snapshot import Snapshot
from ..text import clean
from .common import NODE_SERVICE_DEPS
from .semver import RangeError, parse_range, satisfies_major

# Checked on 2026-11-03 against https://nodejs.org/dist/index.json and
# https://github.com/nodejs/Release/blob/main/schedule.json: 22 (maintenance until 2027-04-30),
# 24 (maintenance since 2026-10-20) and 26 (active LTS since 2026-10-28). 20 reached end of
# life on 2026-04-30.
NODE_MAINTAINED = (22, 24, 26)
NODE_DEFAULT = 24
PNPM_DEFAULT = "10.34.9"  # newest 10.x on npm on 2026-11-03; matches lockfileVersion 9.0
VITE_DEFAULT_PORT = 5173
NEXT_DEFAULT_PORT = 3000

LOCKFILES = {
    "package-lock.json": "npm",
    "npm-shrinkwrap.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "bun.lockb": "bun",
    "bun.lock": "bun",
}
INSTALL_LIFECYCLE = (
    "preinstall",
    "install",
    "postinstall",
    "prepublish",
    "preprepare",
    "prepare",
    "postprepare",
)
START_LIFECYCLE = ("predev", "postdev")
_SHELL_SYNTAX = re.compile(r"[;&|<>`$(){}\\\n\r]")
PM_FLAG = "--package-manager {npm,pnpm}"
RUNTIME_FLAG = "--runtime-version MAJOR"


class ScriptUnsupported(Exception):
    pass


def parse_dev_script(script: str) -> tuple[str, int | None]:
    """Return (framework, port-from-script) for a simple `vite`/`next dev` invocation."""
    if _SHELL_SYNTAX.search(script):
        raise ScriptUnsupported("it uses shell syntax (chaining, redirects or substitutions)")
    try:
        tokens = shlex.split(script)
    except ValueError as exc:
        raise ScriptUnsupported(f"it cannot be parsed ({exc})") from exc
    if not tokens:
        raise ScriptUnsupported("it is empty")
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        raise ScriptUnsupported("it sets environment variables inline")
    binary = tokens[0]
    if binary not in {"vite", "next"}:
        raise ScriptUnsupported(
            f"it runs {clean(binary)!r}, not a simple `vite` or `next dev` invocation"
        )
    rest = tokens[1:]
    if binary == "vite":
        if rest and rest[0] in {"build", "preview", "optimize"}:
            raise ScriptUnsupported(f"`vite {rest[0]}` is not a dev server")
        if rest and rest[0] in {"dev", "serve"}:
            rest = rest[1:]
        port_flags: tuple[str, ...] = ("--port",)
    else:
        if rest and not rest[0].startswith("-"):
            if rest[0] != "dev":
                raise ScriptUnsupported(f"`next {clean(rest[0])}` is not the dev server")
            rest = rest[1:]
        port_flags = ("--port", "-p")
    port: int | None = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        value: str | None = None
        for flag in port_flags:
            if tok == flag and i + 1 < len(rest):
                value = rest[i + 1]
                i += 1
            elif tok.startswith(flag + "="):
                value = tok.split("=", 1)[1]
        if value is not None:
            if not value.isdigit() or not 1 <= int(value) <= 65535:
                raise ScriptUnsupported(f"its port {clean(value)!r} is not a valid number")
            port = int(value)
        i += 1
    return binary, port


def node_versions_for(text: str) -> set[int]:
    """Node majors (10-39) that a version range or .nvmrc-style string allows."""
    intervals = parse_range(text)
    return {m for m in range(10, 40) if satisfies_major(intervals, m)}


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def _node_version(insp: Inspection, snap: Snapshot, pkg: dict, opts: Options) -> None:
    insp.runtime_name = "node"
    if opts.runtime_version is not None:
        if not re.fullmatch(r"\d{1,2}", opts.runtime_version):
            raise InputError(
                "--runtime-version for Node must be a major version like 22, "
                f"got {opts.runtime_version!r}"
            )
        major = int(opts.runtime_version)
        insp.runtime_constraints.append(f"--runtime-version {major}")
        insp.base_image = f"node:{major}-bookworm-slim"
        insp.add("runtime", f"Node {major}", override_evidence(f"--runtime-version {major}"))
        if major not in NODE_MAINTAINED:
            insp.assumptions.append(
                f"Node {major} is outside the maintained set {list(NODE_MAINTAINED)}; "
                "used because you asked for it"
            )
        return

    sources: list[tuple[str, str, Evidence]] = []
    for name in (".nvmrc", ".node-version"):
        if snap.has_file(name):
            sources.append((name, _first_line(snap.read_text(name)), file_evidence(name)))
    engines = pkg.get("engines")
    if isinstance(engines, dict) and "node" in engines:
        value = engines["node"]
        sources.append(
            (
                "package.json#engines.node",
                value if isinstance(value, str) else "",
                file_evidence("package.json", "engines.node"),
            )
        )
    if not sources:
        insp.base_image = f"node:{NODE_DEFAULT}-bookworm-slim"
        insp.assumptions.append(
            "No Node version is declared (.nvmrc, .node-version, engines.node); "
            f"using the default LTS major {NODE_DEFAULT}"
        )
        insp.add("runtime", f"Node {NODE_DEFAULT}", Evidence("default", detail="default LTS major"))
        return

    allowed: set[int] | None = None
    evidences = []
    for label, text, ev in sources:
        insp.runtime_constraints.append(f"{label}: {clean(text)}")
        evidences.append(ev)
        try:
            majors = node_versions_for(text)
        except RangeError as exc:
            insp.block(
                NEEDS_INPUT,
                f"cannot interpret the Node version in {label} ({clean(text)!r}): {exc}",
                RUNTIME_FLAG,
                ev,
            )
            return
        allowed = majors if allowed is None else allowed & majors
    assert allowed is not None  # noqa: S101
    found = "; ".join(insp.runtime_constraints)
    if not allowed:
        insp.block(
            NEEDS_INPUT, f"the Node version sources conflict ({found})", RUNTIME_FLAG, *evidences
        )
        return
    choices = sorted(allowed & set(NODE_MAINTAINED))
    if not choices:
        insp.block(
            NEEDS_INPUT,
            f"no maintained Node major {list(NODE_MAINTAINED)} satisfies {found}",
            RUNTIME_FLAG,
            *evidences,
        )
        return
    major = choices[-1]
    insp.base_image = f"node:{major}-bookworm-slim"
    insp.add("runtime", f"Node {major}", *evidences)
    if len(choices) > 1:
        others = ", ".join(map(str, choices[:-1]))
        insp.assumptions.append(
            f"Node {major} is the highest maintained major that satisfies the declared "
            f"constraints (also allowed: {others})"
        )


def _package_manager(insp: Inspection, snap: Snapshot, pkg: dict, opts: Options) -> bool:
    """Decide the package manager and install command. Returns False when blocked."""
    locks = {name: pm for name, pm in LOCKFILES.items() if snap.has_file(name)}
    npm_locks = [n for n, pm in locks.items() if pm == "npm"]
    pnpm_locks = [n for n, pm in locks.items() if pm == "pnpm"]
    lock_ev = [file_evidence(n) for n in locks]
    present = set(locks.values())

    declared = pkg.get("packageManager")
    decl_name = decl_version = None
    decl_ev = file_evidence("package.json", "packageManager")
    if isinstance(declared, str):
        m = re.match(r"^([a-z]+)@([^+\s]+)", declared)
        if m:
            decl_name, decl_version = m.group(1), m.group(2)

    pm: str
    pm_ev: list[Evidence]
    if opts.package_manager:
        pm = opts.package_manager
        pm_ev = [override_evidence(f"--package-manager {pm}")]
    elif present and present <= {"yarn", "bun"}:
        insp.block(
            UNSUPPORTED,
            f"{', '.join(locks)} found: Yarn and Bun projects are not supported in the MVP "
            "(npm and pnpm only)",
            None,
            *lock_ev,
        )
        return False
    elif len(present) > 1:
        insp.block(
            NEEDS_INPUT,
            f"multiple lockfiles found ({', '.join(locks)}); r2t cannot tell which one the "
            "project uses",
            PM_FLAG,
            *lock_ev,
        )
        return False
    elif decl_name in {"yarn", "bun"}:
        insp.block(
            UNSUPPORTED,
            f"package.json declares packageManager {declared!r}: Yarn and Bun are not supported",
            None,
            decl_ev,
        )
        return False
    elif decl_name in {"npm", "pnpm"}:
        pm, pm_ev = decl_name, [decl_ev]
        conflict = pnpm_locks if pm == "npm" else npm_locks
        if conflict:
            insp.block(
                NEEDS_INPUT,
                f"packageManager says {declared!r} but the lockfile is {conflict[0]}",
                PM_FLAG,
                decl_ev,
                file_evidence(conflict[0]),
            )
            return False
    elif npm_locks:
        pm, pm_ev = "npm", [file_evidence(npm_locks[0])]
    elif pnpm_locks:
        pm, pm_ev = "pnpm", [file_evidence(pnpm_locks[0])]
    else:
        pm, pm_ev = "npm", [Evidence("default", detail="no lockfile or packageManager field")]
        insp.assumptions.append("No lockfile or packageManager field; assuming npm")

    insp.package_manager = pm
    insp.add("package_manager", pm, *pm_ev)

    lockfile = None
    if pm == "npm" and npm_locks:
        lockfile = npm_locks[0]
    elif pm == "pnpm" and pnpm_locks:
        lockfile = pnpm_locks[0]
    insp.lockfile = lockfile
    if lockfile:
        insp.lock_status = "locked"
        insp.install_argv = (
            ["npm", "ci"] if pm == "npm" else ["pnpm", "install", "--frozen-lockfile"]
        )
        insp.add("lockfile", lockfile, file_evidence(lockfile))
    else:
        insp.lock_status = "unlocked"
        insp.install_argv = ["npm", "install"] if pm == "npm" else ["pnpm", "install"]
        ignored = [n for n in locks if LOCKFILES[n] != pm]
        if ignored:
            insp.assumptions.append(
                f"Ignoring {', '.join(ignored)} because the package manager is {pm}"
            )
        insp.add("lockfile", "none", Evidence("default", detail=f"no lockfile for {pm}"))

    if pm == "pnpm":
        if (
            decl_name == "pnpm"
            and decl_version
            and re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?", decl_version)
        ):
            insp.package_manager_version = decl_version
            insp.add("pnpm_version", decl_version, decl_ev)
        else:
            insp.package_manager_version = PNPM_DEFAULT
            insp.assumptions.append(
                f"pnpm version is not pinned by the repo; r2t installs pnpm@{PNPM_DEFAULT}"
            )
            insp.add("pnpm_version", PNPM_DEFAULT, Evidence("default", detail="chosen by r2t"))
    return True


def _workspace_checks(
    insp: Inspection, snap: Snapshot, pkg: dict, repo_root: Path, app_rel: str
) -> None:
    if "workspaces" in pkg:
        insp.block(
            UNSUPPORTED,
            "package.json declares `workspaces`: monorepos are not supported in the MVP "
            "(try --subdir on a standalone app)",
            None,
            file_evidence("package.json", "workspaces"),
        )
    if snap.has_file("pnpm-workspace.yaml"):
        insp.block(
            UNSUPPORTED,
            "pnpm-workspace.yaml found: workspaces are not supported in the MVP",
            None,
            file_evidence("pnpm-workspace.yaml"),
        )
    if app_rel == ".":
        return
    own = any(snap.has_file(n) for n in LOCKFILES)
    parent = Path(app_rel).parent
    while True:
        base = repo_root if parent == Path(".") else repo_root / parent
        prefix = "" if parent == Path(".") else parent.as_posix() + "/"
        found = [n for n in (*LOCKFILES, "pnpm-workspace.yaml") if (base / n).is_file()]
        if found and (not own or "pnpm-workspace.yaml" in found):
            insp.block(
                UNSUPPORTED,
                f"{prefix}{found[0]} lives in an ancestor directory of {app_rel}: the app "
                "looks like a workspace member, which the MVP does not support",
                None,
                Evidence("file", prefix + found[0], detail="ancestor of the app root"),
            )
            break
        if parent == Path("."):
            break
        parent = parent.parent


def detect_node(
    insp: Inspection, snap: Snapshot, repo_root: Path, app_rel: str, opts: Options
) -> None:
    insp.stack = "node"
    pj = file_evidence("package.json")
    try:
        pkg = json.loads(snap.read_text("package.json"))
    except (json.JSONDecodeError, RecursionError):
        insp.block(UNSUPPORTED, "package.json is not valid JSON", None, pj)
        return
    if not isinstance(pkg, dict):
        insp.block(UNSUPPORTED, "package.json is not a JSON object", None, pj)
        return
    insp.add("stack", "Node.js", pj)

    _workspace_checks(insp, snap, pkg, repo_root, app_rel)

    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    dev = scripts.get("dev")
    dev_ev = file_evidence("package.json", "scripts.dev")
    framework = None
    script_port = None
    if not isinstance(dev, str):
        insp.block(
            UNSUPPORTED,
            "package.json has no `dev` script, so there is no dev server to launch",
            None,
            file_evidence("package.json", "scripts"),
        )
    else:
        try:
            framework, script_port = parse_dev_script(dev)
        except ScriptUnsupported as exc:
            insp.block(
                UNSUPPORTED,
                f"scripts.dev is {clean(dev)!r} and {exc}. Only a simple `vite` or `next dev` "
                "script is supported in the MVP",
                None,
                dev_ev,
            )

    ok_pm = _package_manager(insp, snap, pkg, opts)
    _node_version(insp, snap, pkg, opts)

    if framework:
        insp.framework = framework
        dep_ev = []
        for section in ("dependencies", "devDependencies"):
            deps = pkg.get(section)
            if isinstance(deps, dict) and framework in deps:
                dep_ev.append(file_evidence("package.json", f"{section}.{framework}"))
                break
        insp.add("framework", framework, dev_ev, *dep_ev)
        default_port = VITE_DEFAULT_PORT if framework == "vite" else NEXT_DEFAULT_PORT
        port = script_port or default_port
        insp.container_port = port
        insp.add(
            "container_port",
            str(port),
            dev_ev if script_port else Evidence("default", detail=f"{framework} default port"),
        )
        insp.readiness_path = "/"
        if ok_pm and insp.package_manager:
            if framework == "vite":
                flags = ["--host", "0.0.0.0", "--port", str(port), "--strictPort"]  # noqa: S104
            else:
                flags = ["-H", "0.0.0.0", "-p", str(port)]  # noqa: S104
            # npm needs `--` before script arguments; pnpm passes `--` through literally.
            sep = ["--"] if insp.package_manager == "npm" else []
            insp.start_argv = [insp.package_manager, "run", "dev", *sep, *flags]
            insp.add("start_command", " ".join(insp.start_argv), dev_ev)

    # lifecycle scripts that will execute
    for names, when in ((INSTALL_LIFECYCLE, "install"), (START_LIFECYCLE, "start")):
        for name in names:
            if isinstance(scripts.get(name), str):
                ev = file_evidence("package.json", f"scripts.{name}")
                insp.lifecycle.append(LifecycleScript(name, clean(scripts[name]), when, ev))
    if isinstance(dev, str):
        insp.lifecycle.append(LifecycleScript("dev", clean(dev), "start", dev_ev))

    # services implied by dependencies
    for section in ("dependencies", "devDependencies"):
        deps = pkg.get(section)
        if not isinstance(deps, dict):
            continue
        for dep, what in NODE_SERVICE_DEPS.items():
            if dep in deps:
                insp.services.append(
                    ServiceHint(
                        dep,
                        f"{dep} is a dependency: this app may need {what}",
                        file_evidence("package.json", f"{section}.{dep}"),
                    )
                )
