from __future__ import annotations

import json
import shutil

import pytest

from conftest import FIXTURES, assert_evidence_exists, run_analyze
from r2t.detect.node import ScriptUnsupported, parse_dev_script
from r2t.errors import InputError


def pkg(**extra):
    base = {"name": "x", "scripts": {"dev": "vite"}, "devDependencies": {"vite": "^8.0.0"}}
    base.update(extra)
    return json.dumps(base)


def blockers(insp):
    return [(b.status, b.flag) for b in insp.blockers]


def test_vite_fixture():
    insp, _, _ = run_analyze(FIXTURES / "vite-app")
    assert insp.status == "supported"
    assert (insp.stack, insp.framework) == ("node", "vite")
    assert insp.base_image == "node:22-bookworm-slim"  # from .nvmrc
    assert insp.package_manager == "npm" and insp.lockfile == "package-lock.json"
    assert insp.lock_status == "locked"
    assert insp.install_argv == ["npm", "ci"]
    assert insp.start_argv == [
        "npm",
        "run",
        "dev",
        "--",
        "--host",
        "0.0.0.0",
        "--port",
        "5173",
        "--strictPort",
    ]
    assert insp.container_port == 5173 and insp.readiness_path == "/"
    assert_evidence_exists(insp, FIXTURES / "vite-app")
    refs = {e.ref() for d in insp.detections for e in d.evidence}
    assert {"package.json#scripts.dev", ".nvmrc", "package-lock.json"} <= refs


def test_next_fixture():
    insp, _, _ = run_analyze(FIXTURES / "next-app")
    assert insp.status == "supported"
    assert insp.framework == "next"
    assert insp.package_manager == "pnpm" and insp.package_manager_version == "10.28.0"
    assert insp.install_argv == ["pnpm", "install", "--frozen-lockfile"]
    # pnpm must not get a `--` separator (it would be passed to the script literally)
    assert insp.start_argv == ["pnpm", "run", "dev", "-H", "0.0.0.0", "-p", "3000"]
    assert insp.container_port == 3000
    assert insp.base_image == "node:26-bookworm-slim"  # highest maintained major for >=20.9.0
    assert any("highest maintained" in a for a in insp.assumptions)
    assert_evidence_exists(insp, FIXTURES / "next-app")


def test_ambiguous_lockfiles_name_the_flag_and_flag_resolves():
    insp, _, _ = run_analyze(FIXTURES / "ambiguous-app")
    assert insp.status == "needs_input"
    assert blockers(insp) == [("needs_input", "--package-manager {npm,pnpm}")]
    for pm, install in (("npm", ["npm", "ci"]), ("pnpm", ["pnpm", "install", "--frozen-lockfile"])):
        resolved, _, _ = run_analyze(FIXTURES / "ambiguous-app", package_manager=pm)
        assert resolved.status == "supported"
        assert resolved.install_argv == install
        assert resolved.detection("package_manager").evidence[0].kind == "user override"


@pytest.mark.parametrize("lock", ["yarn.lock", "bun.lockb", "bun.lock"])
def test_yarn_bun_unsupported(make_tree, lock):
    root = make_tree({"package.json": pkg(), lock: ""})
    insp, _, _ = run_analyze(root)
    assert insp.status == "unsupported"
    assert lock in insp.blockers[0].reason


def test_package_manager_field_yarn_unsupported(make_tree):
    root = make_tree({"package.json": pkg(packageManager="yarn@4.1.0")})
    insp, _, _ = run_analyze(root)
    assert insp.status == "unsupported"


def test_package_manager_field_conflicts_with_lockfile(make_tree):
    root = make_tree({"package.json": pkg(packageManager="pnpm@9.1.0"), "package-lock.json": "{}"})
    insp, _, _ = run_analyze(root)
    assert blockers(insp) == [("needs_input", "--package-manager {npm,pnpm}")]
    fixed, _, _ = run_analyze(root, package_manager="npm")
    assert fixed.status == "supported"


def test_no_lockfile_is_unlocked_npm_install(make_tree):
    root = make_tree({"package.json": pkg()})
    insp, _, _ = run_analyze(root)
    assert insp.install_argv == ["npm", "install"]
    assert insp.lock_status == "unlocked"
    assert insp.lockfile is None


