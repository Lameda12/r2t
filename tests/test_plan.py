from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import FIXTURES, FakeRunner, make_bundle
from r2t import render
from r2t.cli import Services, app
from r2t.errors import InputError
from r2t.runner import RunResult
from r2t.snapshot import collect

SENTINEL = "SECRET_SENTINEL_9f2c"


def cli(*args, runner=None, **kw):
    return CliRunner().invoke(app, list(args), obj=Services(runner=runner or FakeRunner()), **kw)


def read_json(path: Path):
    return json.loads(path.read_text())


@pytest.fixture
def next_bundle(tmp_path) -> Path:
    return make_bundle(FIXTURES / "next-app", tmp_path / "out")


def test_bundle_layout(next_bundle):
    names = {p.name for p in next_bundle.iterdir()}
    assert names == {
        "source",
        "Dockerfile",
        ".dockerignore",
        "compose.yaml",
        "plan.json",
        "REPORT.md",
        ".env.template",
    }
    assert (next_bundle / "source" / "package.json").is_file()
    assert not (next_bundle / "source" / "node_modules").exists()


def test_dockerignore_only_allows_source_and_dockerfile(next_bundle):
    lines = [
        ln
        for ln in (next_bundle / ".dockerignore").read_text().splitlines()
        if ln and not ln.startswith("#")
    ]
    assert lines == ["*", "!source", "!Dockerfile"]


def test_compose_is_loopback_only_unprivileged_and_mount_free(next_bundle):
    text = (next_bundle / "compose.yaml").read_text()
    plan = read_json(next_bundle / "plan.json")
    port = plan["container_port"]
    assert f'"127.0.0.1:${{R2T_HOST_PORT:-{port}}}:{port}"' in text
    assert re.search(r"^name: \"r2t-next-app-[0-9a-f]{8}\"$", text, re.M)
    assert f'image: "r2t/{plan["compose_project"]}:latest"' in text
    assert f'dev.r2t.bundle: "{plan["bundle_id"]}"' in text
    assert "no-new-privileges:true" in text and "cap_drop:" in text and "- ALL" in text
    for forbidden in (
        "volumes",
        "privileged",
        "cap_add",
        "network_mode",
        "pid:",
        "ipc:",
        "devices",
        "docker.sock",
        "0.0.0.0",
    ):
        assert forbidden not in text
    assert "required: false" in text


def test_dockerfiles_use_non_root_users(tmp_path):
    node = make_bundle(FIXTURES / "next-app", tmp_path / "n")
    py = make_bundle(FIXTURES / "fastapi-app", tmp_path / "p")
    vite = make_bundle(FIXTURES / "vite-app", tmp_path / "v")
    nd = (node / "Dockerfile").read_text()
    assert "USER node" in nd and "COPY --chown=node:node source/ ./" in nd
    assert nd.index("USER node") < nd.index("RUN pnpm install --frozen-lockfile")
    assert "NEXT_TELEMETRY_DISABLED=1" in nd
    assert "npm install --global pnpm@10.28.0" in nd
    pd = (py / "Dockerfile").read_text()
    assert "USER app" in pd and "COPY --chown=app:app source/ ./" in pd
    for var in (
        "PYTHONDONTWRITEBYTECODE=1",
        "PIP_NO_CACHE_DIR=1",
        "PIP_DISABLE_PIP_VERSION_CHECK=1",
    ):
        assert var in pd
    assert pd.index("USER app") < pd.index("RUN pip install")
    vd = (vite / "Dockerfile").read_text()
    assert "NEXT_TELEMETRY_DISABLED" not in vd and "pnpm" not in vd
    assert (
        'CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173", "--strictPort"]'
        in vd
    )


def test_plan_json_contents(next_bundle):
    plan = read_json(next_bundle / "plan.json")
    assert plan["schema_version"] == 1 and plan["status"] == "supported"
    assert plan["r2t_version"]
    assert plan["container_port"] == 3000 and plan["readiness_path"] == "/"
    assert re.fullmatch(r"r2t-next-app-[0-9a-f]{8}", plan["compose_project"])
    assert plan["commands"]["install"] == ["pnpm", "install", "--frozen-lockfile"]
    assert plan["commands"]["build"][:5] == [
        "docker",
        "compose",
        "-p",
        plan["compose_project"],
        "-f",
    ]
    assert (
        plan["files"]["dockerfile_sha256"]
        == hashlib.sha256((next_bundle / "Dockerfile").read_bytes()).hexdigest()
    )
    assert (
        plan["files"]["compose_sha256"]
        == hashlib.sha256((next_bundle / "compose.yaml").read_bytes()).hexdigest()
    )
    assert plan["detections"] and all(d["evidence"] for d in plan["detections"])
    assert plan["assumptions"] and plan["variation_sources"] and plan["pinned"]
    src = plan["source"]
    assert src["kind"] == "local" and src["path"].endswith("tests/fixtures/next-app")
    assert "dirty" in src


