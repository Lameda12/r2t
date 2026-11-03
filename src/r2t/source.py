"""Source acquisition and validation. Git is only ever run hardened."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import InputError
from .models import SourceIdentity
from .runner import Runner, RunResult, base_env

GIT_TIMEOUT = 300
GIT_QUICK_TIMEOUT = 30

# Applied to every git invocation that touches repository content.
GIT_HARDENING = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.file.allow=never",
)

_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")


@dataclass
class Acquired:
    root: Path
    identity: SourceIdentity


def git_env() -> dict[str, str]:
    return base_env(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_ALLOW_PROTOCOL": "https",
            "GIT_OPTIONAL_LOCKS": "0",
        },
        network=True,
    )


def looks_like_url(source: str) -> bool:
    return "://" in source or source.startswith("git@")


def parse_github_url(url: str) -> tuple[str, str]:
    """Return (owner, repo). Only https://github.com/OWNER/REPO[.git] is accepted."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise InputError(f"invalid URL: {exc}") from exc
    if parts.scheme != "https":
        raise InputError(
            "only https:// GitHub URLs are supported", "e.g. https://github.com/OWNER/REPO"
        )
    if parts.username is not None or parts.password is not None or port is not None:
        raise InputError("URL must not contain credentials or a port")
    if parts.hostname != "github.com" or parts.netloc != "github.com":
        raise InputError("only github.com URLs are supported")
    if parts.query or parts.fragment:
        raise InputError("URL must not contain a query string or fragment")
    segments = parts.path.split("/")
    if len(segments) != 3 or segments[0] != "":
        raise InputError("URL must look like https://github.com/OWNER/REPO")
    owner, repo = segments[1], segments[2]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not owner or not repo or not _SEGMENT.match(owner) or not _SEGMENT.match(repo):
        raise InputError("URL must look like https://github.com/OWNER/REPO")
    if owner in {".", ".."} or repo in {".", ".."}:
        raise InputError("URL must look like https://github.com/OWNER/REPO")
    return owner, repo


def is_commit_sha(ref: str) -> bool:
    return bool(_SHA.match(ref))


def validate_ref(ref: str, runner: Runner) -> None:
    if not ref or ref.startswith("-"):
        raise InputError(f"invalid ref {ref!r}: must not be empty or start with '-'")
    if is_commit_sha(ref):
        return
    result = runner(
        ["git", "check-ref-format", "--allow-onelevel", ref],
        env=git_env(),
        timeout=GIT_QUICK_TIMEOUT,
    )
    if not result.ok:
        raise InputError(f"invalid ref {ref!r}: not a valid Git ref name")


def validate_subdir(root: Path, subdir: str) -> tuple[Path, str]:
    """Return (absolute path, POSIX relative path). The result is a directory inside root."""
    if not subdir or subdir.startswith("-"):
        raise InputError(f"invalid --subdir {subdir!r}: must not be empty or start with '-'")
    if "\x00" in subdir:
        raise InputError("invalid --subdir: contains a NUL byte")
    pure = PurePosixPath(subdir.replace("\\", "/"))
    if pure.is_absolute():
        raise InputError(f"--subdir must be a relative path, got {subdir!r}")
    if ".." in pure.parts:
        raise InputError(f"--subdir must not contain '..', got {subdir!r}")
    root_real = root.resolve()
    target = (root_real / pure).resolve()
    if target != root_real and root_real not in target.parents:
        raise InputError(f"--subdir {subdir!r} resolves outside the repository")
    if not target.is_dir():
        raise InputError(f"--subdir {subdir!r} is not a directory in the repository")
    rel = target.relative_to(root_real).as_posix()
    return target, rel


def _git_failed(action: str, result: RunResult) -> InputError:
    detail = (result.stderr or result.stdout).strip().splitlines()
    tail = detail[-1] if detail else "no output"
    if result.timed_out:
        tail = "timed out"
    return InputError(f"git {action} failed: {tail}")


def _clone(url: str, ref: str | None, dest: Path, runner: Runner) -> str:
    """Shallow-clone into dest and return the resolved full commit SHA."""
    env = git_env()
    if ref and is_commit_sha(ref):
        steps = [
            ("init", ["git", *GIT_HARDENING, "init", "--quiet", "--", str(dest)]),
            (
                "fetch",
                [
                    "git",
                    *GIT_HARDENING,
                    "-C",
                    str(dest),
                    "fetch",
                    "--depth",
                    "1",
                    "--no-recurse-submodules",
                    "--quiet",
                    "--",
                    url,
                    ref,
                ],
            ),
            (
                "checkout",
                [
                    "git",
                    *GIT_HARDENING,
                    "-C",
                    str(dest),
                    "checkout",
                    "--detach",
                    "--quiet",
                    "FETCH_HEAD",
                ],
            ),
        ]
    else:
        clone = [
            "git",
            *GIT_HARDENING,
            "clone",
            "--depth",
            "1",
            "--no-recurse-submodules",
            "--quiet",
        ]
        if ref:
            clone += ["--branch", ref]
        clone += ["--", url, str(dest)]
        steps = [("clone", clone)]
    for name, args in steps:
        result = runner(args, env=env, timeout=GIT_TIMEOUT)
        if not result.ok:
            err = _git_failed(name, result)
            if name == "fetch" and ref and len(ref) < 40:
                err.hint = (
                    "GitHub cannot fetch abbreviated SHAs; pass the full 40-character commit SHA"
                )
            raise err
    head = runner(["git", "-C", str(dest), "rev-parse", "HEAD"], env=env, timeout=GIT_QUICK_TIMEOUT)
    sha = head.stdout.strip()
    if not head.ok or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise _git_failed("rev-parse", head)
    return sha


