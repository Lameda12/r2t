"""Turn an acquired source into an Inspection: app root, stack choice, detection."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from .detect.common import (
    collect_env_names,
    env_service_hints,
    existing_compose_services,
    existing_container_files,
    find_candidate_dirs,
    read_small,
)
from .detect.node import detect_node
from .detect.python import detect_python
from .models import (
    NEEDS_INPUT,
    UNSUPPORTED,
    Inspection,
    Options,
    SourceIdentity,
    file_evidence,
)
from .snapshot import MANIFEST_NAMES, Snapshot, collect
from .source import Acquired, validate_subdir

_PY_MANIFESTS = ("requirements.txt", "pyproject.toml")
_PY_OTHER = ("poetry.lock", "uv.lock", "Pipfile", "Pipfile.lock")


def _looks_like_node_app(path: Path) -> bool:
    text = read_small(path / "package.json")
    if text is None:
        return False
    try:
        pkg = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return False
    if not isinstance(pkg, dict):
        return False
    dev = (pkg.get("scripts") or {}).get("dev") if isinstance(pkg.get("scripts"), dict) else None
    if isinstance(dev, str) and re.match(r"^\s*(vite|next)\b", dev):
        return True
    for section in ("dependencies", "devDependencies"):
        deps = pkg.get(section)
        if isinstance(deps, dict) and ({"vite", "next"} & set(deps)):
            return True
    return False


def _looks_like_python_app(path: Path) -> bool:
    for name in _PY_MANIFESTS:
        text = read_small(path / name)
        if text and re.search(r"(?im)(^|[\s\"',\[])fastapi\b", text):
            return True
    return False


def looks_like_app(path: Path) -> str | None:
    if _looks_like_node_app(path):
        return "node (vite/next)"
    if _looks_like_python_app(path):
        return "python (fastapi)"
    return None


def _has_node(path: Path) -> bool:
    return (path / "package.json").is_file()


def _has_python(path: Path) -> bool:
    return any((path / n).is_file() for n in (*_PY_MANIFESTS, *_PY_OTHER))


def _resolve_app_root(repo_root: Path, opts: Options, insp: Inspection) -> tuple[Path, str] | None:
    """Pick the app directory. Returns None (with a blocker recorded) when r2t cannot."""
    if opts.subdir:
        return validate_subdir(repo_root, opts.subdir)
    if looks_like_app(repo_root):
        others = find_candidate_dirs(repo_root, looks_like_app)
        if others:
            listing = ", ".join(c["subdir"] for c in others)
            insp.notes.append(
                f"Other app candidates exist in subdirectories ({listing}); "
                "pass --subdir to choose one instead of the repository root"
            )
        return repo_root, "."
    candidates = find_candidate_dirs(repo_root, looks_like_app)
    if candidates:
        insp.candidates = candidates
        listing = "; ".join(f"{c['subdir']} ({c['kind']}): {c['flag']}" for c in candidates)
        insp.block(
            NEEDS_INPUT,
            "the repository root is not a recognizable app but subdirectories contain "
            f"candidates: {listing}",
            "--subdir PATH",
        )
        return None
    if _has_node(repo_root) or _has_python(repo_root):
        return repo_root, "."
    insp.block(
        UNSUPPORTED,
        "no recognizable app found: expected a Vite or Next.js app (package.json) or a "
        "FastAPI app (requirements.txt or pyproject.toml) at the root or in a subdirectory",
        None,
    )
    return None


def _check_unsafe_symlinks(snap: Snapshot) -> None:
    """A rejected symlink standing in for a manifest or lockfile fails the whole plan."""
    for rel in snap.rejected_symlinks:
        if "/" not in rel and rel in MANIFEST_NAMES:
            snap.has(rel)  # raises InputError with an explanation


def analyze(acq: Acquired, opts: Options) -> tuple[Inspection, Snapshot]:
    identity: SourceIdentity = replace(acq.identity)
    insp = Inspection(source=identity)
    resolved = _resolve_app_root(acq.root, opts, insp)
    if resolved is None:
        snap = collect(acq.root)
        _common(insp, snap)
        identity.snapshot_digest = snap.digest()
        _record_snapshot_notes(insp, snap)
        return insp, snap

    app_abs, app_rel = resolved
    insp.app_root = app_rel
    identity.subdir = None if app_rel == "." else app_rel
    snap = collect(app_abs)
    _check_unsafe_symlinks(snap)

    has_node, has_python = _has_node(app_abs), _has_python(app_abs)
    node_like, py_like = _looks_like_node_app(app_abs), _looks_like_python_app(app_abs)
    if has_node and has_python and node_like == py_like and node_like:
        insp.block(
            UNSUPPORTED,
            "this directory contains both a Node app and a Python app; the MVP supports one "
            "per app root. Move them into separate subdirectories and use --subdir",
            None,
            file_evidence("package.json"),
        )
    elif has_node and (not has_python or node_like or not py_like):
        detect_node(insp, snap, acq.root, app_rel, opts)
    elif has_python:
        detect_python(insp, snap, app_rel, opts)
    else:
        insp.block(
            UNSUPPORTED,
            "no recognizable app in this directory: expected package.json (Vite or Next.js) "
            "or requirements.txt / pyproject.toml (FastAPI)",
            None,
        )
    _common(insp, snap)
    identity.snapshot_digest = snap.digest()
    _record_snapshot_notes(insp, snap)
    return insp, snap


def _common(insp: Inspection, snap: Snapshot) -> None:
    insp.env_names = collect_env_names(snap)
    insp.services.extend(env_service_hints(insp.env_names))
    insp.services.extend(existing_compose_services(snap))
    insp.container_files = existing_container_files(snap)


def _record_snapshot_notes(insp: Inspection, snap: Snapshot) -> None:
    insp.skipped_symlinks = [
        {"path": rel, "reason": reason} for rel, reason in sorted(snap.rejected_symlinks.items())
    ]
    insp.excluded = [{"path": rel, "category": cat} for rel, cat in snap.excluded]
    if any(rel.rsplit("/", 1)[-1] in {".npmrc", ".pypirc", ".netrc"} for rel, _ in snap.excluded):
        insp.notes.append(
            "Registry or login config (.npmrc, .pypirc or .netrc) was excluded from the "
            "snapshot; private packages that need it will fail to install"
        )
    for rel, size in snap.skipped_large:
        insp.notes.append(f"Skipped {rel}: {size // 1024 // 1024} MB exceeds the 50 MB file limit")
    for rel in snap.skipped_special:
        insp.notes.append(f"Skipped {rel}: not a regular file")
