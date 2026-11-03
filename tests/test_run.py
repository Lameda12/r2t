from __future__ import annotations

import json
import shlex

import pytest
from typer.testing import CliRunner

from conftest import FIXTURES, FakeRunner, docker_runner, make_bundle
from r2t.cli import Services, app
from r2t.docker import safety_problems
from r2t.readiness import ProbeResult


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


def invoke(bundle, *args, runner, probe=None, tty=False, port_free=True, input=None, clock=None):
    clock = clock or Clock()
    svc = Services(
        runner=runner,
        probe=probe or (lambda url, t: ProbeResult(200)),
        clock=clock,
        sleep=clock.sleep,
        is_tty=lambda: tty,
        port_free=lambda p: port_free,
    )
    return CliRunner().invoke(app, ["run", str(bundle), *args], obj=svc, input=input)


@pytest.fixture
def bundle(tmp_path):
    return make_bundle(FIXTURES / "fastapi-app", tmp_path / "bundle")


def verbs(runner):
    out = []
    for call in runner.calls:
        if "-f" in call:
            out.append(call[call.index("-f") + 2])
        else:
            out.append(" ".join(call[:3]))
    return out


def test_non_tty_without_yes_exits_2_and_never_calls_docker(bundle):
    runner, _ = docker_runner(bundle)
    result = invoke(bundle, runner=runner, tty=False)
    assert result.exit_code == 2
    assert "--yes" in result.stderr
    assert runner.calls == []


def test_prompt_shown_and_declining_builds_nothing(bundle):
    runner, _ = docker_runner(bundle)
    result = invoke(bundle, runner=runner, tty=True, input="n\n")
    assert result.exit_code == 2
    assert "Continue?" in result.stdout
    assert "runs the repository's code" in result.stdout
    assert "isolation boundary" in result.stdout
    assert "build" not in verbs(runner) and "up" not in verbs(runner)


def test_prompt_accept_runs(bundle):
    runner, _ = docker_runner(bundle)
    result = invoke(bundle, runner=runner, tty=True, input="y\n")
    assert result.exit_code == 0, result.output
    assert "Continue?" in result.stdout
    assert "build" in verbs(runner) and "up" in verbs(runner)


def test_yes_skips_prompt_and_reports_success(bundle):
    runner, _ = docker_runner(bundle)
    result = invoke(bundle, "--yes", runner=runner)
    assert result.exit_code == 0, result.output
    assert "Continue?" not in result.stdout
    assert "an HTTP endpoint responded with status 200" in result.stdout
    assert "http://127.0.0.1:8000/" in result.stdout
    assert "/docs" in result.stdout
    assert "does not check that the app works" in result.stdout


def test_commands_are_shown_with_exact_argv(bundle):
    runner, fake = docker_runner(bundle)
    result = invoke(bundle, "--yes", runner=runner)
    build = shlex.join(
        ["docker", "compose", "-p", fake.project, "-f", str(bundle / "compose.yaml"), "build"]
    )
    assert build in result.stdout
    assert "R2T_HOST_PORT=8000" in result.stdout


def test_every_compose_call_uses_this_bundles_project_and_file(bundle):
    runner, fake = docker_runner(bundle)
    invoke(bundle, "--yes", runner=runner)
    compose_calls = [
        c for c in runner.calls if c[:2] == ["docker", "compose"] and c[2] != "version"
    ]
    assert compose_calls
    prefix = ["docker", "compose", "-p", fake.project, "-f", str(bundle / "compose.yaml")]
    for call in compose_calls:
        assert call[:6] == prefix
    assert not any("down" in c or "rm" in c or "prune" in c for c in runner.calls)