def test_snapshot_digest_matches_bundle_source(next_bundle):
    plan = read_json(next_bundle / "plan.json")
    assert plan["source"]["snapshot_digest"] == collect(next_bundle / "source").digest()


def test_sentinels_only_in_the_copied_example_file(next_bundle):
    hits = []
    for path in next_bundle.rglob("*"):
        if path.is_file() and SENTINEL in path.read_text(errors="ignore"):
            hits.append(path.relative_to(next_bundle).as_posix())
    assert hits == ["source/.env.example"]
    template = (next_bundle / ".env.template").read_text()
    assert "DATABASE_URL=\n" in template and "NEXT_PUBLIC_API_KEY=\n" in template


def test_cli_output_never_contains_sentinels(tmp_path):
    for args in (
        ["inspect", str(FIXTURES / "next-app")],
        ["inspect", str(FIXTURES / "next-app"), "--json"],
        ["plan", str(FIXTURES / "next-app"), "-o", str(tmp_path / "o"), "--json"],
    ):
        result = cli(*args)
        assert result.exit_code == 0, result.output
        assert SENTINEL not in result.output and SENTINEL not in result.stderr


def test_report_is_honest_and_complete(next_bundle):
    report = (next_bundle / "REPORT.md").read_text()
    assert "reproducible" in report  # only in the "does not claim" form
    assert "does not claim the app works or that the build is reproducible" in report
    for heading in (
        "## Source",
        "## Launch plan",
        "## Detections and evidence",
        "## Assumptions",
        "## Scripts that will execute",
        "## Environment variable names",
        "## Sources of variation",
    ):
        assert heading in report
    assert "referenced by tag, not by digest" in report
    assert "Source snapshot digest" in report
    assert "pnpm 10.28.0 (from package.json#packageManager)" in report
    assert "Docker is an isolation boundary" in report
    assert SENTINEL not in report


def test_report_lists_excluded_registry_config_and_symlinks(make_tree, tmp_path):
    (tmp_path / "secret.txt").write_text("s")
    root = make_tree(
        {
            "package.json": json.dumps({"scripts": {"dev": "vite"}}),
            "package-lock.json": "{}",
            ".npmrc": "//r/:_authToken=TOKEN123\n",
            ".env": "REAL=hunter2\n",
        }
    )
    (root / "leak").symlink_to("../secret.txt")
    out = make_bundle(root, tmp_path / "out")
    report = (out / "REPORT.md").read_text()
    assert ".npmrc" in report and "registry" in report
    assert "`leak`: target escapes the source root" in report
    assert "TOKEN123" not in report and "hunter2" not in report
    assert not (out / "source" / ".npmrc").exists() and not (out / "source" / "leak").exists()


def test_existing_output_dir_refused_even_if_empty(tmp_path):
    out = tmp_path / "exists"
    out.mkdir()
    result = cli("plan", str(FIXTURES / "vite-app"), "-o", str(out))
    assert result.exit_code == 2
    assert "already exists" in result.stderr
    assert list(out.iterdir()) == []


def test_output_inside_source_refused(make_tree, tmp_path):
    root = make_tree({"package.json": json.dumps({"scripts": {"dev": "vite"}})})
    result = cli("plan", str(root), "-o", str(root / "bundle"))
    assert result.exit_code == 2
    assert not (root / "bundle").exists()


