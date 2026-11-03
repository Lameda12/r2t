from __future__ import annotations

import json

import pytest

from conftest import FIXTURES, assert_evidence_exists, run_analyze
from r2t.detect.common import extract_env_names

REQS = "fastapi==0.162.0\nuvicorn==0.54.0\n"
APP = "from fastapi import FastAPI\n\napp = FastAPI()\n"


def vite_pkg(**extra):
    return json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "^8"}, **extra})


def test_env_name_extraction():
    text = (
        "# comment=1\nA=1\nexport B=2\n  C = 3\nlower_case=x\n1BAD=1\nNOEQUALS\n"
        'D==double\nE=\n#F=1\nexport  G="quoted value"\nH-I=1\n'
    )
    assert [n for n, _ in extract_env_names(text)] == [
        "A",
        "B",
        "C",
        "lower_case",
        "D",
        "E",
        "G",
    ]
    assert extract_env_names("A=1\n\nB=2\n") == [("A", 1), ("B", 3)]


def test_sentinel_values_never_leave_the_example_file():
    insp, _, _ = run_analyze(FIXTURES / "next-app")
    assert [e.name for e in insp.env_names] == [
        "DATABASE_URL",
        "NEXT_PUBLIC_API_KEY",
        "STRIPE_SECRET",
    ]
    dumped = json.dumps(insp.to_dict()) + repr(insp)
    assert "SECRET_SENTINEL_9f2c" not in dumped
    assert "postgres://" not in dumped
    assert_evidence_exists(insp, FIXTURES / "next-app")


def test_env_names_from_all_example_files_and_dedup(make_tree):
    root = make_tree(
        {
            "package.json": vite_pkg(),
            ".env.example": "A=1\nB=2\n",
            ".env.sample": "B=3\nC=4\n",
            ".env.template": "D=5\n",
            ".env.dist": "E=6\n",
            ".env": "REAL_SECRET=hunter2\n",
            ".env.local": "LOCAL_SECRET=hunter2\n",
        }
    )
    insp, _, _ = run_analyze(root)
    assert [e.name for e in insp.env_names] == ["A", "B", "C", "D", "E"]
    assert "hunter2" not in json.dumps(insp.to_dict())
    assert "REAL_SECRET" not in json.dumps(insp.to_dict())


@pytest.mark.parametrize(
    "name,expected",
    [
        ("DATABASE_URL", "SQL"),
        ("REDIS_URL", "Redis"),
        ("MONGODB_URI", "Mongo"),
        ("OPENAI_API_KEY", "API"),
    ],
)
def test_env_service_hints(make_tree, name, expected):
    root = make_tree({"package.json": vite_pkg(), ".env.example": f"{name}=\n"})
    insp, _, _ = run_analyze(root)
    assert any(expected in s.what for s in insp.services)
    assert any(s.evidence.ref().startswith(".env.example:") for s in insp.services)


def test_existing_container_files_listed_as_evidence_only(make_tree):
    root = make_tree(
        {
            "package.json": vite_pkg(),
            "Dockerfile": "FROM scratch\n",
            "docker-compose.yml": "services:\n  web:\n    image: x\n  db:\n    image: postgres\n",
            ".devcontainer/devcontainer.json": "{}",
            "Procfile": "web: x\n",
        }
    )
    insp, _, _ = run_analyze(root)
    assert {e.file for e in insp.container_files} == {
        "Dockerfile",
        "docker-compose.yml",
        ".devcontainer/devcontainer.json",
        "Procfile",
    }
    assert {s.what for s in insp.services} >= {"service 'web'", "service 'db'"}
    assert insp.status == "supported"
    assert_evidence_exists(insp, root)


# ---- app root ambiguity ------------------------------------------------------------------


def test_root_without_app_but_subdirs_need_input_and_subdir_resolves(make_tree):
    root = make_tree(
        {
            "web/package.json": vite_pkg(),
            "web/package-lock.json": "{}",
            "backend/requirements.txt": REQS,
            "backend/main.py": APP,
            "docs/readme.md": "x",
        }
    )
    insp, _, _ = run_analyze(root)
    assert insp.status == "needs_input"
    assert insp.blockers[0].flag == "--subdir PATH"
    assert {c["flag"] for c in insp.candidates} == {"--subdir web", "--subdir backend"}
    web, snap, _ = run_analyze(root, subdir="web")
    assert web.status == "supported" and web.app_root == "web"
    assert web.source.subdir == "web"
    assert "package.json" in snap.entries and "backend/main.py" not in snap.entries
    be, _, _ = run_analyze(root, subdir="backend")
    assert be.status == "supported" and be.framework == "fastapi"


def test_single_subdir_candidate_still_needs_input(make_tree):
    root = make_tree({"app/package.json": vite_pkg(), "app/package-lock.json": "{}"})
    insp, _, _ = run_analyze(root)
    assert insp.status == "needs_input"
    assert [c["subdir"] for c in insp.candidates] == ["app"]


def test_candidates_inside_excluded_dirs_are_ignored(make_tree):
    root = make_tree(
        {
            "node_modules/pkg/package.json": vite_pkg(),
            "venv/pyvenv.cfg": "",
            "venv/lib/requirements.txt": REQS,
        }
    )
    insp, _, _ = run_analyze(root)
    assert insp.status == "unsupported"
    assert insp.candidates == []


def test_depth_limit_three(make_tree):
    root = make_tree({"a/b/c/package.json": vite_pkg(), "a/b/c/d/package.json": vite_pkg()})
    insp, _, _ = run_analyze(root)
    assert [c["subdir"] for c in insp.candidates] == ["a/b/c"]


def test_root_app_wins_and_notes_other_candidates(make_tree):
    root = make_tree(
        {
            "package.json": vite_pkg(),
            "package-lock.json": "{}",
            "examples/x/package.json": vite_pkg(),
        }
    )
    insp, _, _ = run_analyze(root)
    assert insp.status == "supported" and insp.app_root == "."
    assert any("examples/x" in n for n in insp.notes)


def test_nothing_recognizable_is_unsupported(make_tree):
    insp, _, _ = run_analyze(make_tree({"README.md": "x", "main.go": "package main"}))
    assert insp.status == "unsupported"


def test_python_and_node_at_same_root_unsupported(make_tree):
    root = make_tree(
        {
            "package.json": vite_pkg(),
            "requirements.txt": REQS,
            "main.py": APP,
            "package-lock.json": "{}",
        }
    )
    insp, _, _ = run_analyze(root)
    assert insp.status == "unsupported"
    assert "both a Node app and a Python app" in insp.blockers[0].reason


def test_node_root_with_tooling_requirements_prefers_node(make_tree):
    root = make_tree(
        {"package.json": vite_pkg(), "package-lock.json": "{}", "requirements.txt": "black\n"}
    )
    insp, _, _ = run_analyze(root)
    assert insp.stack == "node" and insp.status == "supported"


def test_registry_config_exclusion_is_noted(make_tree):
    root = make_tree(
        {"package.json": vite_pkg(), "package-lock.json": "{}", ".npmrc": "//r/:_authToken=abc\n"}
    )
    insp, snap, _ = run_analyze(root)
    assert ".npmrc" not in snap.entries
    assert any(".npmrc" in n for n in insp.notes)
    assert "_authToken" not in json.dumps(insp.to_dict())
