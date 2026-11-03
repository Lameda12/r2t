"""Python FastAPI detection. Source is parsed with `ast`; nothing is ever imported or executed."""

from __future__ import annotations

import ast
import re
import tomllib
from dataclasses import dataclass

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

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
from .common import PYTHON_SERVICE_DEPS

PY_SUPPORTED = (11, 12, 13)  # python:3.X-slim-bookworm images
PY_DEFAULT = 12
CONTAINER_PORT = 8000
RUNTIME_FLAG = "--runtime-version 3.X"
ENTRY_FLAG = "--entry MODULE:ATTR"

UNSUPPORTED_TOOL_FILES = {
    "poetry.lock": "Poetry",
    "uv.lock": "uv",
    "Pipfile": "Pipenv",
    "Pipfile.lock": "Pipenv",
}
_SKIP_DIRS = {"tests", "test", "testing", "__tests__", "e2e"}
_FACTORY_NAME = re.compile(r"^(create|make|build|get)_?app$")
_ENTRY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):([A-Za-z_]\w*)$")
_MAX_SOURCE = 1_000_000


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class Dep:
    name: str | None
    extras: frozenset[str]
    pinned: bool
    hashed: bool
    file: str
    line: int


def _parse_dep(text: str, file: str, line: int, hashed: bool) -> Dep:
    try:
        req = Requirement(text)
    except InvalidRequirement:
        return Dep(None, frozenset(), False, hashed, file, line)
    pinned = req.url is None and any(
        s.operator in {"==", "==="} and "*" not in s.version for s in req.specifier
    )
    return Dep(norm(req.name), frozenset(e.lower() for e in req.extras), pinned, hashed, file, line)


def parse_requirements(
    snap: Snapshot, rel: str, insp: Inspection, _seen: set[str] | None = None
) -> list[Dep]:
    seen = _seen if _seen is not None else set()
    if rel in seen or not snap.has_file(rel):
        return []
    seen.add(rel)
    deps: list[Dep] = []
    logical: list[tuple[int, str]] = []
    buf, start = "", 0
    for lineno, raw in enumerate(snap.read_text(rel).splitlines(), start=1):
        if not buf:
            start = lineno
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        logical.append((start, buf + raw))
        buf = ""
    if buf:
        logical.append((start, buf))
    base = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
    for lineno, line in logical:
        line = re.sub(r"(^|\s)#.*$", "", line).strip()
        if not line:
            continue
        m = re.match(r"^(?:-r|--requirement)[=\s]+(\S+)$", line)
        if m:
            deps += parse_requirements(snap, base + m.group(1), insp, seen)
            continue
        if re.match(r"^(--index-url|--extra-index-url|-i|--find-links|-f)\b", line):
            insp.notes.append(
                f"{rel}:{lineno} configures a custom package index or link source; "
                "dependencies may come from outside PyPI"
            )
            continue
        if line.startswith("-e") or line.startswith("--editable"):
            deps.append(Dep(None, frozenset(), False, False, rel, lineno))
            continue
        if line.startswith("-"):
            continue
        hashed = "--hash=" in line
        text = re.split(r"\s--", line, maxsplit=1)[0].strip()
        deps.append(_parse_dep(text, rel, lineno, hashed))
    return deps


def _lock_status(deps: list[Dep]) -> str:
    if deps and all(d.pinned for d in deps):
        if all(d.hashed for d in deps):
            return "locked"
        return "direct pins only (transitive dependencies unlocked)"
    return "unlocked"


def _minors_from_specifier(text: str) -> set[int]:
    spec = SpecifierSet(text)
    return {
        m
        for m in range(8, 16)
        if any(spec.contains(Version(f"3.{m}.{p}"), prereleases=True) for p in range(0, 30))
    }


