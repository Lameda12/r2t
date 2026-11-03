from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from r2t.runner import RunResult

FIXTURES = Path(__file__).parent / "fixtures"


class FakeRunner:
    """Records every call; answers via a handler(args) -> RunResult | None."""

    def __init__(self, handler: Callable[[list[str]], RunResult | None] | None = None):
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []
        self.kwargs: list[dict] = []
        self.handler = handler

    def __call__(self, args: Sequence[str], *, cwd=None, env=None, timeout, stream=False):
        argv = [str(a) for a in args]
        self.calls.append(argv)
        self.envs.append(dict(env) if env is not None else None)
        self.kwargs.append({"cwd": cwd, "timeout": timeout, "stream": stream})
        if self.handler:
            result = self.handler(argv)
            if result is not None:
                return result
        return RunResult(argv, 0, "", "")


@pytest.fixture
def fake_runner() -> type[FakeRunner]:
    return FakeRunner


@pytest.fixture
def make_tree(tmp_path: Path) -> Callable[..., Path]:
    """make_tree({"a/b.txt": "x"}) -> root directory containing those files."""

    def _make(files: dict[str, str | bytes], name: str = "repo") -> Path:
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content)
        return root

    return _make


def git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout
