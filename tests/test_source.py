from __future__ import annotations

import os

import pytest

from conftest import FakeRunner, git
from r2t.errors import InputError
from r2t.runner import RunResult, SubprocessRunner
from r2t.source import (
    GIT_HARDENING,
    acquire,
    parse_github_url,
    validate_ref,
    validate_subdir,
)

SHA = "a" * 40


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/Lameda12/r2t", ("Lameda12", "r2t")),
        ("https://github.com/Lameda12/r2t.git", ("Lameda12", "r2t")),
        ("https://github.com/a-b/c_d.e", ("a-b", "c_d.e")),
    ],
)
def test_url_ok(url, expected):
    assert parse_github_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b",
        "ssh://git@github.com/a/b",
        "git@github.com:a/b.git",
        "https://evil.com/a/b",
        "https://github.com.evil.com/a/b",
        "https://user@github.com/a/b",
        "https://user:pw@github.com/a/b",
        "https://github.com:443/a/b",
        "https://github.com/a/b?x=1",
        "https://github.com/a/b#frag",
        "https://github.com/a/b/tree/main",
        "https://github.com/a",
        "https://github.com/",
        "https://github.com/a/b/",
        "https://github.com/a/b c",
        "https://github.com/-x/--upload-pack=touch",
        "https://github.com/../b",
        "file:///etc/passwd",
    ],
)
def test_url_rejected(url):
    with pytest.raises(InputError):
        parse_github_url(url)


def test_ref_rejects_leading_dash_and_never_calls_git():
    runner = FakeRunner()
    for ref in ["-x", "--upload-pack=evil", "-"]:
        with pytest.raises(InputError):
            validate_ref(ref, runner)
    assert runner.calls == []


def test_ref_sha_skips_git_and_branch_uses_check_ref_format():
    runner = FakeRunner()
    validate_ref("a" * 7, runner)
    validate_ref(SHA, runner)
    assert runner.calls == []
    validate_ref("feat/x", runner)
    assert runner.calls == [["git", "check-ref-format", "--allow-onelevel", "feat/x"]]


def test_ref_invalid_per_git():
    runner = FakeRunner(lambda a: RunResult(a, 1))
    with pytest.raises(InputError):
        validate_ref("bad..ref", runner)


def test_ref_real_git_check_ref_format():
    runner = SubprocessRunner()
    validate_ref("main", runner)
    validate_ref("v1.2.3", runner)
    for bad in ["a..b", "a b", "a~b", "@{x}", "x.lock"]:
        with pytest.raises(InputError):
            validate_ref(bad, runner)


def test_subdir_validation(make_tree, tmp_path):
    root = make_tree({"apps/web/x.txt": "1", "file.txt": "x"})
    path, rel = validate_subdir(root, "apps/web")
    assert rel == "apps/web" and path.is_dir()
    for bad in ["../x", "apps/../../x", "/etc", "-x", "--help", "file.txt", "missing", ""]:
        with pytest.raises(InputError):
            validate_subdir(root, bad)


