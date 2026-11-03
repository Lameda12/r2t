from __future__ import annotations

import os

import pytest

from r2t import snapshot
from r2t.errors import InputError
from r2t.snapshot import collect, materialize


def test_exclusions(make_tree):
    root = make_tree(
        {
            "package.json": "{}",
            "src/app.js": "x",
            ".git/config": "[core]",
            "node_modules/x/index.js": "x",
            "venv/pyvenv.cfg": "home=/usr",
            "myenv/pyvenv.cfg": "home=/usr",
            "myenv/lib/x.py": "x",
            ".venv/bin/python": "x",
            "src/__pycache__/a.pyc": "x",
            ".pytest_cache/v": "x",
            ".mypy_cache/x": "x",
            ".ruff_cache/x": "x",
            ".cache/x": "x",
            ".turbo/x": "x",
            ".next/server.js": "x",
            "dist/a.js": "x",
            "build/a.js": "x",
            "coverage/a": "x",
            "src/build/keep.js": "nested build dir is kept",
            ".env": "SECRET=1",
            ".env.local": "SECRET=1",
            ".env.production": "SECRET=1",
            ".env.example": "KEY=",
            ".env.sample": "KEY=",
            ".env.template": "KEY=",
            ".env.dist": "KEY=",
            "server.pem": "x",
            "a.key": "x",
            "a.p12": "x",
            "a.pfx": "x",
            "id_rsa": "x",
            "id_rsa.pub": "x",
            "id_ed25519": "x",
            ".ssh/known_hosts": "x",
            ".aws/credentials": "x",
            "credentials.json": "x",
            "credentials-prod.json": "x",
            "my-service-account-key.json": "x",
            ".docker/config.json": "x",
            ".docker/other.txt": "kept",
            ".npmrc": "//registry/:_authToken=abc",
            ".pypirc": "x",
            ".netrc": "x",
            "sub/.npmrc": "x",
        }
    )
    snap = collect(root)
    kept = set(snap.entries)
    assert kept == {
        "package.json",
        "src/app.js",
        "src/build/keep.js",
        ".env.example",
        ".env.sample",
        ".env.template",
        ".env.dist",
        ".docker/other.txt",
    }
    excluded = dict(snap.excluded)
    assert excluded[".npmrc"] == "registry or login config"
    assert excluded["sub/.npmrc"] == "registry or login config"
    assert excluded[".env"] == "environment file"
    assert excluded["server.pem"] == "key or credential file"
    assert excluded["myenv/"] == "virtual environment"


def test_symlinks(make_tree, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("TOPSECRET")
    root = make_tree({"real.txt": "hello", "dir/inner.txt": "i", "node_modules/m.js": "x"})
    (root / "inside-link").symlink_to("real.txt")
    (root / "dir-link").symlink_to("dir")
    (root / "escape").symlink_to("../secret.txt")
    (root / "absolute").symlink_to(str(outside))
    (root / "abs-inside").symlink_to(str(root / "real.txt"))
    (root / "dangling").symlink_to("nope.txt")
    (root / "into-nm").symlink_to("node_modules/m.js")
    (root / "loop").symlink_to("loop")
    snap = collect(root)
    assert snap.entries["inside-link"].kind == "symlink"
    assert snap.entries["inside-link"].link == "real.txt"
    assert snap.entries["dir-link"].kind == "symlink"
    assert set(snap.rejected_symlinks) == {
        "escape",
        "absolute",
        "abs-inside",
        "dangling",
        "into-nm",
        "loop",
    }
    assert snap.rejected_symlinks["escape"] == "target escapes the source root"
    dest = tmp_path / "out"
    materialize(snap, dest)
    assert (dest / "inside-link").is_symlink()
    assert os.readlink(dest / "inside-link") == "real.txt"
    assert (dest / "dir-link" / "inner.txt").read_text() == "i"
    for name in snap.rejected_symlinks:
        assert not os.path.lexists(dest / name)
    assert "TOPSECRET" not in "".join(
        p.read_text() for p in dest.rglob("*") if p.is_file() and not p.is_symlink()
    )


def test_reading_a_rejected_manifest_symlink_fails(make_tree, tmp_path):
    outside = tmp_path / "pkg.json"
    outside.write_text("{}")
    root = make_tree({"a": "1"})
    (root / "package.json").symlink_to("../pkg.json")
    snap = collect(root)
    with pytest.raises(InputError, match="symlink"):
        snap.has("package.json")
    with pytest.raises(InputError):
        snap.read_text("package.json")
    assert snap.has("other.json") is False


def test_digest_is_stable_and_content_sensitive(make_tree, tmp_path):
    files = {"a.txt": "one", "b/c.txt": "two"}
    r1 = make_tree(files, "r1")
    r2 = make_tree(files, "r2")
    d1 = collect(r1).digest()
    assert d1 == collect(r1).digest() == collect(r2).digest()
    (r2 / "b" / "c.txt").write_text("changed")
    assert collect(r2).digest() != d1
    # excluded files do not influence the digest
    (r1 / ".env").write_text("X=1")
    (r1 / "node_modules").mkdir()
    (r1 / "node_modules" / "x.js").write_text("x")
    assert collect(r1).digest() == d1
    # mode matters
    (r1 / "a.txt").chmod(0o755)
    assert collect(r1).digest() != d1


def test_digest_survives_copy(make_tree, tmp_path):
    root = make_tree({"a.txt": "one", "run.sh": "#!/bin/sh\n", "d/e.txt": "e"})
    (root / "run.sh").chmod(0o755)
    snap = collect(root)
    dest = tmp_path / "copy"
    materialize(snap, dest)
    assert collect(dest).digest() == snap.digest()
    assert os.access(dest / "run.sh", os.X_OK)


def test_large_file_skipped_and_total_refused(make_tree, monkeypatch):
    monkeypatch.setattr(snapshot, "MAX_FILE_BYTES", 10)
    monkeypatch.setattr(snapshot, "MAX_TOTAL_BYTES", 25)
    root = make_tree({"small.txt": "12345", "big.bin": "x" * 11})
    snap = collect(root)
    assert set(snap.entries) == {"small.txt"}
    assert snap.skipped_large == [("big.bin", 11)]
    root2 = make_tree({"a": "x" * 9, "b": "x" * 9, "c": "x" * 9}, "r2")
    with pytest.raises(InputError, match="exceeds"):
        collect(root2)


def test_materialize_detects_change_during_copy(make_tree, tmp_path, monkeypatch):
    root = make_tree({"a.txt": "one"})
    snap = collect(root)
    (root / "a.txt").write_text("two")
    with pytest.raises(InputError, match="changed"):
        materialize(snap, tmp_path / "copy")
