"""r2t bench: real Git checkouts, scripted Docker (mocked), fake clock. No network."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import FIXTURES, FakeRunner, git, resolved_config
from r2t.bench import BenchReport, Trial, checkout_head, render_markdown
from r2t.cli import Services, app
from r2t.readiness import ProbeResult
from r2t.runner import RunResult, SubprocessRunner

REAL = SubprocessRunner()


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


class BenchDocker(FakeRunner):
    """Real `git`, scripted `docker`. Per-trial build durations advance the fake clock."""

    def __init__(self, clock: Clock, *, durations=(10.0,), **kw):
        super().__init__()
        self.clock = clock
        self.durations = list(durations)
        self.build_rc = kw.get("build_rc", {})  # trial number -> returncode
        self.state = kw.get("state", {})  # trial number -> container state
        self.exit_code = kw.get("exit_code", 1)
        self.logs = kw.get("logs", "app  | listening\n")
        self.git_fail = kw.get("git_fail")  # substring of a git verb to fail, e.g. "clone"
        self.git_raise = kw.get("git_raise")
        self.build_raise = kw.get("build_raise", {})  # trial number -> exception
        self.builds = 0

    def __call__(self, args, *, cwd=None, env=None, timeout, stream=False):
        argv = [str(a) for a in args]
        self.calls.append(argv)
        self.envs.append(dict(env) if env is not None else None)
        self.kwargs.append({"cwd": cwd, "timeout": timeout, "stream": stream})
        if argv[0] == "git":
            if self.git_raise and self.git_raise in argv:
                raise KeyboardInterrupt
            if self.git_fail and self.git_fail in argv:
                return RunResult(argv, 128, "", "fatal: simulated failure\n")
            return REAL(argv, cwd=cwd, env=env, timeout=timeout)
        if argv[:2] == ["docker", "info"]:
            return RunResult(argv, 0, "27.3.1\t8\t17179869184\n")
        if argv[:3] == ["docker", "compose", "version"]:
            return RunResult(argv, 0, "2.30.0\n")
        if argv[:3] == ["docker", "image", "rm"]:
            return RunResult(argv, 0, "")
        verb = argv[argv.index("-f") + 2]
        compose = Path(argv[argv.index("-f") + 1])
        trial = self.builds if verb != "build" else self.builds + 1
        if verb == "config":
            plan = json.loads((compose.parent / "plan.json").read_text())
            cfg = resolved_config(
                plan["compose_project"],
                plan["container_port"],
                int(env["R2T_HOST_PORT"]),
                context=str(compose.parent),
            )
            return RunResult(argv, 0, json.dumps(cfg))
        if verb == "build":
            self.builds += 1
            if self.builds in self.build_raise:
                raise self.build_raise[self.builds]
            self.clock.now += self.durations[min(self.builds, len(self.durations)) - 1]
            rc = self.build_rc.get(self.builds, 0)
            tail = "#7 ERROR: process npm ci did not complete successfully\n" if rc else "ok\n"
            return RunResult(argv, rc, tail)
        if verb == "up":
            return RunResult(argv, 0, "started\n")
        if verb == "ps":
            state = self.state.get(trial, "running")
            row = {"Service": "app", "State": state, "ExitCode": self.exit_code}
            return RunResult(argv, 0, json.dumps([row]))
        if verb == "logs":
            return RunResult(argv, 0, self.logs)
        if verb == "down":
            return RunResult(argv, 0, "")
        return RunResult(argv, 0, "")

    def verbs(self):
        out = []
        for c in self.calls:
            if c[0] == "docker" and "-f" in c:
                out.append(c[c.index("-f") + 2])
        return out


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """A real Git repo (one commit) containing the Vite fixture, plus an isolated TMPDIR."""
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    root = tmp_path / "vite-app"
    shutil.copytree(FIXTURES / "vite-app", root)
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")
    return root


def head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD").strip()


def bench(repo, *args, runner, probe=None, tty=False, clock=None, input=None):
    clock = clock or Clock()
    svc = Services(
        runner=runner,
        probe=probe or (lambda url, t: ProbeResult(200)),
        clock=clock,
        sleep=clock.sleep,
        is_tty=lambda: tty,
        port_free=lambda p: True,
    )
    return CliRunner().invoke(app, ["bench", str(repo), *args], obj=svc, input=input)


def rows(result):
    return [ln for ln in result.stdout.splitlines() if ln.startswith("| ")]


def leaderboard(result):
    return rows(result)[1]  # header is rows[0]; the separator starts with |---


def leftovers(repo):
    return [p.name for p in Path(tempfile.gettempdir()).iterdir()]


# ---- leaderboard ------------------------------------------------------------------------


def test_leaderboard_row_has_repo_commit_success_count_and_median(repo):
    clock = Clock()
    runner = BenchDocker(clock, durations=(10.0, 30.0, 20.0))
    result = bench(repo, "--runs", "3", "--yes", runner=runner, clock=clock)
    assert result.exit_code == 0, result.output + result.stderr
    assert rows(result)[0] == "| Repo | Commit | Successful runs | Median time to HTTP |"
    assert leaderboard(result) == f"| vite-app | `{head(repo)[:12]}` | 3/3 | 20.0s |"
    assert "Failures\nNone." in result.stdout
    assert leftovers(repo) == [], "temporary checkouts must be removed"


def test_runs_defaults_to_three_and_is_configurable(repo):
    clock = Clock()
    runner = BenchDocker(clock)
    result = bench(repo, "--yes", runner=runner, clock=clock)
    assert "| 3/3 |" in leaderboard(result) + " |"
    result = bench(repo, "--yes", "--runs", "1", runner=BenchDocker(Clock()))
    assert "1/1" in leaderboard(result)


def test_median_is_over_successful_runs_only(repo):
    clock = Clock()
    runner = BenchDocker(clock, durations=(10.0, 999.0, 40.0), build_rc={2: 1})
    result = bench(repo, "--runs", "3", "--yes", runner=runner, clock=clock)
    assert "| 2/3 | 25.0s |" in leaderboard(result)
    assert result.exit_code == 5


def test_every_trial_is_sequential_fresh_and_cache_free(repo):
    clock = Clock()
    runner = BenchDocker(clock)
    bench(repo, "--runs", "3", "--yes", runner=runner, clock=clock)
    builds = [c for c in runner.calls if c[0] == "docker" and c[-2:] == ["build", "--no-cache"]]
    assert len(builds) == 3
    projects = [c[c.index("-p") + 1] for c in builds]
    assert len(set(projects)) == 3, "a new Compose project (and image) per trial"
    # strictly sequential: trial N is fully cleaned up before trial N+1 starts cloning
    seq = [
        "clone" if c[0] == "git" and "clone" in c else c[c.index("-f") + 2]
        for c in runner.calls
        if (c[0] == "git" and "clone" in c)
        or (c[0] == "docker" and "-f" in c and c[c.index("-f") + 2] in {"build", "up", "down"})
    ]
    # the first clone is the untimed review step; then one full cycle per trial
    assert seq == ["clone"] + ["clone", "build", "up", "down"] * 3


def test_each_trial_checks_out_the_committed_head_not_the_working_tree(repo):
    (repo / "package.json").write_text('{"scripts": {"dev": "echo dirty"}}')  # uncommitted
    (repo / "untracked.txt").write_text("x")
    clock = Clock()
    runner = BenchDocker(clock)
    result = bench(repo, "--runs", "1", "--yes", runner=runner, clock=clock)
    assert result.exit_code == 0, result.output + result.stderr
    assert f"`{head(repo)[:12]}`" in leaderboard(result)
    assert "uncommitted changes in the working tree were ignored" in result.stdout
    checkout = next(c for c in runner.calls if c[0] == "git" and "checkout" in c)
    assert checkout[-1] == head(repo)


def test_checkout_head_is_exact_and_never_runs_source_repo_filters(repo, tmp_path):
    marker = tmp_path / "filter-ran"
    (repo / ".gitattributes").write_text("* filter=demo\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "attrs")
    git(repo, "config", "filter.demo.smudge", f"sh -c 'touch {marker}; cat'")
    git(repo, "config", "filter.demo.clean", f"sh -c 'touch {marker}; cat'")
    (repo / "package.json").write_text("changed")
    dest = tmp_path / "co"
    checkout_head(REAL, repo, head(repo), dest)
    assert not marker.exists()
    assert (dest / "package.json").read_text() != "changed"
    assert not (dest / "untracked.txt").exists()
    assert git(dest, "rev-parse", "HEAD").strip() == head(repo)


# ---- failures stay visible --------------------------------------------------------------


def test_build_failure_is_reported_with_stage_and_reason(repo):
    clock = Clock()
    runner = BenchDocker(clock, build_rc={1: 1, 2: 1})
    result = bench(repo, "--runs", "2", "--yes", runner=runner, clock=clock)
    assert result.exit_code == 5
    assert "| 0/2 | n/a (no successful run) |" in leaderboard(result)
    assert "- Run 1: stage `build`:" in result.stdout
    assert "npm ci did not complete successfully" in result.stdout
    assert "- Run 2: stage `build`:" in result.stdout
    assert "| 1 | failed at build |" in result.stdout


def test_all_failed_shows_no_median_number(repo):
    clock = Clock()
    result = bench(repo, "--yes", runner=BenchDocker(clock, build_rc={1: 1, 2: 1, 3: 1}))
    assert "0/3" in leaderboard(result)
    cell = leaderboard(result).split("|")[4].strip()
    assert cell.startswith("n/a") and not any(ch.isdigit() for ch in cell)


def test_container_exit_reports_readiness_stage_exit_code_and_log_line(repo):
    clock = Clock()
    runner = BenchDocker(
        clock, state={1: "exited"}, exit_code=137, logs="app  | Traceback\napp  | boom\n"
    )
    result = bench(repo, "--runs", "1", "--yes", runner=runner, clock=clock)
    assert result.exit_code == 6
    assert "- Run 1: stage `readiness`:" in result.stdout
    assert "exited with code 137" in result.stdout and "boom" in result.stdout
    assert "0/1" in leaderboard(result)
    assert "down" in runner.verbs(), "the exited container must still be cleaned up"


def test_readiness_timeout_is_a_failure_and_is_cleaned_up(repo):
    clock = Clock()
    runner = BenchDocker(clock)
    result = bench(
        repo,
        "--runs",
        "1",
        "--yes",
        "--timeout",
        "5",
        runner=runner,
        clock=clock,
        probe=lambda u, t: ProbeResult(None, "connection refused"),
    )
    assert result.exit_code == 6
    assert "stage `readiness`" in result.stdout and "no HTTP response within 5s" in result.stdout
    assert runner.verbs().count("down") == 1
    assert any(c[:3] == ["docker", "image", "rm"] for c in runner.calls)
    assert leftovers(repo) == []


def test_http_error_status_still_counts_as_serving_and_is_shown(repo):
    clock = Clock()
    result = bench(
        repo,
        "--runs",
        "1",
        "--yes",
        runner=BenchDocker(clock),
        clock=clock,
        probe=lambda u, t: ProbeResult(404),
    )
    assert "1/1" in leaderboard(result) and "| 1 | ok | 10.0s | 404 |" in result.stdout


def test_checkout_failure_is_stage_checkout_exit_2(repo):
    clock = Clock()
    runner = BenchDocker(clock, git_fail="clone")
    # preflight also clones, so it fails first with a usage error
    result = bench(repo, "--yes", runner=runner, clock=clock)
    assert result.exit_code == 2 and "git clone failed" in result.stderr
    assert "build" not in runner.verbs()


def test_trial_checkout_failure_after_a_good_review(repo):
    """The review clone succeeds, the timed checkout then fails: reported per run."""
    clock = Clock()

    class FlakyClone(BenchDocker):
        clones = 0

        def __call__(self, args, **kw):
            if args[0] == "git" and "clone" in args:
                type(self).clones += 1
                if type(self).clones > 1:
                    self.git_fail = "clone"
                else:
                    self.git_fail = None
            return super().__call__(args, **kw)

    result = bench(repo, "--runs", "2", "--yes", runner=FlakyClone(clock), clock=clock)
    assert result.exit_code == 2
    assert "- Run 1: stage `checkout`:" in result.stdout and "simulated failure" in result.stdout
    assert "0/2" in leaderboard(result)


# ---- cleanup on interruption ------------------------------------------------------------


def test_interrupt_during_build_cleans_up_and_still_prints_the_row(repo):
    clock = Clock()
    runner = BenchDocker(clock, durations=(10.0,), build_raise={2: KeyboardInterrupt()})
    result = bench(repo, "--runs", "3", "--yes", runner=runner, clock=clock)
    # launch() turns a build interrupt into an interrupted outcome
    assert result.exit_code == 130
    assert "1/2" in leaderboard(result) and "- Run 2: stage `interrupted`:" in result.stdout
    assert "stopped by an interrupt" in result.stdout and "2 of 3 requested" in result.stdout
    assert runner.verbs().count("down") == 2, "interrupted trial is cleaned up too"
    assert runner.builds == 2, "no third trial after an interrupt"
    assert leftovers(repo) == []


def test_interrupt_while_waiting_for_readiness_cleans_up(repo):
    clock = Clock()
    runner = BenchDocker(clock)

    def probe(url, t):
        raise KeyboardInterrupt

    result = bench(repo, "--runs", "3", "--yes", runner=runner, clock=clock, probe=probe)
    assert result.exit_code == 130
    assert "0/1" in leaderboard(result)
    assert runner.verbs().count("down") == 1
    assert any(c[:3] == ["docker", "image", "rm"] for c in runner.calls)


def test_interrupt_during_checkout_removes_the_temp_directory(repo):
    clock = Clock()

    class InterruptTrialClone(BenchDocker):
        clones = 0

        def __call__(self, args, **kw):
            if args[0] == "git" and "clone" in args:
                type(self).clones += 1
                self.git_raise = "clone" if type(self).clones > 1 else None
            return super().__call__(args, **kw)

    runner = InterruptTrialClone(clock)
    result = bench(repo, "--runs", "2", "--yes", runner=runner, clock=clock)
    assert result.exit_code == 130
    assert leftovers(repo) == []
    assert "build" not in runner.verbs()


def test_cleanup_touches_only_this_trials_project_and_image(repo):
    clock = Clock()
    runner = BenchDocker(clock, build_rc={1: 1})
    bench(repo, "--runs", "2", "--yes", runner=runner, clock=clock)
    banned = {"-v", "--volumes", "--rmi", "prune", "--remove-orphans", "-f"}
    removals = [c for c in runner.calls if c[:3] == ["docker", "image", "rm"]]
    downs = [c for c in runner.calls if c[0] == "docker" and c[-1] == "down"]
    assert len(downs) == len(removals) == 2
    for down, rm in zip(downs, removals, strict=True):
        project = down[down.index("-p") + 1]
        assert rm == ["docker", "image", "rm", f"r2t/{project}:latest"]
        assert not (set(down) & {"-v", "--volumes", "--rmi", "prune", "--remove-orphans"})
        assert not (set(rm) & banned)
    forbidden = [c for c in runner.calls if "prune" in c or "--rmi" in c or "system" in c]
    assert forbidden == []


# ---- review step, input checks ----------------------------------------------------------


def test_non_tty_without_yes_exits_2_and_touches_nothing(repo):
    runner = BenchDocker(Clock())
    result = bench(repo, runner=runner, tty=False)
    assert result.exit_code == 2 and "--yes" in result.stderr
    assert runner.calls == []


def test_review_is_shown_and_declining_builds_nothing(repo):
    runner = BenchDocker(Clock())
    result = bench(repo, "--runs", "2", runner=runner, tty=True, input="n\n")
    assert result.exit_code == 2
    out = result.stdout
    assert "Continue?" in out and "runs the repository's code" in out
    assert "isolation boundary" in out
    assert "build --no-cache" in out and "2 sequential trial(s)" in out
    assert "Status: supported" in out and "npm ci" in out
    assert f"Source: {repo}" in out and "r2t-bench-review" not in out
    assert not runner.verbs(), "no docker command may run before the user agrees"


def test_accepting_the_prompt_runs_and_yes_skips_it(repo):
    clock = Clock()
    result = bench(
        repo, "--runs", "1", runner=BenchDocker(clock), tty=True, input="y\n", clock=clock
    )
    assert result.exit_code == 0 and "Continue?" in result.stdout
    result = bench(repo, "--runs", "1", "--yes", runner=BenchDocker(Clock()))
    assert "Continue?" not in result.stdout


def test_not_a_git_repo_or_no_commits_exit_2(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("x")
    result = bench(plain, "--yes", runner=BenchDocker(Clock()))
    assert result.exit_code == 2 and "Git repository" in result.stderr
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    assert bench(empty, "--yes", runner=BenchDocker(Clock())).exit_code == 2
    assert bench(tmp_path / "missing", "--yes", runner=BenchDocker(Clock())).exit_code == 2


def test_repo_subdirectory_must_use_subdir(repo, tmp_path):
    outer = tmp_path / "outer"
    shutil.copytree(repo, outer / "web", ignore=shutil.ignore_patterns(".git"))
    git(outer, "init", "-q")
    git(outer, "add", ".")
    git(outer, "commit", "-qm", "x")
    runner = BenchDocker(Clock())
    result = bench(outer / "web", "--yes", runner=runner)
    assert result.exit_code == 2 and "--subdir" in result.stderr
    ok = bench(outer, "--yes", "--runs", "1", "--subdir", "web", runner=runner)
    assert ok.exit_code == 0, ok.output + ok.stderr


def test_unsupported_target_exits_3_without_any_docker_call(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    root = tmp_path / "plain-lib"
    root.mkdir()
    (root / "README.md").write_text("nothing runnable")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-qm", "x")
    runner = BenchDocker(Clock())
    result = bench(root, "--yes", runner=runner)
    assert result.exit_code == 3 and "Nothing to benchmark" in result.stderr
    assert runner.verbs() == []


def test_docker_unavailable_is_an_environment_error_not_a_failed_trial(repo):
    class NoDocker(BenchDocker):
        def __call__(self, args, **kw):
            if args[:2] == ["docker", "info"]:
                return RunResult(list(args), 1, "", "Cannot connect")
            return super().__call__(args, **kw)

    result = bench(repo, "--yes", runner=NoDocker(Clock()))
    assert result.exit_code == 4 and "daemon" in result.stderr
    assert "Successful runs" not in result.stdout


def test_stdout_is_markdown_and_progress_goes_to_stderr(repo):
    clock = Clock()
    result = bench(repo, "--runs", "1", "--yes", runner=BenchDocker(clock), clock=clock)
    assert "Run 1/1" in result.stderr and "run 1: ok in 10.0s" in result.stderr
    assert "Run 1/1" not in result.stdout
    assert "\x1b" not in result.stdout


# ---- method and runner details ----------------------------------------------------------


def test_report_records_cache_policy_and_runner_details(repo):
    clock = Clock()
    result = bench(repo, "--runs", "1", "--yes", runner=BenchDocker(clock), clock=clock)
    out = result.stdout
    assert "docker compose build --no-cache" in out
    assert "Base images are not pulled or cleared per trial and may be pre-pulled" in out
    assert "Timer: starts before the temporary checkout" in out
    assert "Docker Engine 27.3.1 (8 CPUs, 16.0 GiB visible to Docker), Docker Compose 2.30.0" in out
    assert "- Runner:\n  - r2t " in out and "Python" in out and "  - Host:" in out
    assert head(repo) in out


def test_markdown_escapes_untrusted_text():
    report = BenchReport("a|b\x1b[31m", "f" * 40, False, 1, 180.0)
    report.trials.append(
        Trial(1, False, 1.0, stage="build", reason="line with | pipe and `ticks` \x1b[0m")
    )
    text = render_markdown(report)
    assert "\x1b" not in text
    assert "a\\|b" in text
    assert "n/a" in text


def test_exit_code_follows_the_first_failure():
    r = BenchReport("x", "a" * 40, False, 3, 1.0)
    assert r.exit_code == 0
    r.trials += [
        Trial(1, True, 1.0),
        Trial(2, False, 1.0, stage="readiness"),
        Trial(3, False, 1.0, stage="build"),
    ]
    assert r.exit_code == 6 and r.median == 1.0


def test_median_of_even_count_averages_the_middle_pair():
    r = BenchReport("x", "a" * 40, False, 4, 1.0)
    r.trials += [Trial(i, True, t) for i, t in enumerate([10.0, 40.0, 20.0, 30.0], 1)]
    assert r.median == 25.0


def test_timer_starts_before_checkout_and_stops_at_readiness(repo):
    """Elapsed counts checkout and build, not the confirmation prompt or cleanup."""
    clock = Clock()

    class SlowClone(BenchDocker):
        def __call__(self, args, **kw):
            result = super().__call__(args, **kw)
            if args[0] == "git" and "checkout" in args:
                self.clock.now += 3.0  # checkout takes 3s
            if args[0] == "docker" and args[-1] == "down":
                self.clock.now += 500.0  # cleanup is slow but must not be counted
            return result

    runner = SlowClone(clock, durations=(10.0,))
    result = bench(repo, "--runs", "1", "--yes", runner=runner, clock=clock)
    # trial = 3s checkout + 10s build; the 500s cleanup and the review step are not counted
    assert leaderboard(result) == f"| vite-app | `{head(repo)[:12]}` | 1/1 | 13.0s |"
