"""Exceptions that carry process exit codes."""

from __future__ import annotations

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_NOT_SUPPORTED = 3
EXIT_DOCKER = 4
EXIT_BUILD = 5
EXIT_RUNTIME = 6
EXIT_INTERRUPTED = 130


class R2TError(Exception):
    """Base class: a failure the CLI reports without a traceback."""

    exit_code = EXIT_INTERNAL

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class InputError(R2TError):
    """Bad URL, ref, path, existing output directory, missing confirmation."""

    exit_code = EXIT_USAGE


class DockerError(R2TError):
    """Docker or Compose unavailable or too old, or the bundle is invalid or unsafe."""

    exit_code = EXIT_DOCKER


class BuildError(R2TError):
    """The image build failed."""

    exit_code = EXIT_BUILD


class RuntimeFailure(R2TError):
    """The container exited, or readiness timed out."""

    exit_code = EXIT_RUNTIME