def _minors_from_version_file(text: str) -> set[int]:
    m = re.fullmatch(r"(?:cpython-|python-)?(\d+)(?:\.(\d+))?(?:\.(?:\d+|x|\*))?", text.strip())
    if not m:
        raise ValueError(f"cannot parse {text!r}")
    major, minor = int(m.group(1)), m.group(2)
    if major != 3:
        return set()
    return {int(minor)} if minor is not None else set(range(8, 16))


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def _python_version(insp: Inspection, snap: Snapshot, project: dict, opts: Options) -> None:
    insp.runtime_name = "python"
    if opts.runtime_version is not None:
        if not re.fullmatch(r"3\.\d{1,2}", opts.runtime_version):
            raise InputError(
                f"--runtime-version for Python must look like 3.12, got {opts.runtime_version!r}"
            )
        insp.runtime_constraints.append(f"--runtime-version {opts.runtime_version}")
        insp.base_image = f"python:{opts.runtime_version}-slim-bookworm"
        insp.add(
            "runtime",
            f"Python {opts.runtime_version}",
            override_evidence(f"--runtime-version {opts.runtime_version}"),
        )
        if int(opts.runtime_version.split(".")[1]) not in PY_SUPPORTED:
            insp.assumptions.append(
                f"Python {opts.runtime_version} is outside r2t's tested set "
                f"3.{PY_SUPPORTED[0]}-3.{PY_SUPPORTED[-1]}; used because you asked for it"
            )
        return

    sources: list[tuple[str, str, Evidence, bool]] = []
    rp = project.get("requires-python")
    if isinstance(rp, str):
        sources.append(
            (
                "pyproject.toml#project.requires-python",
                rp,
                file_evidence("pyproject.toml", "project.requires-python"),
                True,
            )
        )
    for name in (".python-version", "runtime.txt"):
        if snap.has_file(name):
            sources.append((name, _first_line(snap.read_text(name)), file_evidence(name), False))
    if not sources:
        insp.base_image = f"python:3.{PY_DEFAULT}-slim-bookworm"
        insp.assumptions.append(
            "No Python version is declared (requires-python, .python-version, runtime.txt); "
            f"using the default 3.{PY_DEFAULT}"
        )
        insp.add("runtime", f"Python 3.{PY_DEFAULT}", Evidence("default", detail="r2t default"))
        return
    allowed: set[int] | None = None
    evidences = []
    for label, text, ev, is_spec in sources:
        insp.runtime_constraints.append(f"{label}: {clean(text)}")
        evidences.append(ev)
        try:
            minors = _minors_from_specifier(text) if is_spec else _minors_from_version_file(text)
        except (InvalidSpecifier, ValueError) as exc:
            insp.block(
                NEEDS_INPUT,
                f"cannot interpret the Python version in {label} ({clean(text)!r}): {exc}",
                RUNTIME_FLAG,
                ev,
            )
            return
        allowed = minors if allowed is None else allowed & minors
    assert allowed is not None  # noqa: S101
    found = "; ".join(insp.runtime_constraints)
    if not allowed:
        insp.block(
            NEEDS_INPUT, f"the Python version sources conflict ({found})", RUNTIME_FLAG, *evidences
        )
        return
    choices = sorted(allowed & set(PY_SUPPORTED))
    if not choices:
        insp.block(
            NEEDS_INPUT,
            f"no supported Python (3.{PY_SUPPORTED[0]}-3.{PY_SUPPORTED[-1]}) satisfies {found}",
            RUNTIME_FLAG,
            *evidences,
        )
        return
    minor = choices[-1]
    insp.base_image = f"python:3.{minor}-slim-bookworm"
    insp.add("runtime", f"Python 3.{minor}", *evidences)
    if len(choices) > 1:
        insp.assumptions.append(
            f"Python 3.{minor} is the highest supported version that satisfies the declared "
            "constraints"
        )


# ---- entry point ---------------------------------------------------------------------------


@dataclass
class AppRef:
    file: str
    attr: str
    line: int
    kind: str  # "instance" or "factory"


def _is_fastapi_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Name):
        return f.id == "FastAPI"
    return isinstance(f, ast.Attribute) and f.attr == "FastAPI"


def _scan_module(source: str, file: str) -> tuple[list[AppRef], list[AppRef]]:
    """Return (FastAPI instances, factory-like names) defined at module level."""
    try:
        tree = ast.parse(source, filename=file)
    except (SyntaxError, ValueError, RecursionError):
        return [], []
    apps: list[AppRef] = []
    factories: list[AppRef] = []
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if _FACTORY_NAME.match(node.name):
                factories.append(AppRef(file, node.name, node.lineno, "factory"))
            continue
        for target in targets:
            if not isinstance(target, ast.Name) or value is None:
                continue
            if _is_fastapi_call(value):
                apps.append(AppRef(file, target.id, node.lineno, "instance"))
            elif (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and _FACTORY_NAME.match(value.func.id)
            ):
                factories.append(AppRef(file, target.id, node.lineno, "factory-call"))
    return apps, factories


def _module_for(snap: Snapshot, rel: str) -> tuple[str, str | None]:
    """Importable module path for a file, and the --app-dir to use (src layout)."""
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if parts[0] == "src" and len(parts) > 1 and not snap.has("src/__init__.py"):
        return ".".join(parts[1:]), "src"
    return ".".join(parts), None


def _candidate_files(snap: Snapshot) -> list[str]:
    out = []
    for rel in snap.files(".py"):
        parts = rel.split("/")
        name = parts[-1]
        if name not in {"main.py", "app.py"} or len(parts) > 4:
            continue
        if any(p in _SKIP_DIRS for p in parts[:-1]):
            continue
        out.append(rel)
    # an unsafe symlink standing in for the entry module must fail the plan
    for rel in snap.rejected_symlinks:
        parts = rel.split("/")
        if parts[-1] in {"main.py", "app.py"} and len(parts) <= 4:
            snap.has(rel)
    return out


