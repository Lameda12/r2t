from __future__ import annotations

import pytest

from conftest import FIXTURES, assert_evidence_exists, run_analyze
from r2t.errors import InputError

APP = "from fastapi import FastAPI\n\napp = FastAPI()\n"
REQS = "fastapi==0.162.0\nuvicorn==0.54.0\n"


def flags(insp):
    return [(b.status, b.flag) for b in insp.blockers]


def test_fastapi_fixture():
    insp, _, _ = run_analyze(FIXTURES / "fastapi-app")
    assert insp.status == "supported"
    assert (insp.stack, insp.framework) == ("python", "fastapi")
    assert insp.base_image == "python:3.12-slim-bookworm"
    assert insp.install_argv == ["pip", "install", "-r", "requirements.txt"]
    assert insp.start_argv == [
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    assert insp.lock_status == "direct pins only (transitive dependencies unlocked)"
    assert insp.container_port == 8000
    refs = {e.ref() for d in insp.detections for e in d.evidence}
    assert "app/main.py:3" in refs and ".python-version" in refs
    assert_evidence_exists(insp, FIXTURES / "fastapi-app")


@pytest.mark.parametrize(
    "reqs,expected",
    [
        ("fastapi==1.0\nuvicorn==2.0\n", "direct pins only (transitive dependencies unlocked)"),
        ("fastapi>=1.0\nuvicorn==2.0\n", "unlocked"),
        ("fastapi==1.0\nuvicorn==2.*\n", "unlocked"),
        ("fastapi==1.0 --hash=sha256:aa\nuvicorn==2.0 --hash=sha256:bb\n", "locked"),
        ("fastapi==1.0 \\\n    --hash=sha256:aa\nuvicorn==2.0 --hash=sha256:bb\n", "locked"),
        (
            "fastapi==1.0 --hash=sha256:aa\nuvicorn==2.0\n",
            "direct pins only (transitive dependencies unlocked)",
        ),
        ("fastapi==1.0\nuvicorn==2.0\n-e .\n", "unlocked"),
    ],
)
def test_lock_status(make_tree, reqs, expected):
    root = make_tree({"requirements.txt": reqs, "main.py": APP})
    insp, _, _ = run_analyze(root)
    assert insp.lock_status == expected


def test_pyproject_install(make_tree):
    pyproject = (
        '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
        '[project]\nname = "x"\nversion = "0"\nrequires-python = ">=3.11,<3.13"\n'
        'dependencies = ["fastapi>=0.100", "uvicorn[standard]"]\n'
    )
    root = make_tree({"pyproject.toml": pyproject, "main.py": APP})
    insp, _, _ = run_analyze(root)
    assert insp.status == "supported", insp.blockers
    assert insp.install_argv == ["pip", "install", "."]
    assert insp.base_image == "python:3.12-slim-bookworm"  # highest in >=3.11,<3.13
    assert insp.lock_status == "unlocked"
    assert any(s.name == "build-backend" for s in insp.lifecycle)
    assert_evidence_exists(insp, root)


def test_requirements_win_over_pyproject_with_assumption(make_tree):
    pyproject = (
        '[build-system]\nrequires = ["setuptools"]\n[project]\nname = "x"\nversion = "0"\n'
        'dependencies = ["fastapi"]\n'
    )
    root = make_tree({"pyproject.toml": pyproject, "requirements.txt": REQS, "main.py": APP})
    insp, _, _ = run_analyze(root)
    assert insp.install_argv == ["pip", "install", "-r", "requirements.txt"]
    assert any("using requirements.txt" in a for a in insp.assumptions)


@pytest.mark.parametrize("name", ["poetry.lock", "uv.lock", "Pipfile", "Pipfile.lock"])
def test_other_tools_unsupported(make_tree, name):
    root = make_tree({"requirements.txt": REQS, "main.py": APP, name: ""})
    insp, _, _ = run_analyze(root)
    assert insp.status == "unsupported"
    assert name in insp.blockers[0].reason


def test_poetry_only_pyproject_unsupported(make_tree):
    root = make_tree(
        {"pyproject.toml": '[tool.poetry]\nname="x"\n[tool.poetry.dependencies]\nfastapi="*"\n'}
    )
    insp, _, _ = run_analyze(root)
    assert insp.status == "unsupported"
    assert "Poetry" in insp.blockers[0].reason


def test_missing_uvicorn_unsupported(make_tree):
    root = make_tree({"requirements.txt": "fastapi==0.1\n", "main.py": APP})
    insp, _, _ = run_analyze(root)
    assert insp.status == "unsupported"
    assert "no ASGI server declared" in insp.blockers[0].reason


def test_fastapi_standard_extra_counts_as_server(make_tree):
    root = make_tree({"requirements.txt": "fastapi[standard]==0.1\n", "main.py": APP})
    assert run_analyze(root)[0].status == "supported"


def test_fastapi_not_declared_unsupported(make_tree):
    root = make_tree({"requirements.txt": "flask\nuvicorn\n", "app.py": "x = 1\n"})
    insp, _, _ = run_analyze(root)
    assert insp.status == "unsupported"
    assert "fastapi is not declared" in insp.blockers[0].reason


def test_no_dependency_file(make_tree):
    root = make_tree({"setup.py": "", "main.py": APP})
    assert run_analyze(root)[0].status == "unsupported"


@pytest.mark.parametrize(
    "path,module",
    [
        ("main.py", "main"),
        ("app.py", "app"),
        ("app/main.py", "app.main"),
        ("api/main.py", "api.main"),
        ("server/core/main.py", "server.core.main"),
    ],
)
def test_entry_locations(make_tree, path, module):
    root = make_tree({"requirements.txt": REQS, path: APP})
    insp, _, _ = run_analyze(root)
    assert insp.status == "supported"
    assert insp.start_argv[1] == f"{module}:app"


def test_src_layout_uses_app_dir(make_tree):
    root = make_tree({"requirements.txt": REQS, "src/pkg/main.py": APP, "src/pkg/__init__.py": ""})
    insp, _, _ = run_analyze(root)
    assert insp.start_argv[1] == "pkg.main:app"
    assert insp.start_argv[-2:] == ["--app-dir", "src"]


def test_attribute_form_and_annotation(make_tree):
    code = "import fastapi\nfrom fastapi import FastAPI\nweb: FastAPI = FastAPI()\n"
    insp, _, _ = run_analyze(make_tree({"requirements.txt": REQS, "main.py": code}))
    assert insp.start_argv[1] == "main:web"
    code2 = "import fastapi\n\napi = fastapi.FastAPI(title='x')\n"
    insp2, _, _ = run_analyze(make_tree({"requirements.txt": REQS, "main.py": code2}, "r2"))
    assert insp2.start_argv[1] == "main:api"


def test_ambiguous_entries_list_candidates_and_flag_resolves(make_tree):
    root = make_tree({"requirements.txt": REQS, "main.py": APP, "api/main.py": APP})
    insp, _, _ = run_analyze(root)
    assert flags(insp) == [("needs_input", "--entry MODULE:ATTR")]
    assert {c["flag"] for c in insp.candidates} == {"--entry main:app", "--entry api.main:app"}
    fixed, _, _ = run_analyze(root, entry="api.main:app")
    assert fixed.status == "supported"
    assert fixed.start_argv[1] == "api.main:app"
    assert fixed.detection("entry").evidence[0].kind == "user override"


def test_ambiguous_fixture_two_apps_scenario(make_tree):
    root = make_tree({"requirements.txt": REQS, "app/main.py": APP, "main.py": APP})
    insp, _, _ = run_analyze(root)
    assert insp.status == "needs_input" and len(insp.candidates) == 2


def test_factory_pattern_needs_input_and_entry_adds_factory_flag(make_tree):
    code = "from fastapi import FastAPI\n\ndef create_app():\n    return FastAPI()\n"
    root = make_tree({"requirements.txt": REQS, "main.py": code})
    insp, _, _ = run_analyze(root)
    assert flags(insp) == [("needs_input", "--entry MODULE:ATTR")]
    assert "factory" in insp.blockers[0].reason
    fixed, _, _ = run_analyze(root, entry="main:create_app")
    assert fixed.start_argv[-1] == "--factory"
    code2 = code + "\napp = create_app()\n"
    root2 = make_tree({"requirements.txt": REQS, "main.py": code2}, "r2")
    assert run_analyze(root2)[0].status == "needs_input"
    assert "--factory" not in run_analyze(root2, entry="main:app")[0].start_argv


def test_no_candidates_needs_input(make_tree):
    root = make_tree({"requirements.txt": REQS, "main.py": "from fastapi import FastAPI\n"})
    insp, _, _ = run_analyze(root)
    assert flags(insp) == [("needs_input", "--entry MODULE:ATTR")]


def test_tests_dirs_and_syntax_errors_are_skipped(make_tree):
    root = make_tree(
        {
            "requirements.txt": REQS,
            "main.py": APP,
            "tests/main.py": APP,
            "tests/app.py": APP,
            "broken/main.py": "def (:\n",
        }
    )
    insp, _, _ = run_analyze(root)
    assert insp.status == "supported" and insp.start_argv[1] == "main:app"


def test_entry_override_validation(make_tree):
    root = make_tree({"requirements.txt": REQS, "main.py": APP})
    for bad in ["main", "main:", ":app", "main:app;rm", "../x:app", "-x:app", "main:1"]:
        with pytest.raises(InputError):
            run_analyze(root, entry=bad)
    with pytest.raises(InputError, match="no module file"):
        run_analyze(root, entry="nope:app")
    with pytest.raises(InputError, match="not defined"):
        run_analyze(root, entry="main:missing")


def test_target_code_is_never_executed(make_tree, tmp_path):
    marker = tmp_path / "executed"
    code = f"open({str(marker)!r}, 'w').write('x')\nfrom fastapi import FastAPI\napp = FastAPI()\n"
    run_analyze(make_tree({"requirements.txt": REQS, "main.py": code}))
    assert not marker.exists()


@pytest.mark.parametrize(
    "files,image",
    [
        ({".python-version": "3.11\n"}, "python:3.11-slim-bookworm"),
        ({".python-version": "3.13.1\n"}, "python:3.13-slim-bookworm"),
        ({"runtime.txt": "python-3.11.4\n"}, "python:3.11-slim-bookworm"),
        ({"runtime.txt": "python-3.12.1", ".python-version": "3.12"}, "python:3.12-slim-bookworm"),
        ({}, "python:3.12-slim-bookworm"),
    ],
)
def test_python_version_mapping(make_tree, files, image):
    root = make_tree({"requirements.txt": REQS, "main.py": APP, **files})
    insp, _, _ = run_analyze(root)
    assert insp.base_image == image


def test_python_version_conflict_and_override(make_tree):
    root = make_tree(
        {
            "requirements.txt": REQS,
            "main.py": APP,
            ".python-version": "3.11",
            "runtime.txt": "python-3.13.0",
        }
    )
    insp, _, _ = run_analyze(root)
    assert flags(insp) == [("needs_input", "--runtime-version 3.X")]
    assert run_analyze(root, runtime_version="3.13")[0].base_image == "python:3.13-slim-bookworm"
    old = make_tree({"requirements.txt": REQS, "main.py": APP, ".python-version": "3.9"}, "old")
    assert flags(run_analyze(old)[0]) == [("needs_input", "--runtime-version 3.X")]
    garbage = make_tree(
        {"requirements.txt": REQS, "main.py": APP, ".python-version": "pypy3.10"}, "g"
    )
    assert flags(run_analyze(garbage)[0]) == [("needs_input", "--runtime-version 3.X")]


def test_requires_python_specifier(make_tree):
    pyproject = (
        '[build-system]\nrequires = ["setuptools"]\n[project]\nname = "x"\nversion = "0"\n'
        'requires-python = ">=3.13"\ndependencies = ["fastapi", "uvicorn"]\n'
    )
    insp, _, _ = run_analyze(make_tree({"pyproject.toml": pyproject, "main.py": APP}))
    assert insp.base_image == "python:3.13-slim-bookworm"


def test_python_service_deps(make_tree):
    reqs = REQS + "psycopg2-binary==2.9.9\nredis==5.0.0\nSQLAlchemy==2.0.0\n"
    insp, _, _ = run_analyze(make_tree({"requirements.txt": reqs, "main.py": APP}))
    assert {"psycopg2-binary", "redis", "sqlalchemy"} <= {s.what for s in insp.services}


def test_custom_index_is_noted(make_tree):
    reqs = "--extra-index-url https://example.com/simple\n" + REQS
    insp, _, _ = run_analyze(make_tree({"requirements.txt": reqs, "main.py": APP}))
    assert any("custom package index" in n for n in insp.notes)


def test_unsafe_entry_symlink_fails_plan(make_tree, tmp_path):
    outside = tmp_path / "evil.py"
    outside.write_text(APP)
    root = make_tree({"requirements.txt": REQS})
    (root / "main.py").symlink_to(outside)
    with pytest.raises(InputError, match="symlink"):
        run_analyze(root)
