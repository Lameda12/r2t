"""Detection helpers shared by every stack."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from ..models import EnvName, Evidence, ServiceHint, file_evidence
from ..snapshot import ENV_EXAMPLE_NAMES, Snapshot, dir_exclusion

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

# Environment variable names that suggest an external service.
_ENV_SERVICE_HINTS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(^|_)(DATABASE|DB)(_URL|_URI|_HOST)$|^POSTGRES|^PG(HOST|URL)", re.I),
        "a SQL database",
    ),
    (re.compile(r"MYSQL", re.I), "a MySQL database"),
    (re.compile(r"MONGO", re.I), "a MongoDB database"),
    (re.compile(r"REDIS", re.I), "Redis"),
    (re.compile(r"(AMQP|RABBITMQ|KAFKA|SQS)", re.I), "a message broker"),
    (re.compile(r"(S3_|AWS_ACCESS|AWS_SECRET)", re.I), "cloud storage credentials"),
    (re.compile(r"(OPENAI|ANTHROPIC|STRIPE|SENDGRID|TWILIO)_", re.I), "a third-party API key"),
]

NODE_SERVICE_DEPS = {
    "prisma": "Prisma (a database; usually needs DATABASE_URL and migrations)",
    "@prisma/client": "Prisma (a database; usually needs DATABASE_URL and migrations)",
    "mongoose": "MongoDB",
    "mongodb": "MongoDB",
    "redis": "Redis",
    "ioredis": "Redis",
    "pg": "PostgreSQL",
    "mysql2": "MySQL",
    "typeorm": "a SQL database",
    "sequelize": "a SQL database",
    "drizzle-orm": "a SQL database",
    "@supabase/supabase-js": "Supabase",
}
PYTHON_SERVICE_DEPS = {
    "psycopg": "PostgreSQL",
    "psycopg2": "PostgreSQL",
    "psycopg2-binary": "PostgreSQL",
    "asyncpg": "PostgreSQL",
    "sqlalchemy": "a SQL database",
    "pymongo": "MongoDB",
    "motor": "MongoDB",
    "redis": "Redis",
    "celery": "a message broker and workers",
    "pymysql": "MySQL",
    "aiomysql": "MySQL",
}

_CONTAINER_FILES = (
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    ".devcontainer/devcontainer.json",
    ".devcontainer.json",
    "Procfile",
)


def extract_env_names(text: str) -> list[tuple[str, int]]:
    """Variable names (never values) from KEY=... and export KEY=... lines."""
    out: list[tuple[str, int]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if m:
            out.append((m.group(1), lineno))
    return out


def collect_env_names(snap: Snapshot) -> list[EnvName]:
    seen: set[str] = set()
    result: list[EnvName] = []
    for name in ENV_EXAMPLE_NAMES:
        if not snap.has_file(name):
            continue
        for var, lineno in extract_env_names(snap.read_text(name)):
            if var not in seen:
                seen.add(var)
                result.append(EnvName(var, name, lineno))
    return result


def env_service_hints(env_names: list[EnvName]) -> list[ServiceHint]:
    hints: list[ServiceHint] = []
    for env in env_names:
        for pattern, what in _ENV_SERVICE_HINTS:
            if pattern.search(env.name):
                hints.append(
                    ServiceHint(
                        what,
                        f"{env.name} is listed in {env.file}; you may need to provide {what}",
                        file_evidence(env.file, env.line),
                    )
                )
                break
    return hints


def existing_compose_services(snap: Snapshot) -> list[ServiceHint]:
    """Service names from an existing compose file (read as text; nothing is executed)."""
    hints: list[ServiceHint] = []
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        if not snap.has_file(name):
            continue
        in_services = False
        child_indent: int | None = None
        for lineno, line in enumerate(snap.read_text(name).splitlines(), start=1):
            if re.match(r"^services:\s*(#.*)?$", line):
                in_services, child_indent = True, None
                continue
            if not in_services or not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line[0].isspace():  # next top-level key ends the services block
                in_services = False
                continue
            indent = len(line) - len(line.lstrip())
            if child_indent is None:
                child_indent = indent
            m = re.match(r"^([A-Za-z0-9_.-]+):\s*(#.*)?$", line.strip())
            if indent == child_indent and m:
                hints.append(
                    ServiceHint(
                        f"service {m.group(1)!r}",
                        f"{name} defines a service named {m.group(1)!r} that this app may expect",
                        file_evidence(name, lineno),
                    )
                )
    return hints


def existing_container_files(snap: Snapshot) -> list[Evidence]:
    return [file_evidence(name) for name in _CONTAINER_FILES if snap.has_file(name)]


# ---- app root search ----------------------------------------------------------------------


def iter_dirs(root: Path, max_depth: int):
    """Yield (relative POSIX path, absolute path) of non-excluded directories, breadth-limited."""
    root = root.resolve()
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        here = Path(dirpath)
        rel_dir = here.relative_to(root).as_posix()
        depth = 0 if rel_dir == "." else rel_dir.count("/") + 1
        keep = []
        for name in sorted(dirnames):
            path = here / name
            if path.is_symlink():
                continue
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if dir_exclusion(name, rel, path):
                continue
            keep.append(name)
        dirnames[:] = keep if depth < max_depth else []
        yield rel_dir, here


def read_small(path: Path, limit: int = 1_000_000) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def find_candidate_dirs(
    root: Path, looks_like_app: Callable[[Path], str | None], max_depth: int = 3
) -> list[dict[str, str]]:
    """Subdirectories (not the root) that look like a supported app."""
    found: list[dict[str, str]] = []
    for rel, path in iter_dirs(root, max_depth):
        if rel == ".":
            continue
        kind = looks_like_app(path)
        if kind:
            found.append({"subdir": rel, "kind": kind, "flag": f"--subdir {rel}"})
    return found