def test_atomic_write_leaves_nothing_behind(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(render, "render_report", boom)
    out = tmp_path / "parent" / "out"
    with pytest.raises(RuntimeError):
        make_bundle(FIXTURES / "vite-app", out)
    assert not out.exists()
    assert list((tmp_path / "parent").iterdir()) == []


def test_atomic_write_failure_does_not_touch_existing_siblings(tmp_path, monkeypatch):
    sibling = tmp_path / "keep"
    sibling.mkdir()
    (sibling / "f").write_text("x")
    monkeypatch.setattr(render, "render_compose", lambda *a, **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        make_bundle(FIXTURES / "vite-app", tmp_path / "out")
    assert [p.name for p in tmp_path.iterdir()] == ["keep"]
    assert (sibling / "f").read_text() == "x"


def test_unsupported_writes_report_but_no_docker_files_and_exits_3(tmp_path):
    out = tmp_path / "amb"
    result = cli("plan", str(FIXTURES / "ambiguous-app"), "-o", str(out))
    assert result.exit_code == 3
    assert {p.name for p in out.iterdir()} == {"plan.json", "REPORT.md", ".env.template"}
    plan = read_json(out / "plan.json")
    assert plan["status"] == "needs_input"
    assert plan["blockers"][0]["flag"] == "--package-manager {npm,pnpm}"
    assert plan["files"] == {"dockerfile_sha256": None, "compose_sha256": None}
    assert "--package-manager {npm,pnpm}" in (out / "REPORT.md").read_text()
    assert "No Dockerfile or compose.yaml" in result.output


def test_flag_in_needs_input_resolves_through_the_cli(tmp_path):
    result = cli(
        "plan",
        str(FIXTURES / "ambiguous-app"),
        "-o",
        str(tmp_path / "o"),
        "--package-manager",
        "pnpm",
    )
    assert result.exit_code == 0, result.output
    plan = read_json(tmp_path / "o" / "plan.json")
    assert plan["package_manager"]["name"] == "pnpm"
    assert any(e["kind"] == "user override" for d in plan["detections"] for e in d["evidence"])


def test_remote_identity_in_plan(tmp_path):
    sha = "b" * 40

    def handler(argv):
        if "clone" in argv:
            dest = Path(argv[-1])
            dest.mkdir(parents=True)
            (dest / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
            (dest / "package-lock.json").write_text("{}")
            (dest / ".git").mkdir()
            (dest / ".git" / "config").write_text("x")
            return RunResult(argv, 0)
        if "rev-parse" in argv:
            return RunResult(argv, 0, sha + "\n")
        return None

    runner = FakeRunner(handler)
    result = cli(
        "plan",
        "https://github.com/o/some-repo",
        "--ref",
        "v1",
        "-o",
        str(tmp_path / "o"),
        runner=runner,
    )
    assert result.exit_code == 0, result.output
    plan = read_json(tmp_path / "o" / "plan.json")
    assert plan["source"]["kind"] == "remote"
    assert plan["source"]["url"] == "https://github.com/o/some-repo"
    assert plan["source"]["requested_ref"] == "v1"
    assert plan["source"]["resolved_commit"] == sha
    assert re.fullmatch(r"r2t-some-repo-[0-9a-f]{8}", plan["compose_project"])
    assert not (tmp_path / "o" / "source" / ".git").exists()


def test_subdir_recorded_in_identity(make_tree, tmp_path):
    root = make_tree(
        {
            "web/package.json": json.dumps({"scripts": {"dev": "vite"}}),
            "web/package-lock.json": "{}",
            "other/x.txt": "x",
        }
    )
    out = make_bundle(root, tmp_path / "o", subdir="web")
    plan = read_json(out / "plan.json")
    assert plan["source"]["subdir"] == "web" and plan["app_root"] == "web"
    assert not (out / "source" / "other").exists()


def test_repo_derived_markdown_is_neutralised(make_tree, tmp_path):
    script = "vite --port 3000 `rm -rf /` [link](http://evil) \x1b[31m"
    root = make_tree(
        {"package.json": json.dumps({"scripts": {"dev": script, "preinstall": script}})}
    )
    out = make_bundle(root, tmp_path / "o")
    report = (out / "REPORT.md").read_text()
    assert "\x1b" not in report
    plan_text = (out / "plan.json").read_text()
    assert "\x1b" not in plan_text


def test_json_flag_is_pure_json_without_ansi():
    result = cli("inspect", str(FIXTURES / "fastapi-app"), "--json")
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["schema_version"] == 1 and data["status"] == "supported"
    assert "\x1b" not in result.stdout
    plan = cli(
        "plan", str(FIXTURES / "fastapi-app"), "-o", "/tmp/r2t-json-plan-test-dir-unused", "--json"
    )
    json.loads(plan.stdout)
    import shutil

    shutil.rmtree("/tmp/r2t-json-plan-test-dir-unused", ignore_errors=True)


def test_inspect_exit_zero_even_when_blocked():
    result = cli("inspect", str(FIXTURES / "ambiguous-app"))
    assert result.exit_code == 0
    assert "needs_input" in result.stdout and "--package-manager" in result.stdout


def test_inspect_human_output_sections():
    result = cli("inspect", str(FIXTURES / "next-app"))
    out = result.stdout
    for text in (
        "Status: supported",
        "pnpm@10.28.0",
        "pnpm install --frozen-lockfile",
        "DATABASE_URL",
        "[start] dev: next dev --turbopack",
        "package.json#scripts.dev",
        "Node 26 is the highest",
    ):
        assert text in out
    assert SENTINEL not in out


def test_usage_errors_exit_2():
    assert cli("inspect", "/definitely/not/here").exit_code == 2
    assert cli("inspect", "https://evil.com/a/b").exit_code == 2
    assert cli("inspect", str(FIXTURES / "vite-app"), "--ref", "main").exit_code == 2
    assert cli("inspect", str(FIXTURES / "vite-app"), "--subdir", "../x").exit_code == 2
    assert cli("inspect", str(FIXTURES / "vite-app"), "--entry", "bad").exit_code in (0, 2)


def test_internal_error_exit_1(monkeypatch):
    import r2t.cli as cli_mod

    monkeypatch.setattr(cli_mod, "analyze", lambda *a, **k: 1 / 0)
    result = cli("inspect", str(FIXTURES / "vite-app"))
    assert result.exit_code == 1
    assert "unexpected internal error" in result.stderr


def test_unwritable_output_location_is_a_usage_error():
    with pytest.raises(InputError):
        make_bundle(FIXTURES / "vite-app", Path("/proc/definitely-not-writable/out"))
