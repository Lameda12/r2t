"""Filtered snapshot of an app directory, with an authoritative content digest."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .errors import InputError

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024

ENV_EXAMPLE_NAMES = (".env.example", ".env.sample", ".env.template", ".env.dist")

EXCLUDED_DIR_NAMES = {
    ".git": "version control",
    "node_modules": "installed dependencies",
    ".venv": "virtual environment",
    "venv": "virtual environment",
    "__pycache__": "cache",
    ".pytest_cache": "cache",
    ".mypy_cache": "cache",
    ".ruff_cache": "cache",
    ".cache": "cache",
    ".turbo": "cache",
    ".ssh": "credentials",
    ".aws": "credentials",
}
ROOT_ONLY_DIR_NAMES = {
    ".next": "build output",
    "dist": "build output",
    "build": "build output",
    "coverage": "build output",
}
_CREDENTIAL_PATTERNS = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "credentials*.json",
    "*service-account*.json",
)
_REGISTRY_NAMES = (".npmrc", ".pypirc", ".netrc")

# Names that must never be reached through an unsafe symlink.
MANIFEST_NAMES = frozenset(
    {
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "requirements.txt",
        "pyproject.toml",
        "poetry.lock",
        "uv.lock",
        "Pipfile",
        "Pipfile.lock",
        ".nvmrc",
        ".node-version",
        ".python-version",
        "runtime.txt",
    }
)


def file_exclusion(rel: str) -> str | None:
    """Return the exclusion category for a file path, or None to keep it."""
    name = rel.rsplit("/", 1)[-1]
    lower = name.lower()
    if (
        lower == ".env"
        or lower == ".envrc"
        or (lower.startswith(".env.") and lower not in ENV_EXAMPLE_NAMES)
    ):
        return "environment file"
    if lower in _REGISTRY_NAMES:
        return "registry or login config"
    if any(fnmatch.fnmatch(lower, pat) for pat in _CREDENTIAL_PATTERNS):
        return "key or credential file"
    if rel.endswith(".docker/config.json") or rel == ".docker/config.json":
        return "registry or login config"
    return None


def dir_exclusion(name: str, rel: str, path: Path) -> str | None:
    if name in EXCLUDED_DIR_NAMES:
        return EXCLUDED_DIR_NAMES[name]
    if "/" not in rel and name in ROOT_ONLY_DIR_NAMES:
        return ROOT_ONLY_DIR_NAMES[name]
    if (path / "pyvenv.cfg").is_file():
        return "virtual environment"
    return None


def path_excluded(rel: str) -> str | None:
    """Is any component of this relative path excluded? Used for symlink targets."""
    parts = rel.split("/")
    for i, part in enumerate(parts[:-1]):
        if part in EXCLUDED_DIR_NAMES:
            return EXCLUDED_DIR_NAMES[part]
        if i == 0 and part in ROOT_ONLY_DIR_NAMES:
            return ROOT_ONLY_DIR_NAMES[part]
    last = parts[-1]
    if last in EXCLUDED_DIR_NAMES:
        return EXCLUDED_DIR_NAMES[last]
    if len(parts) == 1 and last in ROOT_ONLY_DIR_NAMES:
        return ROOT_ONLY_DIR_NAMES[last]
    return file_exclusion(rel)


@dataclass(frozen=True)
class Entry:
    rel: str
    kind: str  # "file" or "symlink"
    mode: str  # git-style: 100644, 100755, 120000
    sha256: str
    size: int
    link: str | None = None


@dataclass
class Snapshot:
    root: Path
    entries: dict[str, Entry] = field(default_factory=dict)
    rejected_symlinks: dict[str, str] = field(default_factory=dict)  # rel -> reason
    excluded: list[tuple[str, str]] = field(default_factory=list)  # (rel, category)
    skipped_large: list[tuple[str, int]] = field(default_factory=list)
    skipped_special: list[str] = field(default_factory=list)
    total_bytes: int = 0

    # ---- reading (detection only ever reads through these) ----
    def _guard(self, rel: str) -> None:
        if rel in self.rejected_symlinks:
            raise InputError(
                f"{rel} is a symlink that cannot be followed ({self.rejected_symlinks[rel]}) "
                "but r2t needs to read it (manifest, lockfile or entry module)",
                "replace the symlink with a regular file, or choose a different --subdir",
            )

    def has(self, rel: str) -> bool:
        self._guard(rel)
        return rel in self.entries

    def has_file(self, rel: str) -> bool:
        self._guard(rel)
        entry = self.entries.get(rel)
        return entry is not None and (self.root / rel).is_file()

    def read_text(self, rel: str) -> str:
        self._guard(rel)
        entry = self.entries.get(rel)
        if entry is None:
            raise FileNotFoundError(rel)
        path = self.root / rel
        if path.stat().st_size > MAX_TEXT_BYTES:
            raise InputError(f"{rel} is larger than {MAX_TEXT_BYTES // 1024 // 1024} MB")
        return path.read_text(encoding="utf-8", errors="replace")

    def files(self, suffix: str | None = None) -> list[str]:
        return sorted(
            rel
            for rel, e in self.entries.items()
            if e.kind == "file" and (suffix is None or rel.endswith(suffix))
        )

    def digest(self) -> str:
        return digest_entries(self.entries.values())


def digest_entries(entries) -> str:
    """sha256 over the sorted (relative path, mode, content sha256) triples."""
    h = hashlib.sha256()
    for e in sorted(entries, key=lambda e: e.rel):
        h.update(f"{e.rel}\0{e.mode}\0{e.sha256}\n".encode())
    return h.hexdigest()


def _hash_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _classify_symlink(path: Path, rel: str, root_real: Path) -> tuple[str | None, str]:
    """Return (rejection reason or None, link text)."""
    link = os.readlink(path)
    if os.path.isabs(link):
        return "absolute target", link
    resolved = Path(os.path.realpath(path))
    if resolved != root_real and root_real not in resolved.parents:
        return "target escapes the source root", link
    if not resolved.exists():
        return "dangling link", link
    target_rel = resolved.relative_to(root_real).as_posix() if resolved != root_real else "."
    if target_rel != "." and path_excluded(target_rel):
        return "target is in an excluded path", link
    return None, link


def collect(root: Path) -> Snapshot:
    """Walk root (never following symlinks) and describe what a snapshot would contain."""
    root_real = root.resolve()
    snap = Snapshot(root=root_real)
    for dirpath, dirnames, filenames in os.walk(root_real, followlinks=False):
        here = Path(dirpath)
        rel_dir = here.relative_to(root_real).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        keep: list[str] = []
        for name in sorted(dirnames):
            path = here / name
            rel = prefix + name
            if path.is_symlink():
                _add_symlink(snap, path, rel, root_real)
                continue
            category = dir_exclusion(name, rel, path)
            if category:
                snap.excluded.append((rel + "/", category))
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            path = here / name
            rel = prefix + name
            if path.is_symlink():
                _add_symlink(snap, path, rel, root_real)
                continue
            category = file_exclusion(rel)
            if category:
                snap.excluded.append((rel, category))
                continue
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode):
                snap.skipped_special.append(rel)
                continue
            if st.st_size > MAX_FILE_BYTES:
                snap.skipped_large.append((rel, st.st_size))
                continue
            snap.total_bytes += st.st_size
            if snap.total_bytes > MAX_TOTAL_BYTES:
                raise InputError(
                    f"snapshot exceeds {MAX_TOTAL_BYTES // 1024 // 1024} MB",
                    "choose a smaller app with --subdir",
                )
            sha, size = _hash_file(path)
            mode = "100755" if st.st_mode & 0o111 else "100644"
            snap.entries[rel] = Entry(rel, "file", mode, sha, size)
    return snap


def _add_symlink(snap: Snapshot, path: Path, rel: str, root_real: Path) -> None:
    reason, link = _classify_symlink(path, rel, root_real)
    if reason:
        snap.rejected_symlinks[rel] = reason
        return
    sha = hashlib.sha256(link.encode()).hexdigest()
    snap.entries[rel] = Entry(rel, "symlink", "120000", sha, len(link), link=link)


def materialize(snap: Snapshot, dest: Path) -> None:
    """Copy the snapshot into dest and verify the copy matches the digest."""
    dest.mkdir(parents=True, exist_ok=True)
    for rel, entry in sorted(snap.entries.items()):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "symlink":
            assert entry.link is not None  # noqa: S101
            os.symlink(entry.link, target)
            continue
        src = snap.root / rel
        fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as fin, open(target, "wb") as fout:
            while chunk := fin.read(1024 * 1024):
                fout.write(chunk)
        target.chmod(0o755 if entry.mode == "100755" else 0o644)
    copied = []
    for rel, entry in snap.entries.items():
        if entry.kind == "symlink":
            copied.append(entry)
            continue
        sha, size = _hash_file(dest / rel)
        copied.append(Entry(rel, "file", entry.mode, sha, size))
    if digest_entries(copied) != snap.digest():
        raise InputError("source files changed while the snapshot was being copied; try again")
