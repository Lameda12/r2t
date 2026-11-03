from __future__ import annotations

import contextlib
import http.server
import threading

import pytest

from r2t.readiness import ContainerState, ProbeResult, http_probe, wait_for_ready


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def run(probe, states, timeout=10):
    clock = Clock()
    it = iter(states)
    last = [ContainerState("running")]

    def state():
        with contextlib.suppress(StopIteration):
            last[0] = next(it)
        return last[0]

    out = wait_for_ready(
        "http://127.0.0.1:1/",
        timeout,
        probe=probe,
        container_state=state,
        clock=clock,
        sleep=clock.sleep,
    )
    return out, clock


def test_responds_immediately():
    out, clock = run(lambda u, t: ProbeResult(200), [])
    assert (out.kind, out.status) == ("responded", 200) and clock.sleeps == []


def test_retries_until_response():
    results = iter([ProbeResult(None, "refused")] * 3 + [ProbeResult(404)])
    out, clock = run(lambda u, t: next(results), [])
    assert (out.kind, out.status) == ("responded", 404) and len(clock.sleeps) == 3


def test_timeout_is_bounded():
    out, clock = run(lambda u, t: ProbeResult(None, "refused"), [], timeout=5)
    assert out.kind == "timeout" and out.last_error == "refused"
    assert clock.now <= 6


def test_container_exit_stops_waiting_immediately():
    calls = []

    def probe(u, t):
        calls.append(1)
        return ProbeResult(None, "refused")

    out, clock = run(probe, [ContainerState("running"), ContainerState("exited", 3)])
    assert out.kind == "exited" and out.exit_code == 3
    assert len(calls) == 1 and clock.now < 3


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = {"/ok": 200, "/missing": 404, "/boom": 500, "/redir": 302}.get(self.path, 200)
        self.send_response(code)
        if code == 302:
            self.send_header("Location", "/ok")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.mark.parametrize(
    "path,code", [("/ok", 200), ("/missing", 404), ("/boom", 500), ("/redir", 302)]
)
def test_http_probe_reports_status_codes_without_following_redirects(server, path, code):
    assert http_probe(f"http://127.0.0.1:{server}{path}", 2).status == code


def test_http_probe_connection_refused_is_not_a_response():
    result = http_probe("http://127.0.0.1:1/", 1)
    assert result.status is None and result.error


def test_http_probe_refuses_non_loopback():
    assert http_probe("http://example.com/", 1).status is None


# ---- an HTTP response only counts when the expected container is running ------------------


def test_missing_container_with_http_200_is_not_ready():
    """Regression: something else answering on the port used to be reported as 'responded'."""
    probed = []

    def probe(url, timeout):
        probed.append(url)
        return ProbeResult(200)

    out, _ = run(probe, [ContainerState("missing")] * 5)
    assert out.kind == "missing"
    assert out.status is None
    assert probed == [], "must not even ask the port while the container is absent"


@pytest.mark.parametrize("state", ["created", "restarting", "paused", "unknown", "removing"])
def test_non_running_container_is_never_probed_and_times_out(state):
    probed = []
    out, _ = run(
        lambda u, t: probed.append(u) or ProbeResult(200), [ContainerState(state)] * 100, timeout=5
    )
    assert out.kind == "timeout" and probed == []
    assert state in (out.last_error or "")


def test_container_that_starts_running_later_is_then_accepted():
    states = [ContainerState("created"), ContainerState("created")] + [
        ContainerState("running")
    ] * 5
    out, _ = run(lambda u, t: ProbeResult(200), states)
    assert (out.kind, out.status) == ("responded", 200)


def test_container_that_disappears_right_after_a_response_is_not_ready():
    # running when we ask, gone by the time the response has arrived: do not attribute it
    states = [ContainerState("running"), ContainerState("missing")]
    out, _ = run(lambda u, t: ProbeResult(200), states)
    assert out.kind == "missing"
    states = [ContainerState("running"), ContainerState("exited", 1)]
    out, _ = run(lambda u, t: ProbeResult(200), states)
    assert (out.kind, out.exit_code) == ("exited", 1)