def test_pnpm_default_version_is_an_assumption(make_tree):
    root = make_tree({"package.json": pkg(), "pnpm-lock.yaml": "lockfileVersion: '9.0'\n"})
    insp, _, _ = run_analyze(root)
    assert insp.package_manager == "pnpm"
    assert insp.package_manager_version == "10.34.9"
    assert any("not pinned by the repo" in a for a in insp.assumptions)


def test_package_manager_hash_suffix_stripped(make_tree):
    root = make_tree(
        {"package.json": pkg(packageManager="pnpm@10.28.0+sha512.abcdef"), "pnpm-lock.yaml": ""}
    )
    insp, _, _ = run_analyze(root)
    assert insp.package_manager_version == "10.28.0"


def test_workspaces_unsupported(make_tree):
    root = make_tree({"package.json": pkg(workspaces=["packages/*"]), "package-lock.json": "{}"})
    insp, _, _ = run_analyze(root)
    assert insp.status == "unsupported"
    assert any("workspaces" in b.reason for b in insp.blockers)
    root2 = make_tree(
        {"package.json": pkg(), "pnpm-lock.yaml": "", "pnpm-workspace.yaml": "packages: []\n"},
        "r2",
    )
    assert run_analyze(root2)[0].status == "unsupported"


def test_subdir_lockfile_only_in_ancestor_unsupported(make_tree):
    root = make_tree({"package.json": "{}", "package-lock.json": "{}", "web/package.json": pkg()})
    insp, _, _ = run_analyze(root, subdir="web")
    assert insp.status == "unsupported"
    assert "ancestor" in insp.blockers[0].reason
    ok_root = make_tree({"web/package.json": pkg(), "web/package-lock.json": "{}"}, "ok")
    assert run_analyze(ok_root, subdir="web")[0].status == "supported"


@pytest.mark.parametrize(
    "script,framework,port",
    [
        ("vite", "vite", None),
        ("vite --port 3000", "vite", 3000),
        ("vite --port=4000 --host", "vite", 4000),
        ("vite dev --open", "vite", None),
        ("next dev", "next", None),
        ("next dev --turbopack", "next", None),
        ("next dev -p 4321", "next", 4321),
        ("next dev --port=4321 -H 127.0.0.1", "next", 4321),
        ("next", "next", None),
    ],
)
def test_dev_script_ok(script, framework, port):
    assert parse_dev_script(script) == (framework, port)


@pytest.mark.parametrize(
    "script",
    [
        'concurrently "vite" "node server.js"',
        "vite && node other.js",
        "vite; echo hi",
        "cross-env PORT=3000 vite",
        "PORT=3000 vite",
        "node server.js",
        "nodemon server.js",
        "vite build",
        "next build",
        "next start",
        "vite --port abc",
        "vite --port 99999",
        "vite > log.txt",
        "vite $(whoami)",
        "",
    ],
)
def test_dev_script_unsupported(script):
    with pytest.raises(ScriptUnsupported):
        parse_dev_script(script)


def test_unsupported_dev_script_shows_the_script(make_tree):
    root = make_tree({"package.json": pkg(scripts={"dev": "concurrently 'a' 'b'"})})
    insp, _, _ = run_analyze(root)
    assert insp.status == "unsupported"
    assert "concurrently" in insp.blockers[0].reason
    assert insp.blockers[0].evidence[0].ref() == "package.json#scripts.dev"


def test_missing_dev_script_unsupported(make_tree):
    root = make_tree({"package.json": json.dumps({"name": "x", "scripts": {"start": "node ."}})})
    insp, _, _ = run_analyze(root)
    assert insp.status == "unsupported"


def test_script_port_is_used(make_tree):
    root = make_tree({"package.json": pkg(scripts={"dev": "vite --port 3001"})})
    insp, _, _ = run_analyze(root)
    assert insp.container_port == 3001
    assert insp.start_argv[-2:] == ["3001", "--strictPort"]


def test_invalid_package_json(make_tree):
    root = make_tree({"package.json": "{not json"})
    assert run_analyze(root)[0].status == "unsupported"


