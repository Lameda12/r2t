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

    def __call__(
        self, args: Sequence[str], *, cwd=None, env=None, timeout, stream: bool | str = False
    ):
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


def run_analyze(path: Path, **opts):
    """Analyze a local directory without touching Git (fake runner)."""
    from r2t.analyze import analyze
    from r2t.models import Options
    from r2t.source import acquire

    with acquire(str(path), None, FakeRunner()) as acq:
        insp, snap = analyze(acq, Options(**opts))
        return insp, snap, acq


def assert_evidence_exists(insp, app_root: Path) -> None:
    """Every file evidence must point at a file that exists (and a line, if numeric)."""
    refs = [e for d in insp.detections for e in d.evidence]
    refs += [e for b in insp.blockers for e in b.evidence]
    refs += [s.evidence for s in insp.services]
    refs += [s.evidence for s in insp.lifecycle]
    refs += list(insp.container_files)
    for ev in refs:
        if ev.kind != "file" or ev.file is None or ev.detail == "ancestor of the app root":
            continue
        path = app_root / ev.file
        assert path.exists(), f"evidence points at missing file: {ev.ref()}"
        if ev.locator and ev.locator.isdigit():
            assert int(ev.locator) <= len(path.read_text().splitlines())


# ---- helpers for bundle / docker tests ---------------------------------------------------


def make_bundle(src: Path, out: Path, **opts) -> Path:
    """Run the real analysis + bundle writer on a local source directory."""
    from r2t.analyze import analyze
    from r2t.models import Options
    from r2t.render import write_bundle
    from r2t.source import acquire

    with acquire(str(src), None, FakeRunner()) as acq:
        insp, snap = analyze(acq, Options(**opts))
        return write_bundle(insp, snap, out, acq.root).path


def resolved_config(
    project: str, port: int, host_port: int | None = None, context: str = "/bundle", **svc_overrides
) -> dict:
    """What `docker compose config --format json` prints for a healthy r2t bundle."""
    svc = {
        "build": {"context": context, "dockerfile": "Dockerfile"},
        "image": f"r2t/{project}:latest",
        "ports": [
            {
                "mode": "ingress",
                "host_ip": "127.0.0.1",
                "target": port,
                "published": str(host_port or port),
                "protocol": "tcp",
            }
        ],
        "security_opt": ["no-new-privileges:true"],
        "cap_drop": ["ALL"],
    }
    svc.update(svc_overrides)
    return {"name": project, "services": {"app": svc}}


class DockerFake:
    """A scripted `docker` for CLI tests. Records calls via FakeRunner."""

    def __init__(self, bundle: Path, **kw):
        self.bundle = bundle
        plan = __import__("json").loads((bundle / "plan.json").read_text())
        self.project = plan["compose_project"]
        self.port = plan["container_port"]
        self.compose_version = kw.get("compose_version", "2.30.0")
        self.info_rc = kw.get("info_rc", 0)
        self.config_overrides = kw.get("config_overrides", {})
        self.config_top = kw.get("config_top", {})  # extra top-level keys, e.g. volumes
        self.env_files = kw.get("env_files")  # shown only for `config --no-env-resolution`
        self.build_rc = kw.get("build_rc", 0)
        self.up_rc = kw.get("up_rc", 0)
        self.state = kw.get("state", "running")
        self.exit_code = kw.get("exit_code", 1)
        self.build_exc = kw.get("build_exc")
        self.logs = kw.get("logs", "app  | listening\n")
        self.states_after = kw.get("states_after")  # list of states per ps call
        self.ps_calls = 0

    def __call__(self, argv: list[str]) -> RunResult | None:
        if argv[:2] == ["docker", "info"]:
            return RunResult(
                argv, self.info_rc, "27.0.0\n", "" if self.info_rc == 0 else "Cannot connect"
            )
        if argv[:3] == ["docker", "compose", "version"]:
            return RunResult(argv, 0, self.compose_version + "\n")
        verb = argv[argv.index("-f") + 2] if "-f" in argv else None
        if verb == "config":
            host_port = (
                int(self.last_env.get("R2T_HOST_PORT", self.port))
                if hasattr(self, "last_env")
                else self.port
            )
            cfg = resolved_config(
                self.project,
                self.port,
                host_port,
                context=str(self.bundle),
                **self.config_overrides,
            )
            cfg.update(self.config_top)
            if self.env_files is not None and "--no-env-resolution" in argv:
                cfg["services"]["app"]["env_file"] = self.env_files
            return RunResult(argv, 0, __import__("json").dumps(cfg))
        if verb == "build":
            if self.build_exc:
                raise self.build_exc
            return RunResult(
                argv, self.build_rc, "Step 1/3\nfailed: boom\n" if self.build_rc else "ok\n"
            )
        if verb == "up":
            return RunResult(argv, self.up_rc, "started\n", "")
        if verb == "ps":
            self.ps_calls += 1
            state = self.state
            if self.states_after:
                state = self.states_after[min(self.ps_calls - 1, len(self.states_after) - 1)]
            row = {
                "Service": "app",
                "State": state,
                "ExitCode": self.exit_code if state == "exited" else 0,
            }
            return RunResult(argv, 0, __import__("json").dumps([row]))
        if verb == "logs":
            return RunResult(argv, 0, self.logs)
        if verb == "down":
            return RunResult(argv, 0, "")
        return None


def docker_runner(bundle: Path, **kw):
    fake = DockerFake(bundle, **kw)
    runner = FakeRunner()

    def handler(argv):
        runner_env = runner.envs[-1] or {}
        fake.last_env = runner_env
        return fake(argv)

    runner.handler = handler
    return runner, fake