def test_subdir_symlink_escape_rejected(make_tree, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = make_tree({"a.txt": "x"})
    (root / "link").symlink_to(outside)
    with pytest.raises(InputError):
        validate_subdir(root, "link")


def test_remote_clone_arguments_are_hardened(tmp_path):
    def handler(args):
        if "rev-parse" in args:
            return RunResult(args, 0, SHA + "\n")
        return None

    runner = FakeRunner(handler)
    with acquire("https://github.com/o/r", None, runner) as acq:
        clone = runner.calls[0]
        assert acq.identity.resolved_commit == SHA
        assert acq.identity.url == "https://github.com/o/r"
        tmp_root = acq.root.parent
        assert tmp_root.exists()
    assert not tmp_root.exists(), "temporary clone must always be removed"
    assert clone[0] == "git"
    for flag in GIT_HARDENING:
        assert flag in clone
    assert "core.hooksPath=/dev/null" in clone
    assert "core.fsmonitor=false" in clone
    assert "protocol.file.allow=never" in clone
    assert "--depth" in clone and "--no-recurse-submodules" in clone
    sep = clone.index("--")
    assert clone[sep + 1] == "https://github.com/o/r"
    env = runner.envs[0]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_LFS_SKIP_SMUDGE"] == "1"
    assert env["GIT_ALLOW_PROTOCOL"] == "https"


def test_remote_clone_removed_on_exception():
    runner = FakeRunner(lambda a: RunResult(a, 0, SHA + "\n") if "rev-parse" in a else None)
    with pytest.raises(RuntimeError), acquire("https://github.com/o/r", None, runner) as acq:
        tmp_root = acq.root.parent
        raise RuntimeError("boom")
    assert not tmp_root.exists()


def test_remote_branch_ref_passes_branch_flag():
    runner = FakeRunner(lambda a: RunResult(a, 0, SHA + "\n") if "rev-parse" in a else None)
    with acquire("https://github.com/o/r", "v1.0", runner):
        pass
    clone = next(c for c in runner.calls if "clone" in c)
    assert clone[clone.index("--branch") + 1] == "v1.0"
    assert clone.index("--branch") < clone.index("--")


def test_remote_sha_ref_uses_init_and_shallow_fetch():
    runner = FakeRunner(lambda a: RunResult(a, 0, SHA + "\n") if "rev-parse" in a else None)
    with acquire("https://github.com/o/r", SHA, runner) as acq:
        assert acq.identity.requested_ref == SHA
    verbs = [
        next(a for a in c if a in {"init", "fetch", "checkout", "rev-parse"}) for c in runner.calls
    ]
    assert verbs == ["init", "fetch", "checkout", "rev-parse"]
    fetch = runner.calls[1]
    assert fetch[-3:] == ["--", "https://github.com/o/r", SHA]
    assert "--depth" in fetch
    for call in runner.calls[:3]:
        assert "core.hooksPath=/dev/null" in call


def test_clone_failure_is_input_error():
    runner = FakeRunner(lambda a: RunResult(a, 128, "", "fatal: repository not found\n"))
    with (
        pytest.raises(InputError, match="repository not found"),
        acquire("https://github.com/o/r", None, runner),
    ):
        pass


def test_ref_with_local_source_rejected(make_tree):
    root = make_tree({"a": "1"})
    with pytest.raises(InputError), acquire(str(root), "main", FakeRunner()):
        pass


def test_bad_url_never_calls_git():
    runner = FakeRunner()
    with pytest.raises(InputError), acquire("https://evil.com/a/b", None, runner):
        pass
    assert runner.calls == []


def test_local_missing_and_file(make_tree):
    root = make_tree({"f.txt": "x"})
    with pytest.raises(InputError), acquire(str(root / "nope"), None, FakeRunner()):
        pass
    with pytest.raises(InputError), acquire(str(root / "f.txt"), None, FakeRunner()):
        pass
    with pytest.raises(InputError), acquire("-x", None, FakeRunner()):
        pass


def test_local_metadata_uses_fsmonitor_off_and_readonly_commands(make_tree):
    root = make_tree({"a": "1"})
    runner = FakeRunner(
        lambda a: (
            RunResult(a, 0, "true\n")
            if "--is-inside-work-tree" in a
            else RunResult(a, 0, SHA + "\n")
            if "HEAD" in a
            else RunResult(a, 0, " M a\n")
        )
    )
    with acquire(str(root), None, runner) as acq:
        assert acq.identity.git_head == SHA
        assert acq.identity.dirty is True
        assert acq.identity.path == str(root.resolve())
    for call in runner.calls:
        assert "core.fsmonitor=false" in call
        assert "--no-optional-locks" in call
        assert not {"commit", "add", "checkout", "fetch", "clone", "gc"} & set(call)


def test_local_real_git_dirty_includes_untracked(make_tree):
    root = make_tree({"a.txt": "1"})
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    with acquire(str(root), None, SubprocessRunner()) as acq:
        assert acq.identity.dirty is False
        assert acq.identity.git_head == git(root, "rev-parse", "HEAD").strip()
    (root / "new.txt").write_text("untracked")
    with acquire(str(root), None, SubprocessRunner()) as acq:
        assert acq.identity.dirty is True


def test_local_not_git(make_tree):
    root = make_tree({"a.txt": "1"})
    with acquire(str(root), None, SubprocessRunner()) as acq:
        assert acq.identity.git_head is None
        assert acq.identity.git_note


# ---- Git clean filters must never run (real Git, no mocks) --------------------------------


def _filter_repo(make_tree, tmp_path, *, scope: str = "local"):
    """A repo with a marker-touching clean filter and a stat-dirty tracked file."""
    marker = tmp_path / "filter-ran"
    root = make_tree({"a.txt": "one\n", ".gitattributes": "* filter=demo\n"})
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    clean = f"sh -c 'touch {marker}; cat'"
    if scope == "local":
        git(root, "config", "filter.demo.clean", clean)
    # same content, new mtime: git must re-clean the file to decide whether it is modified
    os.utime(root / "a.txt", (1, 1))
    return root, marker


def test_local_identity_never_runs_configured_clean_filters(make_tree, tmp_path):
    root, marker = _filter_repo(make_tree, tmp_path)
    with acquire(str(root), None, SubprocessRunner()) as acq:
        identity = acq.identity
    assert not marker.exists(), "a clean filter from .git/config was executed"
    assert identity.dirty is None, "dirty state must be reported as unknown, not guessed"
    assert identity.git_head
    assert "filter" in (identity.git_note or "")
    assert str(marker) not in (identity.git_note or "")  # never echo configured commands


def test_filters_from_the_users_own_global_config_do_not_make_dirty_unknown(
    make_tree, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text('[filter "lfs"]\n\tclean = cat\n\trequired = false\n')
    monkeypatch.setenv("HOME", str(home))
    root = make_tree({"a.txt": "one\n"})
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    with acquire(str(root), None, SubprocessRunner()) as acq:
        assert acq.identity.dirty is False


def test_inspect_and_plan_do_not_run_filters_either(make_tree, tmp_path):
    from typer.testing import CliRunner

    from r2t.cli import Services, app

    root, marker = _filter_repo(make_tree, tmp_path)
    (root / "package.json").write_text('{"scripts": {"dev": "vite"}}')
    runner = CliRunner()
    for args in (["inspect", str(root)], ["plan", str(root), "-o", str(tmp_path / "out")]):
        result = runner.invoke(app, args, obj=Services())
        assert result.exit_code in (0, 3), result.output
    assert not marker.exists()


def test_filter_defined_through_an_included_config_file_is_also_caught(make_tree, tmp_path):
    root, marker = _filter_repo(make_tree, tmp_path, scope="none")
    extra = tmp_path / "evil.cfg"
    extra.write_text(f"[filter \"demo\"]\n\tclean = sh -c 'touch {marker}; cat'\n")
    git(root, "config", "include.path", str(extra))
    os.utime(root / "a.txt", (2, 2))
    with acquire(str(root), None, SubprocessRunner()) as acq:
        assert acq.identity.dirty is None
    assert not marker.exists()