def test_lifecycle_scripts_listed_with_evidence(make_tree):
    scripts = {
        "dev": "vite",
        "preinstall": "echo pre",
        "postinstall": "node setup.js",
        "prepare": "husky",
        "predev": "node gen.js",
        "build": "vite build",
    }
    root = make_tree({"package.json": pkg(scripts=scripts)})
    insp, _, _ = run_analyze(root)
    found = {(s.name, s.when): s.evidence.ref() for s in insp.lifecycle}
    assert found[("preinstall", "install")] == "package.json#scripts.preinstall"
    assert ("postinstall", "install") in found and ("prepare", "install") in found
    assert ("predev", "start") in found and ("dev", "start") in found
    assert ("build", "start") not in found and ("build", "install") not in found


@pytest.mark.parametrize(
    "files,image",
    [
        ({".nvmrc": "v24.1.0\n"}, "node:24.1.0-bookworm-slim"),
        ({".nvmrc": "24\n"}, "node:24-bookworm-slim"),
        ({".node-version": "22\n"}, "node:22-bookworm-slim"),
        ({"engines": ">=22 <24"}, "node:22-bookworm-slim"),
        ({"engines": ">=20.9.0"}, "node:26-bookworm-slim"),
        ({"engines": "^22.0.0 || ^24.0.0"}, "node:24-bookworm-slim"),
        ({".nvmrc": "22", "engines": ">=20"}, "node:22-bookworm-slim"),
        ({}, "node:24-bookworm-slim"),
    ],
)
def test_node_version_mapping(make_tree, files, image):
    files = dict(files)
    engines = files.pop("engines", None)
    extra = {"engines": {"node": engines}} if engines else {}
    root = make_tree({"package.json": pkg(**extra), **files})
    insp, _, _ = run_analyze(root)
    assert insp.status == "supported", insp.blockers
    assert insp.base_image == image


def test_no_node_declaration_is_an_assumption(make_tree):
    insp, _, _ = run_analyze(make_tree({"package.json": pkg()}))
    assert any("default LTS" in a for a in insp.assumptions)
    assert insp.detection("runtime").evidence[0].kind == "default"


def test_conflicting_node_versions_need_input_and_override_resolves(make_tree):
    root = make_tree({"package.json": pkg(engines={"node": ">=24"}), ".nvmrc": "22"})
    insp, _, _ = run_analyze(root)
    assert blockers(insp) == [("needs_input", "--runtime-version MAJOR")]
    assert "conflict" in insp.blockers[0].reason
    fixed, _, _ = run_analyze(root, runtime_version="24")
    assert fixed.status == "supported" and fixed.base_image == "node:24-bookworm-slim"
    assert fixed.detection("runtime").evidence[0].kind == "user override"


def test_eol_or_unparseable_node_versions_need_input(make_tree):
    eol = make_tree({"package.json": pkg(), ".nvmrc": "20"})
    insp, _, _ = run_analyze(eol)
    assert blockers(insp) == [("needs_input", "--runtime-version MAJOR")]
    assert "no maintained Node major" in insp.blockers[0].reason
    lts = make_tree({"package.json": pkg(), ".nvmrc": "lts/*"}, "lts")
    insp2, _, _ = run_analyze(lts)
    assert blockers(insp2) == [("needs_input", "--runtime-version MAJOR")]
    assert run_analyze(eol, runtime_version="20")[0].base_image == "node:20-bookworm-slim"


def test_bad_runtime_override_is_input_error(make_tree):
    with pytest.raises(InputError):
        run_analyze(make_tree({"package.json": pkg()}), runtime_version="3.12")


def test_node_service_dependencies(make_tree):
    root = make_tree({"package.json": pkg(dependencies={"@prisma/client": "5", "mongoose": "8"})})
    insp, _, _ = run_analyze(root)
    names = {s.what for s in insp.services}
    assert {"@prisma/client", "mongoose"} <= names
    assert_evidence_exists(insp, root)


def test_unsafe_manifest_symlink_fails_plan(make_tree, tmp_path):
    outside = tmp_path / "evil.json"
    outside.write_text(pkg())
    root = make_tree({"index.html": "x"})
    (root / "package.json").symlink_to(outside)
    with pytest.raises(InputError, match="symlink"):
        run_analyze(root)


def test_unsafe_lockfile_symlink_fails_plan(make_tree, tmp_path):
    outside = tmp_path / "lock.json"
    outside.write_text("{}")
    root = make_tree({"package.json": pkg()})
    (root / "package-lock.json").symlink_to(outside)
    with pytest.raises(InputError, match="symlink"):
        run_analyze(root)