def _resolve_entry_override(snap: Snapshot, entry: str):
    m = _ENTRY_RE.match(entry)
    if not m:
        raise InputError(f"--entry must look like MODULE:ATTR (e.g. app.main:app), got {entry!r}")
    module, attr = m.group(1), m.group(2)
    base = module.replace(".", "/")
    for rel in (f"{base}.py", f"{base}/__init__.py", f"src/{base}.py", f"src/{base}/__init__.py"):
        if snap.has_file(rel):
            break
    else:
        raise InputError(f"--entry {entry!r}: no module file for {module!r} in the app")
    source = snap.read_text(rel)
    apps, factories = _scan_module(source, rel)
    kind = next((r.kind for r in apps + factories if r.attr == attr), None)
    if kind == "factory-call":  # `app = create_app()` is already an instance
        kind = "instance"
    if kind is None:
        kind = _plain_definition_kind(source, attr)
    if kind is None:
        raise InputError(f"--entry {entry!r}: {attr!r} is not defined at module level in {rel}")
    module, app_dir = _module_for(snap, rel)
    return rel, module, attr, kind, app_dir


def _plain_definition_kind(source: str, attr: str) -> str | None:
    """Kind of a module-level name that is not a FastAPI(...) call: def, assignment or import."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == attr:
            return "factory"
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == attr for t in node.targets
        ):
            return "instance"
        if isinstance(node, ast.ImportFrom) and any(
            (a.asname or a.name) == attr for a in node.names
        ):
            return "instance"
    return None


def _entry(
    insp: Inspection, snap: Snapshot, opts: Options
) -> tuple[str, str, str | None, bool] | None:
    """Return (module, attr, app_dir, is_factory) or record a blocker and return None."""
    if opts.entry:
        rel, module, attr, kind, app_dir = _resolve_entry_override(snap, opts.entry)
        ev = override_evidence(f"--entry {opts.entry}")
        insp.add("entry", f"{module}:{attr}", ev, file_evidence(rel))
        return module, attr, app_dir, kind == "factory"

    apps: list[AppRef] = []
    factories: list[AppRef] = []
    for rel in _candidate_files(snap):
        if snap.entries[rel].size > _MAX_SOURCE:
            continue
        a, f = _scan_module(snap.read_text(rel), rel)
        apps += a
        factories += f
    if len(apps) == 1:
        ref = apps[0]
        module, app_dir = _module_for(snap, ref.file)
        insp.add("entry", f"{module}:{ref.attr}", file_evidence(ref.file, ref.line))
        return module, ref.attr, app_dir, False
    if len(apps) > 1:
        for ref in apps:
            module, _ = _module_for(snap, ref.file)
            insp.candidates.append(
                {
                    "entry": f"{module}:{ref.attr}",
                    "file": f"{ref.file}:{ref.line}",
                    "flag": f"--entry {module}:{ref.attr}",
                }
            )
        listing = ", ".join(c["flag"] for c in insp.candidates)
        insp.block(
            NEEDS_INPUT,
            f"found {len(apps)} FastAPI apps ({listing}); r2t cannot pick one",
            ENTRY_FLAG,
            *[file_evidence(r.file, r.line) for r in apps],
        )
        return None
    if factories:
        ref = factories[0]
        module, _ = _module_for(snap, ref.file)
        insp.block(
            NEEDS_INPUT,
            f"{ref.file}:{ref.line} looks like an app factory ({ref.attr}); no module-level "
            f"`app = FastAPI(...)` was found. Pass the importable app, e.g. "
            f"--entry {module}:{ref.attr}",
            ENTRY_FLAG,
            file_evidence(ref.file, ref.line),
        )
        return None
    insp.block(
        NEEDS_INPUT,
        "no module-level `NAME = FastAPI(...)` was found in main.py, app.py, app/main.py, "
        "src/*/main.py or api/main.py",
        ENTRY_FLAG,
    )
    return None


def detect_python(insp: Inspection, snap: Snapshot, app_rel: str, opts: Options) -> None:
    insp.stack = "python"
    for name, tool in UNSUPPORTED_TOOL_FILES.items():
        if snap.has_file(name):
            insp.block(
                UNSUPPORTED,
                f"{name} found: {tool} projects are not supported in the MVP "
                "(requirements.txt or a PEP 621 pyproject.toml only)",
                None,
                file_evidence(name),
            )
            return

    project: dict = {}
    pyproject: dict = {}
    if snap.has_file("pyproject.toml"):
        try:
            pyproject = tomllib.loads(snap.read_text("pyproject.toml"))
        except tomllib.TOMLDecodeError as exc:
            insp.block(
                UNSUPPORTED,
                f"pyproject.toml is not valid TOML ({exc})",
                None,
                file_evidence("pyproject.toml"),
            )
            return
        project = pyproject.get("project") if isinstance(pyproject.get("project"), dict) else {}
        if "tool" in pyproject and "poetry" in pyproject.get("tool", {}) and not project:
            insp.block(
                UNSUPPORTED,
                "pyproject.toml uses [tool.poetry] without a PEP 621 [project] table: Poetry "
                "projects are not supported in the MVP",
                None,
                file_evidence("pyproject.toml", "tool.poetry"),
            )
            return

    has_req = snap.has_file("requirements.txt")
    has_build = isinstance(pyproject.get("build-system"), dict)
    deps: list[Dep] = []
    dep_file: str
    if has_req:
        dep_file = "requirements.txt"
        insp.install_argv = ["pip", "install", "-r", "requirements.txt"]
        deps = parse_requirements(snap, "requirements.txt", insp)
        if project and has_build:
            insp.assumptions.append(
                "Both requirements.txt and pyproject.toml are present; using requirements.txt"
            )
    elif project and has_build:
        dep_file = "pyproject.toml"
        insp.install_argv = ["pip", "install", "."]
        for i, text in enumerate(project.get("dependencies") or [], start=1):
            if isinstance(text, str):
                deps.append(_parse_dep(text, "pyproject.toml", i, False))
    else:
        reason = (
            "pyproject.toml has a [project] table but no [build-system], so `pip install .` "
            "is not reliable"
            if project
            else "no requirements.txt or installable pyproject.toml ([project] with "
            "[build-system]) was found"
        )
        insp.block(UNSUPPORTED, reason, None, file_evidence("pyproject.toml") if project else None)  # type: ignore[arg-type]
        return
    insp.add("stack", "Python", file_evidence(dep_file))
    insp.add("install", " ".join(insp.install_argv), file_evidence(dep_file))

    insp.lockfile = None
    insp.lock_status = _lock_status(deps)
    insp.add("lock_status", insp.lock_status, file_evidence(dep_file))

    names = {d.name for d in deps if d.name}
    fastapi = next((d for d in deps if d.name == "fastapi"), None)
    if fastapi is None:
        insp.block(
            UNSUPPORTED,
            f"fastapi is not declared in {dep_file}; only FastAPI apps are supported for Python",
            None,
            file_evidence(dep_file),
        )
    else:
        insp.framework = "fastapi"
        insp.add("framework", "fastapi", file_evidence(fastapi.file, fastapi.line))
        has_server = "uvicorn" in names or bool(fastapi.extras & {"standard", "all"})
        if not has_server:
            insp.block(
                UNSUPPORTED,
                "no ASGI server declared: add uvicorn (or fastapi[standard]) to the dependencies",
                None,
                file_evidence(dep_file),
            )

    _python_version(insp, snap, project, opts)
    if fastapi is None:
        _services(insp, deps)
        return

    entry = _entry(insp, snap, opts)
    insp.container_port = CONTAINER_PORT
    insp.readiness_path = "/"
    insp.add(
        "container_port", str(CONTAINER_PORT), Evidence("default", detail="r2t default for uvicorn")
    )
    if entry:
        module, attr, app_dir, is_factory = entry
        argv = ["uvicorn", f"{module}:{attr}", "--host", "0.0.0.0", "--port", str(CONTAINER_PORT)]  # noqa: S104
        if app_dir:
            argv += ["--app-dir", app_dir]
        if is_factory:
            argv.append("--factory")
        insp.start_argv = argv
        insp.add("start_command", " ".join(argv), insp.detection("entry").evidence[0])  # type: ignore[union-attr]
        insp.lifecycle.append(
            LifecycleScript(
                "import",
                f"uvicorn imports {module}, running its module-level code",
                "start",
                insp.detection("entry").evidence[0],  # type: ignore[union-attr]
            )
        )
    if dep_file == "pyproject.toml":
        backend = (pyproject.get("build-system") or {}).get("build-backend", "setuptools (default)")
        insp.lifecycle.append(
            LifecycleScript(
                "build-backend",
                f"pip builds this project with {clean(backend)}, which runs during install",
                "install",
                file_evidence("pyproject.toml", "build-system"),
            )
        )
    _services(insp, deps)


def _services(insp: Inspection, deps: list[Dep]) -> None:
    for dep in deps:
        if dep.name in PYTHON_SERVICE_DEPS:
            insp.services.append(
                ServiceHint(
                    dep.name,
                    f"{dep.name} is a dependency: this app may need "
                    f"{PYTHON_SERVICE_DEPS[dep.name]}",
                    file_evidence(dep.file, dep.line),
                )
            )
