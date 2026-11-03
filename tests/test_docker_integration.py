"""Real plan -> run -> HTTP check -> stop cycles. Requires Docker; run with R2T_DOCKER_TESTS=1.

These tests build real images, which downloads base images and dependencies. Each test
touches only its own Compose project and removes only its own image.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import FIXTURES
from r2t.cli import app

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("R2T_DOCKER_TESTS") != "1", reason="set R2T_DOCKER_TESTS=1 to run"
    ),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def docker(*args: str, check: bool = True, stdin: str | None = None) -> str:
    proc = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, timeout=300, input=stdin
    )
    if check and proc.returncode:
        raise AssertionError(f"docker {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


def get(url: str) -> tuple[int, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=60) as resp:
        return resp.status, resp.read().decode(errors="replace")


@pytest.mark.parametrize(
    "fixture,needle,runtime_user_check",
    [
        ("vite-app", "r2t vite fixture", True),
        ("next-app", "r2t next fixture is running", True),
        ("fastapi-app", "r2t fastapi fixture is running", True),
    ],
)
def test_plan_run_check_stop(fixture, needle, runtime_user_check, tmp_path):
    runner = CliRunner()
    bundle = tmp_path / "bundle"
    result = runner.invoke(app, ["plan", str(FIXTURES / fixture), "-o", str(bundle)])
    assert result.exit_code == 0, result.output
    plan = json.loads((bundle / "plan.json").read_text())
    project, image = plan["compose_project"], plan["image"]
    # A user-created .env: must reach the container at run time but never the image.
    (bundle / ".env").write_text("R2T_TEST_ENV=from-dotenv\n")
    port = free_port()
    try:
        run = runner.invoke(
            app, ["run", str(bundle), "--yes", "--timeout", "420", "--host-port", str(port)]
        )
        assert run.exit_code == 0, run.output + run.stderr
        assert "an HTTP endpoint responded with status" in run.stdout

        status, body = get(f"http://127.0.0.1:{port}/")
        assert status == 200 and needle in body

        cid = docker(
            "compose", "-p", project, "-f", str(bundle / "compose.yaml"), "ps", "-q"
        ).strip()
        assert cid
        info = json.loads(docker("inspect", cid))[0]
        host_cfg = info["HostConfig"]
        assert host_cfg["CapDrop"] == ["ALL"]
        assert "no-new-privileges:true" in host_cfg["SecurityOpt"]
        assert not host_cfg["Privileged"] and not host_cfg["Binds"]
        bindings = host_cfg["PortBindings"]
        assert bindings and all(b["HostIp"] == "127.0.0.1" for v in bindings.values() for b in v)
        if runtime_user_check:
            uid = docker("exec", cid, "id", "-u").strip()
            assert uid != "0", "container must not run as root"
        assert docker("exec", cid, "printenv", "R2T_TEST_ENV").strip() == "from-dotenv"

        # bundle-root files must not be inside the image
        listing = docker(
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            image,
            "-c",
            "find / -xdev \\( -name plan.json -o -name REPORT.md -o -name .env \\)"
            " -not -path '*/node_modules/*' -not -path '/proc/*' -not -path '/usr/*'"
            " 2>/dev/null; true",
        )
        assert "plan.json" not in listing and "REPORT.md" not in listing
        assert "R2T_TEST_ENV" not in docker("run", "--rm", "--entrypoint", "sh", image, "-c", "env")
        assert not any(line.endswith("/.env") for line in listing.splitlines()), listing

        logs = runner.invoke(app, ["logs", str(bundle), "--tail", "20"])
        assert logs.exit_code == 0
    finally:
        runner.invoke(app, ["stop", str(bundle)])
        docker("image", "rm", image, check=False)
    leftover = docker("ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "-q")
    assert leftover.strip() == "", "stop must remove this project's containers"


def _fastapi_copy(tmp_path: Path, main_py: str | None = None, requirements: str | None = None):
    src = tmp_path / "src"
    shutil.copytree(FIXTURES / "fastapi-app", src)
    if main_py is not None:
        (src / "app" / "main.py").write_text(main_py)
    if requirements is not None:
        (src / "requirements.txt").write_text(requirements)
    return src


def _cleanup(runner: CliRunner, bundle: Path) -> None:
    plan = json.loads((bundle / "plan.json").read_text())
    runner.invoke(app, ["stop", str(bundle)])
    docker("image", "rm", plan["image"], check=False)


def test_crashing_app_exits_6_with_logs_and_leaves_container_for_inspection(tmp_path):
    runner = CliRunner()
    crash = (
        "from fastapi import FastAPI\n\napp = FastAPI()\nraise RuntimeError('r2t-crash-marker')\n"
    )
    src = _fastapi_copy(tmp_path, crash)
    bundle = tmp_path / "bundle"
    assert runner.invoke(app, ["plan", str(src), "-o", str(bundle)]).exit_code == 0
    project = json.loads((bundle / "plan.json").read_text())["compose_project"]
    try:
        run = runner.invoke(
            app, ["run", str(bundle), "--yes", "--timeout", "120", "--host-port", str(free_port())]
        )
        assert run.exit_code == 6, run.output + run.stderr
        assert "r2t-crash-marker" in run.stderr
        assert "r2t stop" in run.stderr and "r2t logs" in run.stderr
        exited = docker(
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "status=exited",
            "-q",
        )
        assert exited.strip(), "the exited container must be left in place"
    finally:
        _cleanup(runner, bundle)


def test_unsatisfiable_dependency_exits_5(tmp_path):
    runner = CliRunner()
    src = _fastapi_copy(
        tmp_path, requirements="fastapi==0.162.0\nuvicorn==0.54.0\nr2t-no-such-pkg-xyz==1\n"
    )
    bundle = tmp_path / "bundle"
    assert runner.invoke(app, ["plan", str(src), "-o", str(bundle)]).exit_code == 0
    try:
        run = runner.invoke(app, ["run", str(bundle), "--yes", "--host-port", str(free_port())])
        assert run.exit_code == 5, run.output + run.stderr
        assert "Build failed" in run.stderr
    finally:
        _cleanup(runner, bundle)


def _edit(bundle: Path, old: str, new: str) -> None:
    path = bundle / "compose.yaml"
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


def _break_dockerfile_symlink(bundle: Path) -> None:
    outside = bundle.parent / "outside.Dockerfile"
    outside.write_text((bundle / "Dockerfile").read_text())
    (bundle / "Dockerfile").unlink()
    (bundle / "Dockerfile").symlink_to(outside)


@pytest.mark.parametrize(
    "edit,needle",
    [
        (lambda b: _edit(b, "    init: true", '    init: true\n    user: "0"'), "root"),
        (
            lambda b: _edit(
                b, "      dockerfile: Dockerfile", "      dockerfile: ../outside.Dockerfile"
            ),
            "Dockerfile outside the bundle",
        ),
        (
            lambda b: (
                _edit(b, "    init: true", "    init: true\n    volumes:\n      - ext:/data"),
                (b / "compose.yaml").write_text(
                    (b / "compose.yaml").read_text() + "volumes:\n  ext:\n    external: true\n"
                ),
            ),
            "external volume",
        ),
        (
            lambda b: (
                (b.parent / "outside.env").write_text("X=1\n"),
                _edit(
                    b,
                    "      - path: .env\n        required: false",
                    "      - path: .env\n        required: false\n      - path: ../outside.env",
                ),
            ),
            "env_file",
        ),
        (_break_dockerfile_symlink, "Dockerfile resolves"),
    ],
    ids=[
        "user-root",
        "dockerfile-traversal",
        "external-volume",
        "env-file-outside",
        "symlinked-dockerfile",
    ],
)
def test_real_compose_config_of_edited_bundles_is_refused_before_building(edit, needle, tmp_path):
    runner = CliRunner()
    bundle = tmp_path / "bundle"
    assert (
        runner.invoke(app, ["plan", str(FIXTURES / "fastapi-app"), "-o", str(bundle)]).exit_code
        == 0
    )
    project = json.loads((bundle / "plan.json").read_text())["compose_project"]
    edit(bundle)
    result = runner.invoke(app, ["run", str(bundle), "--yes", "--host-port", str(free_port())])
    assert result.exit_code == 4, result.output + result.stderr
    assert needle in result.stderr, result.stderr
    assert docker("ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "-q") == ""
    assert (
        docker("image", "ls", "-q", json.loads((bundle / "plan.json").read_text())["image"]) == ""
    )


def test_stray_http_200_without_the_container_is_not_reported_as_ready(tmp_path):
    """Real Docker: remove the real container right after `up -d`, then let something else answer
    on the port. r2t must not attribute that response to the app."""
    import http.server
    import threading

    from r2t.cli import Services
    from r2t.runner import SubprocessRunner

    class Stray(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    servers: list[http.server.HTTPServer] = []
    port = free_port()

    class Sabotage(SubprocessRunner):
        def __call__(self, args, **kw):
            result = super().__call__(args, **kw)
            if list(args[-2:]) == ["up", "-d"] and result.ok:
                super().__call__(
                    [*args[:-2], "down"], cwd=kw.get("cwd"), env=kw.get("env"), timeout=120
                )
                srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Stray)
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                servers.append(srv)
            return result

    runner = CliRunner()
    bundle = tmp_path / "bundle"
    assert (
        runner.invoke(app, ["plan", str(FIXTURES / "fastapi-app"), "-o", str(bundle)]).exit_code
        == 0
    )
    try:
        result = runner.invoke(
            app,
            ["run", str(bundle), "--yes", "--timeout", "20", "--host-port", str(port)],
            obj=Services(runner=Sabotage()),
        )
        assert servers, "the sabotage hook did not run"
        assert get(f"http://127.0.0.1:{port}/")[0] == 200, "the stray server must really answer"
        assert result.exit_code == 6, result.output + result.stderr
        assert "not found" in result.stderr
        assert "responded" not in result.stdout
    finally:
        for srv in servers:
            srv.shutdown()
        _cleanup(runner, bundle)


# ---- issue #1: the validator's non-root verdict must match the real effective UID ----------

PY = "FROM python:3.12-slim-bookworm"
UID_CASES = [
    # (id, dockerfile, target, can the image really be non-root?)
    ("last-stage-root", f"{PY} AS a\nUSER 1000\n{PY} AS b\nUSER root\n", None, False),
    ("last-stage-user", f"{PY} AS a\nUSER root\n{PY} AS b\nUSER 1000\n", None, True),
    ("earlier-root-target", f"{PY} AS a\nUSER root\n{PY} AS b\nUSER 1000\n", "a", False),
    ("earlier-user-target", f"{PY} AS a\nUSER 1000\n{PY} AS b\nUSER root\n", "a", True),
    ("target-case", f"{PY} AS Stage\nUSER root\n{PY} AS b\nUSER 1000\n", "STAGE", False),
    ("inherit-root", f"{PY} AS base\nUSER root\nFROM base AS app\n", None, False),
    ("inherit-user", f"{PY} AS base\nUSER 1000\nFROM base AS app\n", None, True),
    ("inherit-then-root", f"{PY} AS base\nUSER 1000\nFROM base AS app\nUSER 0\n", None, False),
    ("no-user", f"{PY}\n", None, False),
    ("user-group", f"{PY}\nUSER 1000:1000\n", None, True),
    ("root-group", f"{PY}\nUSER root:root\n", None, False),
    ("continuation", f"{PY}\nRUN true \\\n  && true\nUSER \\\n  1000\n", None, True),
    ("variable-user", f"{PY}\nARG UID=1000\nUSER ${{UID}}\n", None, True),  # real uid 1000
    ("variable-root", f"{PY}\nARG UID=0\nUSER ${{UID}}\n", None, False),
    # issue #2: heredoc payload is not Dockerfile syntax; tabs separate instruction arguments
    ("heredoc-user", f"{PY}\nRUN cat <<EOF > /tmp/x\nUSER 1000\nEOF\n", None, False),
    ("heredoc-then-user", f"{PY}\nRUN <<EOF\ntrue\nEOF\nUSER 1000\n", None, True),
    ("tab-root", f"{PY}\nUSER 1000\nUSER\troot\n", None, False),
    ("tab-user", f"{PY}\nUSER\t1000\n", None, True),
]


@pytest.mark.parametrize(
    "dockerfile,target,really_non_root", [c[1:] for c in UID_CASES], ids=[c[0] for c in UID_CASES]
)
@pytest.mark.parametrize("inline", [False, True], ids=["file", "inline"])
def test_validator_verdict_matches_the_effective_container_uid(
    dockerfile, target, really_non_root, inline, tmp_path
):
    from r2t.docker import safety_problems

    ctx = tmp_path / "ctx"
    ctx.mkdir()
    build: dict = {"context": str(ctx)}
    if inline:
        build["dockerfile_inline"] = dockerfile
    else:
        (ctx / "Dockerfile").write_text(dockerfile)
        build["dockerfile"] = "Dockerfile"
    if target:
        build["target"] = target
    cfg = {"services": {"app": {"build": build}}}
    accepted = not [p for p in safety_problems(cfg, ctx) if "root" in p or "verify" in p]

    tag = f"r2t-uid-probe-{abs(hash((dockerfile, target, inline))) % 10**8}"
    args = ["build", "-q", "-t", tag, "-f", "-", str(ctx)]
    if target:
        args[1:1] = ["--target", target]
    try:
        docker(*args, stdin=dockerfile)
        uid = int(docker("run", "--rm", tag, "id", "-u").strip())
    finally:
        docker("image", "rm", "-f", tag, check=False)

    assert (uid != 0) is really_non_root, f"test expectation is wrong: real uid {uid}"
    if accepted:
        assert uid != 0, "validator accepted a bundle whose container really runs as root"
    elif really_non_root:
        # refusing a genuinely non-root image is allowed only when it depends on a variable
        assert "$" in dockerfile, "validator wrongly refused a resolvable non-root image"


def _multistage_bundle(tmp_path: Path, compose_target: str, extra_stage: str) -> tuple[Path, dict]:
    runner = CliRunner()
    bundle = tmp_path / "bundle"
    assert (
        runner.invoke(app, ["plan", str(FIXTURES / "fastapi-app"), "-o", str(bundle)]).exit_code
        == 0
    )
    docker_file = bundle / "Dockerfile"
    text = docker_file.read_text().replace(f"{PY}\n", f"{PY} AS app\n", 1)
    docker_file.write_text(text + "\n" + extra_stage)
    compose = bundle / "compose.yaml"
    compose.write_text(
        compose.read_text().replace(
            "      dockerfile: Dockerfile",
            f"      dockerfile: Dockerfile\n      target: {compose_target}",
        )
    )
    return bundle, json.loads((bundle / "plan.json").read_text())


def test_run_follows_build_target_to_a_non_root_stage_even_if_the_last_stage_is_root(tmp_path):
    runner = CliRunner()
    bundle, plan = _multistage_bundle(tmp_path, "app", f"{PY} AS rootlast\nUSER root\n")
    try:
        run = runner.invoke(
            app, ["run", str(bundle), "--yes", "--timeout", "120", "--host-port", str(free_port())]
        )
        assert run.exit_code == 0, run.output + run.stderr
        cid = docker(
            "compose", "-p", plan["compose_project"], "-f", str(bundle / "compose.yaml"), "ps", "-q"
        ).strip()
        assert docker("exec", cid, "id", "-u").strip() == "1000"
    finally:
        _cleanup(runner, bundle)


def test_run_refuses_a_root_build_target_before_building(tmp_path):
    runner = CliRunner()
    bundle, plan = _multistage_bundle(tmp_path, "rootstage", "")
    docker_file = bundle / "Dockerfile"
    docker_file.write_text(
        f'{PY} AS rootstage\nUSER root\nCMD ["sleep", "60"]\n' + docker_file.read_text()
    )
    try:
        run = runner.invoke(app, ["run", str(bundle), "--yes", "--host-port", str(free_port())])
        assert run.exit_code == 4, run.output + run.stderr
        assert "would run as root" in run.stderr and "rootstage" in run.stderr
        label = f"label=com.docker.compose.project={plan['compose_project']}"
        assert docker("ps", "-a", "--filter", label, "-q") == ""
        assert docker("image", "ls", "-q", plan["image"]) == ""
    finally:
        _cleanup(runner, bundle)


def test_run_refuses_an_inline_root_dockerfile_before_building(tmp_path):
    runner = CliRunner()
    bundle = tmp_path / "bundle"
    assert (
        runner.invoke(app, ["plan", str(FIXTURES / "fastapi-app"), "-o", str(bundle)]).exit_code
        == 0
    )
    plan = json.loads((bundle / "plan.json").read_text())
    compose = bundle / "compose.yaml"
    compose.write_text(
        compose.read_text().replace(
            "      dockerfile: Dockerfile",
            "      dockerfile_inline: |\n"
            f"        {PY}\n        USER root\n"
            '        CMD ["sleep", "60"]',
        )
    )
    try:
        run = runner.invoke(app, ["run", str(bundle), "--yes", "--host-port", str(free_port())])
        assert run.exit_code == 4, run.output + run.stderr
        assert "would run as root" in run.stderr
        assert docker("image", "ls", "-q", plan["image"]) == ""
    finally:
        _cleanup(runner, bundle)