def test_unrelated_escaping_symlink_is_reported_not_fatal(make_tree, tmp_path):
    (tmp_path / "secret").write_text("s")
    root = make_tree({"package.json": pkg(), "package-lock.json": "{}"})
    (root / "notes").symlink_to("../secret")
    insp, _, _ = run_analyze(root)
    assert insp.status == "supported"
    assert insp.skipped_symlinks == [{"path": "notes", "reason": "target escapes the source root"}]


def test_fixture_copy_is_unchanged_by_analysis(tmp_path):
    copy = tmp_path / "vite"
    shutil.copytree(FIXTURES / "vite-app", copy)
    before = sorted(p.relative_to(copy).as_posix() for p in copy.rglob("*"))
    run_analyze(copy)
    assert sorted(p.relative_to(copy).as_posix() for p in copy.rglob("*")) == before


# ---- full-version constraint intersection (not just majors) -------------------------------


def _node_case(make_tree, nvmrc=None, engines=None, **opts):
    files = {"package.json": pkg(**({"engines": {"node": engines}} if engines else {}))}
    if nvmrc:
        files[".nvmrc"] = nvmrc
    insp, _, _ = run_analyze(make_tree(files), **opts)
    return insp


def test_nvmrc_patch_below_engines_minimum_is_a_conflict(make_tree):
    """Regression: .nvmrc=22.1.0 with engines >=22.5.0 used to be reported as supported."""
    insp = _node_case(make_tree, nvmrc="22.1.0", engines=">=22.5.0")
    assert insp.status == "needs_input"
    assert blockers(insp) == [("needs_input", "--runtime-version MAJOR")]
    assert "conflict" in insp.blockers[0].reason
    assert insp.base_image is None


def test_conflict_is_resolved_by_the_override(make_tree):
    insp = _node_case(make_tree, nvmrc="22.1.0", engines=">=22.5.0", runtime_version="22")
    assert insp.status == "supported" and insp.base_image == "node:22-bookworm-slim"


@pytest.mark.parametrize(
    "nvmrc,engines,image",
    [
        # open-ended within the major: the floating major tag satisfies it
        ("22", ">=22.5.0", "node:22-bookworm-slim"),
        (None, ">=22.5.0 <23", "node:22-bookworm-slim"),
        (None, "^22.5.0", "node:22-bookworm-slim"),
        # an exact pin is honoured with the exact image tag
        ("22.1.0", None, "node:22.1.0-bookworm-slim"),
        ("v22.11.0", None, "node:22.11.0-bookworm-slim"),
        ("22.5.0", ">=22.5.0", "node:22.5.0-bookworm-slim"),
        ("22.6.0", ">=22.5.0 <23", "node:22.6.0-bookworm-slim"),
        # a pin in a newer major wins over a permissive range
        ("24.1.0", ">=20", "node:24.1.0-bookworm-slim"),
    ],
)
def test_image_satisfies_the_full_constraints(make_tree, nvmrc, engines, image):
    insp = _node_case(make_tree, nvmrc=nvmrc, engines=engines)
    assert insp.status == "supported", insp.blockers
    assert insp.base_image == image


def test_exact_pin_is_recorded_as_an_assumption_about_the_tag(make_tree):
    insp = _node_case(make_tree, nvmrc="22.1.0")
    assert any("22.1.0" in a and "exact" in a for a in insp.assumptions)


@pytest.mark.parametrize("engines", ["~22.1.0", "22.1.x", ">=22 <22.5.0", "<22.5.0 >=22"])
def test_bounded_range_inside_a_major_needs_input_because_a_floating_tag_may_not_fit(
    make_tree, engines
):
    insp = _node_case(make_tree, engines=engines)
    assert blockers(insp) == [("needs_input", "--runtime-version MAJOR")]
    assert "floating" in insp.blockers[0].reason
    assert _node_case(make_tree, engines=engines, runtime_version="22").status == "supported"


def test_lower_major_is_used_when_the_highest_cannot_be_satisfied(make_tree):
    insp = _node_case(make_tree, engines="^22.0.0 || ~26.1.0")
    assert insp.status == "supported" and insp.base_image == "node:22-bookworm-slim"


def test_union_with_open_top_piece_picks_highest_major(make_tree):
    insp = _node_case(make_tree, engines="^22.0.0 || >=24.2.0")
    assert insp.base_image == "node:26-bookworm-slim"