def test_only_allowlisted_environment_reaches_children(bundle, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
    monkeypatch.setenv("GITHUB_TOKEN", "leak-me")
    runner, _ = docker_runner(bundle)
    invoke(bundle, "--yes", runner=runner)
    for env in runner.envs:
        assert env is not None
        assert "AWS_SECRET_ACCESS_KEY" not in env and "GITHUB_TOKEN" not in env
    assert any(e.get("R2T_HOST_PORT") == "8000" for e in runner.envs if e)


def test_host_port_flag_sets_env_and_url(bundle):
    runner, _ = docker_runner(bundle)
    result = invoke(bundle, "--yes", "--host-port", "9123", runner=runner)
    assert result.exit_code == 0
    assert "http://127.0.0.1:9123/" in result.stdout
    assert all(
        e["R2T_HOST_PORT"] == "9123"
        for e, c in zip(runner.envs, runner.calls, strict=True)
        if "build" in c or "up" in c
    )


def test_busy_port_suggests_host_port(bundle):
    runner, _ = docker_runner(bundle)
    result = invoke(bundle, "--yes", runner=runner, port_free=False)
    assert result.exit_code == 2
    assert "--host-port" in result.stderr and "already in use" in result.stderr
    assert "build" not in verbs(runner)


def test_build_failure_exit_5(bundle):
    runner, _ = docker_runner(bundle, build_rc=1)
    result = invoke(bundle, "--yes", runner=runner)
    assert result.exit_code == 5
    assert "Build failed" in result.stderr
    assert "up" not in verbs(runner)


def test_container_exit_during_readiness_exit_6_with_logs_and_commands(bundle):
    runner, _ = docker_runner(
        bundle,
        states_after=["running", "exited"],
        exit_code=137,
        logs="app  | Traceback\napp  | boom\n",
    )
    probe_calls = []

    def probe(url, t):
        probe_calls.append(url)
        return ProbeResult(None, "refused")

    result = invoke(bundle, "--yes", runner=runner, probe=probe)
    assert result.exit_code == 6
    assert "exited" in result.stderr and "137" in result.stderr
    assert "Traceback" in result.stderr and "boom" in result.stderr
    d = shlex.quote(str(bundle))
    assert f"r2t logs {d}" in result.stderr and f"r2t stop {d}" in result.stderr
    assert "down" not in verbs(runner), "containers must be left in place"
    assert len(probe_calls) == 1, "must stop waiting as soon as the container exited"


def test_readiness_timeout_exit_6(bundle):
    runner, _ = docker_runner(bundle)
    clock = Clock()
    result = invoke(
        bundle,
        "--yes",
        "--timeout",
        "5",
        runner=runner,
        probe=lambda u, t: ProbeResult(None, "connection refused"),
        clock=clock,
    )
    assert result.exit_code == 6
    assert "timed out" in result.stderr and "connection refused" in result.stderr
    assert "r2t stop" in result.stderr
    assert clock.now >= 5
    assert "down" not in verbs(runner)


def test_404_counts_as_responded_and_is_reported(bundle):
    runner, _ = docker_runner(bundle)
    result = invoke(bundle, "--yes", runner=runner, probe=lambda u, t: ProbeResult(404))
    assert result.exit_code == 0
    assert "status 404" in result.stdout


def test_5xx_is_flagged_explicitly(bundle):
    runner, _ = docker_runner(bundle)
    result = invoke(bundle, "--yes", runner=runner, probe=lambda u, t: ProbeResult(503))
    assert result.exit_code == 0
    assert "status 503" in result.stdout and "server error" in result.stdout


def test_ctrl_c_during_build_exits_130(bundle):
    runner, _ = docker_runner(bundle, build_exc=KeyboardInterrupt())
    result = invoke(bundle, "--yes", runner=runner)
    assert result.exit_code == 130
    assert "up" not in verbs(runner) and "down" not in verbs(runner)


def test_ctrl_c_during_readiness_leaves_containers_and_prints_commands(bundle):
    runner, _ = docker_runner(bundle)

    def interrupting_probe(url, t):
        raise KeyboardInterrupt

    result = invoke(bundle, "--yes", runner=runner, probe=interrupting_probe)
    assert result.exit_code == 130
    assert "left as they are" in result.stderr
    assert "r2t stop" in result.stderr and "r2t logs" in result.stderr
    assert "down" not in verbs(runner)


def test_compose_too_old_exit_4(bundle):
    runner, _ = docker_runner(bundle, compose_version="2.23.3")
    result = invoke(bundle, "--yes", runner=runner)
    assert result.exit_code == 4
    assert "2.24.0" in result.stderr and "2.23.3" in result.stderr
    assert "build" not in verbs(runner)


def test_daemon_unreachable_exit_4(bundle):
    runner, _ = docker_runner(bundle, info_rc=1)
    result = invoke(bundle, "--yes", runner=runner)
    assert result.exit_code == 4 and "daemon" in result.stderr


def test_docker_missing_exit_4(bundle):
    runner, _ = docker_runner(bundle, info_rc=127)
    result = invoke(bundle, "--yes", runner=runner)
    assert result.exit_code == 4 and "not found" in result.stderr


@pytest.mark.parametrize(
    "override,needle",
    [
        ({"privileged": True}, "privileged"),
        ({"cap_add": ["SYS_ADMIN"]}, "cap_add"),
        ({"network_mode": "host"}, "network_mode"),
        ({"pid": "host"}, "pid: host"),
        ({"ipc": "host"}, "ipc: host"),
        ({"devices": [{"source": "/dev/kvm", "target": "/dev/kvm"}]}, "devices"),
        ({"volumes": [{"type": "bind", "source": "/etc", "target": "/host-etc"}]}, "bind-mounts"),
        (
            {
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/var/run/docker.sock",
                        "target": "/var/run/docker.sock",
                    }
                ]
            },
            "docker.sock",
        ),
        (
            {
                "ports": [
                    {
                        "mode": "ingress",
                        "host_ip": "0.0.0.0",
                        "target": 8000,
                        "published": "8000",
                        "protocol": "tcp",
                    }
                ]
            },
            "not 127.0.0.1",
        ),
        (
            {
                "ports": [
                    {"mode": "ingress", "target": 8000, "published": "8000", "protocol": "tcp"}
                ]
            },
            "not 127.0.0.1",
        ),
        ({"security_opt": ["seccomp=unconfined"]}, "unconfined"),
        ({"build": {"context": "/", "dockerfile": "Dockerfile"}}, "outside the bundle"),
        ({"volumes_from": ["x"]}, "volumes_from"),
    ],
)
def test_unsafe_edits_are_refused_exit_4(bundle, override, needle):
    runner, _ = docker_runner(bundle, config_overrides=override)
    result = invoke(bundle, "--yes", runner=runner)
    assert result.exit_code == 4, result.output
    assert "unsafe" in result.stderr and needle in result.stderr
    assert "build" not in verbs(runner) and "up" not in verbs(runner)


