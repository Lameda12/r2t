"""Typed models. Plain dataclasses with explicit to_dict() for JSON."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import SCHEMA_VERSION, __version__

SUPPORTED = "supported"
NEEDS_INPUT = "needs_input"
UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Evidence:
    """Where a detection came from. `kind` is "file", "user override" or "default"."""

    kind: str
    file: str | None = None
    locator: str | None = None  # key path (package.json#scripts.dev) or line number
    detail: str | None = None

    def ref(self) -> str:
        if self.kind == "user override":
            return f"user override ({self.detail})" if self.detail else "user override"
        if self.kind == "default":
            return "r2t default"
        if self.file is None:
            return self.detail or "unknown"
        if self.locator is None:
            return self.file
        sep = ":" if self.locator.isdigit() else "#"
        return f"{self.file}{sep}{self.locator}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "file": self.file,
            "locator": self.locator,
            "detail": self.detail,
            "ref": self.ref(),
        }


def file_evidence(file: str, locator: str | int | None = None, detail: str | None = None):
    return Evidence("file", file, None if locator is None else str(locator), detail)


def override_evidence(flag: str) -> Evidence:
    return Evidence("user override", detail=flag)


@dataclass
class Detection:
    key: str
    value: str
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "evidence": [e.to_dict() for e in self.evidence],
        }


@dataclass
class Blocker:
    """Why a target is not `supported`. `flag` names the option that resolves a needs_input."""

    status: str  # NEEDS_INPUT or UNSUPPORTED
    reason: str
    flag: str | None = None
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "flag": self.flag,
            "evidence": [e.to_dict() for e in self.evidence],
        }


@dataclass
class LifecycleScript:
    name: str
    command: str
    when: str  # "install" or "start"
    evidence: Evidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "when": self.when,
            "evidence": self.evidence.to_dict(),
        }


@dataclass
class EnvName:
    name: str
    file: str
    line: int

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "file": self.file, "line": self.line}


@dataclass
class ServiceHint:
    what: str
    why: str
    evidence: Evidence

    def to_dict(self) -> dict[str, Any]:
        return {"what": self.what, "why": self.why, "evidence": self.evidence.to_dict()}


@dataclass
class SourceIdentity:
    kind: str  # "remote" or "local"
    snapshot_digest: str | None = None
    # remote
    url: str | None = None
    requested_ref: str | None = None
    resolved_commit: str | None = None
    # local
    path: str | None = None
    git_head: str | None = None
    dirty: bool | None = None
    git_note: str | None = None
    subdir: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "requested_ref": self.requested_ref,
            "resolved_commit": self.resolved_commit,
            "path": self.path,
            "git_head": self.git_head,
            "dirty": self.dirty,
            "git_note": self.git_note,
            "subdir": self.subdir,
            "snapshot_digest": self.snapshot_digest,
        }


@dataclass
class Inspection:
    """Everything `inspect` learned. Paths in evidence are relative to the app root."""

    source: SourceIdentity
    app_root: str = "."  # relative to the repository root
    stack: str | None = None  # "node" or "python"
    framework: str | None = None  # "vite", "next", "fastapi"
    detections: list[Detection] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # runtime and tooling
    runtime_name: str | None = None
    runtime_constraints: list[str] = field(default_factory=list)
    base_image: str | None = None
    package_manager: str | None = None
    package_manager_version: str | None = None
    lockfile: str | None = None
    lock_status: str | None = None
    install_argv: list[str] = field(default_factory=list)
    start_argv: list[str] = field(default_factory=list)
    container_port: int | None = None
    readiness_path: str = "/"
    lifecycle: list[LifecycleScript] = field(default_factory=list)
    env_names: list[EnvName] = field(default_factory=list)
    services: list[ServiceHint] = field(default_factory=list)
    container_files: list[Evidence] = field(default_factory=list)
    candidates: list[dict[str, str]] = field(default_factory=list)
    skipped_symlinks: list[dict[str, str]] = field(default_factory=list)
    excluded: list[dict[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if any(b.status == UNSUPPORTED for b in self.blockers):
            return UNSUPPORTED
        if any(b.status == NEEDS_INPUT for b in self.blockers):
            return NEEDS_INPUT
        return SUPPORTED

    def add(self, key: str, value: str, *evidence: Evidence) -> None:
        self.detections.append(Detection(key, value, list(evidence)))

    def block(self, status: str, reason: str, flag: str | None = None, *evidence: Evidence) -> None:
        self.blockers.append(Blocker(status, reason, flag, list(evidence)))

    def detection(self, key: str) -> Detection | None:
        return next((d for d in self.detections if d.key == key), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "r2t_version": __version__,
            "status": self.status,
            "source": self.source.to_dict(),
            "app_root": self.app_root,
            "stack": self.stack,
            "framework": self.framework,
            "detections": [d.to_dict() for d in self.detections],
            "blockers": [b.to_dict() for b in self.blockers],
            "assumptions": list(self.assumptions),
            "notes": list(self.notes),
            "runtime": {
                "name": self.runtime_name,
                "constraints": list(self.runtime_constraints),
                "base_image": self.base_image,
            },
            "package_manager": {
                "name": self.package_manager,
                "version": self.package_manager_version,
                "lockfile": self.lockfile,
                "lock_status": self.lock_status,
            },
            "commands": {"install": list(self.install_argv), "start": list(self.start_argv)},
            "container_port": self.container_port,
            "readiness_path": self.readiness_path,
            "lifecycle_scripts": [s.to_dict() for s in self.lifecycle],
            "environment_variable_names": [e.to_dict() for e in self.env_names],
            "external_services": [s.to_dict() for s in self.services],
            "existing_container_files": [e.to_dict() for e in self.container_files],
            "candidates": list(self.candidates),
            "skipped_symlinks": list(self.skipped_symlinks),
            "excluded": list(self.excluded),
        }