def _repo_controlled_filters(runner: Runner, base: list[str], env) -> set[str] | None:
    """Names of filter drivers defined by the repository's own config (local or worktree scope).

    Drivers from the user's global or system config are trusted: a repository cannot plant
    them. Returns None when the config could not be read, which callers treat as unsafe.
    """
    result = runner(
        [*base, "config", "--show-scope", "--get-regexp", r"^filter\."],
        env=env,
        timeout=GIT_QUICK_TIMEOUT,
    )
    if result.returncode == 1 and not result.stdout.strip():
        return set()  # git config exits 1 when nothing matches
    if not result.ok:
        return None
    names: set[str] = set()
    for line in result.stdout.splitlines():
        scope, _, rest = line.partition("\t")
        if scope not in {"local", "worktree", "command"}:
            continue
        key = rest.split(None, 1)[0] if rest.strip() else ""
        parts = key.split(".")
        names.add(".".join(parts[1:-1]) if len(parts) >= 3 else key)
    return names


def local_identity(path: Path, runner: Runner) -> SourceIdentity:
    """Read-only Git metadata. fsmonitor is disabled because a hostile .git/config can run code."""
    identity = SourceIdentity(kind="local", path=str(path))
    env = git_env()
    base = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(path)]
    inside = runner(
        [*base, "rev-parse", "--is-inside-work-tree"], env=env, timeout=GIT_QUICK_TIMEOUT
    )
    if not inside.ok or inside.stdout.strip() != "true":
        reason = (inside.stderr or "").strip().splitlines()
        identity.git_note = (
            "not a Git repository"
            if not reason or "not a git repository" in reason[-1].lower()
            else f"Git metadata unavailable: {reason[-1]}"
        )
        return identity
    head = runner([*base, "rev-parse", "HEAD"], env=env, timeout=GIT_QUICK_TIMEOUT)
    if head.ok and re.fullmatch(r"[0-9a-f]{40}", head.stdout.strip()):
        identity.git_head = head.stdout.strip()
    else:
        identity.git_note = "Git repository has no commits"
    filters = _repo_controlled_filters(runner, base, env)
    if filters is None:
        identity.git_note = "dirty state unknown: could not check the repository's Git config"
        return identity
    if filters:
        # `git status` re-cleans stat-dirty files through any configured clean filter, which
        # runs an arbitrary command from the repository's own config. Never do that.
        names = ", ".join(sorted(filters))
        identity.git_note = (
            f"dirty state unknown: the repository's own Git config defines filter driver(s) "
            f"({names}) and r2t will not run Git commands that could execute them"
        )
        return identity
    status = runner(
        [*base, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"],
        env=env,
        timeout=GIT_TIMEOUT,
    )
    if status.ok:
        identity.dirty = bool(status.stdout.strip())
    else:
        identity.git_note = "dirty state unknown: `git status` failed"
    return identity


@contextmanager
def acquire(source: str, ref: str | None, runner: Runner) -> Iterator[Acquired]:
    """Yield the repository root and its identity. Remote clones are always removed."""
    if looks_like_url(source):
        owner, repo = parse_github_url(source)
        if ref is not None:
            validate_ref(ref, runner)
        url = f"https://github.com/{owner}/{repo}"
        with tempfile.TemporaryDirectory(prefix="r2t-clone-") as tmp:
            dest = Path(tmp) / "repo"
            sha = _clone(url, ref, dest, runner)
            identity = SourceIdentity(
                kind="remote", url=url, requested_ref=ref, resolved_commit=sha
            )
            yield Acquired(dest, identity)
        return
    if ref is not None:
        raise InputError("--ref only applies to remote (GitHub URL) sources")
    if source.startswith("-"):
        raise InputError(f"invalid source {source!r}: must not start with '-'")
    path = Path(source).expanduser()
    if not path.exists():
        raise InputError(f"source path does not exist: {source}")
    if not path.is_dir():
        raise InputError(f"source path is not a directory: {source}")
    path = path.resolve()
    yield Acquired(path, local_identity(path, runner))
