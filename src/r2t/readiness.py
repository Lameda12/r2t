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

    @property
    def running(self) -> bool:
        return self.state == "running"


@dataclass
class ReadyOutcome:
    kind: str  # "responded", "exited", "missing", "timeout"
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
    """Wait until the expected container is running AND an HTTP endpoint answers.

    A response is only attributed to the app while the container is known to be running,
    both before and after the request; otherwise anything else listening on the port (or a
    container that just died) could be mistaken for the app.
    """
    deadline = clock() + timeout
    last_error: str | None = None
    while True:
        state = container_state()
        if state.gone:
            return ReadyOutcome("exited", exit_code=state.exit_code, last_error=last_error)
        if state.state == "missing":
            return ReadyOutcome("missing", last_error=last_error)
        if state.running:
            result = probe(url, REQUEST_TIMEOUT)
            if result.status is not None:
                after = container_state()
                if after.running:
                    return ReadyOutcome("responded", status=result.status)
                if after.gone:
                    return ReadyOutcome("exited", exit_code=after.exit_code)
                if after.state == "missing":
                    return ReadyOutcome("missing")
                last_error = f"container state changed to {after.state} during the request"
            else:
                last_error = result.error
        else:
            last_error = f"container state is {state.state}"
        if clock() >= deadline:
            return ReadyOutcome("timeout", last_error=last_error)
        sleep(interval)
