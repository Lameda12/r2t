"""One injectable boundary for every subprocess call."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_ENV_ALLOW = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "XDG_RUNTIME_DIR")
_ENV_ALLOW_PREFIX = ("DOCKER_", "BUILDKIT_")
_NETWORK_ENV = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "GIT_SSL_CAINFO",
    "CURL_CA_BUNDLE",
)


@dataclass
class RunResult:
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class Runner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float,
        stream: bool | str = False,
    ) -> RunResult: ...


def base_env(extra: Mapping[str, str] | None = None, *, network: bool = False) -> dict[str, str]:
    """Allow-listed environment for child processes. The host environment is never forwarded."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        allowed = key in _ENV_ALLOW or key.startswith(_ENV_ALLOW_PREFIX)
        if allowed or (network and key.upper() in _NETWORK_ENV):
            env[key] = value
    if extra:
        env.update(extra)
    return env


def _tail(lines: deque[str]) -> str:
    return "".join(lines)


class SubprocessRunner:
    """Runs argument arrays only. Never uses a shell."""

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float,
        stream: bool | str = False,
    ) -> RunResult:
        argv = [str(a) for a in args]
        run_env = dict(env) if env is not None else base_env()
        if stream == "inherit":
            return self._inherit(argv, cwd, run_env, timeout)
        if stream:
            return self._stream(argv, cwd, run_env, timeout)
        try:
            proc = subprocess.run(  # noqa: S603
                argv,
                cwd=cwd,
                env=run_env,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return RunResult(argv, 127, stderr=f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else ""
            return RunResult(argv, 124, stdout=out, stderr="timed out", timed_out=True)
        return RunResult(argv, proc.returncode, proc.stdout, proc.stderr)

    def _inherit(self, argv: list[str], cwd: Path | None, env: dict[str, str], timeout: float):
        """Let the child write straight to our stdout/stderr (used for `logs --follow`)."""
        try:
            proc = subprocess.run(  # noqa: S603
                argv, cwd=cwd, env=env, timeout=timeout, check=False, stdin=subprocess.DEVNULL
            )
        except FileNotFoundError:
            return RunResult(argv, 127, stderr=f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired:
            return RunResult(argv, 124, timed_out=True)
        return RunResult(argv, proc.returncode)

    def _stream(self, argv: list[str], cwd: Path | None, env: dict[str, str], timeout: float):
        """Echo combined output to stderr as it arrives; keep a tail for failure reports."""
        try:
            proc = subprocess.Popen(  # noqa: S603
                argv,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                errors="replace",
            )
        except FileNotFoundError:
            return RunResult(argv, 127, stderr=f"{argv[0]}: command not found")
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(timeout, _kill)
        timer.start()
        tail: deque[str] = deque(maxlen=200)
        try:
            assert proc.stdout is not None  # noqa: S101
            for line in proc.stdout:
                sys.stderr.write(line)
                sys.stderr.flush()
                tail.append(line)
            proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            timer.cancel()
        return RunResult(
            argv,
            124 if timed_out.is_set() else proc.returncode,
            stdout=_tail(tail),
            timed_out=timed_out.is_set(),
        )