def test_safe_config_has_no_problems(bundle):
    from conftest import resolved_config

    cfg = resolved_config("p", 8000, context=str(bundle))
    assert safety_problems(cfg, bundle) == []


def test_named_volume_with_host_path_driver_opts_refused(bundle):
    from conftest import resolved_config

    cfg = resolved_config(
        "p", 8000, context=str(bundle), volumes=[{"type": "volume", "source": "v", "target": "/d"}]
    )
    cfg["volumes"] = {
        "v": {"driver": "local", "driver_opts": {"type": "none", "o": "bind", "device": "/"}}
    }
    assert any("host path" in p for p in safety_problems(cfg, bundle))


def test_modified_bundle_warns_and_continues(bundle):
    (bundle / "Dockerfile").write_text((bundle / "Dockerfile").read_text() + "\n# edited\n")
    runner, _ = docker_runner(bundle)
    result = invoke(bundle, "--yes", runner=runner)
    assert result.exit_code == 0
    assert "Dockerfile differ" in result.stderr


def test_unsupported_bundle_refused_exit_4(tmp_path):
    out = tmp_path / "amb"
    CliRunner().invoke(
        app,
        ["plan", str(FIXTURES / "ambiguous-app"), "-o", str(out)],
        obj=Services(runner=FakeRunner()),
    )
    runner = FakeRunner()
    result = invoke(out, "--yes", runner=runner)
    assert result.exit_code == 4 and "needs_input" in result.stderr
    assert runner.calls == []


def test_missing_files_refused_exit_4(bundle):
    (bundle / "compose.yaml").unlink()
    runner, _ = docker_runner(bundle)
    result = invoke(bundle, "--yes", runner=runner)
    assert result.exit_code == 4 and "compose.yaml" in result.stderr
    assert runner.calls == []


def test_invalid_plan_values_refused(bundle):
    plan = json.loads((bundle / "plan.json").read_text())
    plan["compose_project"] = "x; rm -rf /"
    (bundle / "plan.json").write_text(json.dumps(plan))
    runner, _ = docker_runner(bundle)
    assert invoke(bundle, "--yes", runner=runner).exit_code == 4
    assert runner.calls == []


def test_missing_directory_is_usage_error(tmp_path):
    runner = FakeRunner()
    result = invoke(tmp_path / "nope", "--yes", runner=runner)
    assert result.exit_code == 2


# ---- logs and stop ------------------------------------------------------------------------


def run_cmd(bundle, *args, runner):
    return CliRunner().invoke(app, list(args), obj=Services(runner=runner))


def test_stop_uses_only_this_project_and_never_deletes_data_or_images(bundle):
    runner, fake = docker_runner(bundle)
    result = run_cmd(bundle, "stop", str(bundle), runner=runner)
    assert result.exit_code == 0
    assert runner.calls == [
        ["docker", "compose", "-p", fake.project, "-f", str(bundle / "compose.yaml"), "down"]
    ]
    flat = " ".join(runner.calls[0])
    for banned in ("-v", "--volumes", "--rmi", "prune", "--remove-orphans"):
        assert banned not in runner.calls[0], flat
    assert f"docker image rm r2t/{fake.project}:latest" in result.stdout
    assert len(runner.calls) == 1, "the image removal is printed, never executed"


def test_logs_uses_only_this_project(bundle):
    runner, fake = docker_runner(bundle, logs="app  | hello\n")
    result = run_cmd(bundle, "logs", str(bundle), "--tail", "7", runner=runner)
    assert result.exit_code == 0 and "hello" in result.stdout
    assert runner.calls == [
        [
            "docker",
            "compose",
            "-p",
            fake.project,
            "-f",
            str(bundle / "compose.yaml"),
            "logs",
            "--no-color",
            "--tail",
            "7",
        ]
    ]


def test_logs_follow_streams_directly(bundle):
    runner, fake = docker_runner(bundle)
    run_cmd(bundle, "logs", str(bundle), "--follow", runner=runner)
    call = runner.calls[0]
    assert call[:6] == ["docker", "compose", "-p", fake.project, "-f", str(bundle / "compose.yaml")]
    assert "--follow" in call and runner.kwargs[0]["stream"] == "inherit"


def test_logs_and_stop_reject_non_bundles(tmp_path):
    (tmp_path / "x").mkdir()
    runner = FakeRunner()
    assert run_cmd(tmp_path, "stop", str(tmp_path / "x"), runner=runner).exit_code == 4
    assert run_cmd(tmp_path, "logs", str(tmp_path / "nope"), runner=runner).exit_code == 2
    assert runner.calls == []
