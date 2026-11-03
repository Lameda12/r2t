"""HTTP readiness probe. Any HTTP response means "an endpoint responded", nothing more."""

from __future__ import annotations

import http.client
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

REQUEST_TIMEOUT = 2.0


@dataclass
class ProbeResult:
    status: int | None
    error: str | None = None


@dataclass
class ContainerState:
    state: str  # "running", "exited", "dead", "created", "missing", ...
    exit_code: int | None = None

    @property
    def gone(self) -> bool:
        return self.state in {"exited", "dead"}


@dataclass
class ReadyOutcome:
    kind: str  # "responded", "exited", "timeout"
    status: int | None = None
    exit_code: int | None = None
    last_error: str | None = None


Probe = Callable[[str, float], ProbeResult]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_probe(url: str, timeout: float) -> ProbeResult:
    """GET url once, without following redirects and without using any proxy."""
    if not url.startswith("http://127.0.0.1:"):
        return ProbeResult(None, "refusing to probe a non-loopback URL")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "r2t-readiness"})  # noqa: S310
    try:
        with opener.open(request, timeout=timeout) as response:
            return ProbeResult(response.status)
    except urllib.error.HTTPError as exc:
        exc.close()
        return ProbeResult(exc.code)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        return ProbeResult(None, str(getattr(exc, "reason", exc)))


def wait_for_ready(
    url: str,
    timeout: float,
    *,
    probe: Probe,
    container_state: Callable[[], ContainerState],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    interval: float = 1.0,
) -> ReadyOutcome:
    deadline = clock() + timeout
    last_error: str | None = None
    while True:
        state = container_state()
        if state.gone:
            return ReadyOutcome("exited", exit_code=state.exit_code, last_error=last_error)
        result = probe(url, REQUEST_TIMEOUT)
        if result.status is not None:
            return ReadyOutcome("responded", status=result.status)
        last_error = result.error
        if clock() >= deadline:
            return ReadyOutcome("timeout", last_error=last_error)
        sleep(interval)
