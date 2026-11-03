"""The CI workflow must actually trigger on the branches this project is developed on."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
# Branches that exist or are used for development in this repository.
DEV_BRANCHES = ["claude/trusting-goodall-ud6kjf", "feat/r2t-mvp", "main"]


def _on_block() -> list[str]:
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(r"^on:\s*$", ln))
    block = []
    for ln in lines[start + 1 :]:
        if ln and not ln.startswith((" ", "#")):
            break
        block.append(ln)
    return block


def _push_branch_patterns() -> list[str]:
    block = _on_block()
    idx = next(i for i, ln in enumerate(block) if re.match(r"^  push:\s*$", ln))
    patterns: list[str] = []
    for i, ln in enumerate(block[idx + 1 :], start=idx + 1):
        if re.match(r"^  \S", ln):  # next trigger
            break
        inline = re.match(r"^\s+branches:\s*\[(.*)\]\s*$", ln)
        if inline:
            patterns += [p.strip().strip("\"'") for p in inline.group(1).split(",") if p.strip()]
        elif re.match(r"^\s+branches:\s*$", ln):
            for item in block[i + 1 :]:
                m = re.match(r"^\s+-\s*(.+?)\s*$", item)
                if not m:
                    break
                patterns.append(m.group(1).strip("\"'"))
    return patterns


def _matches(patterns: list[str], branch: str) -> bool:
    # GitHub's `**` matches across slashes, like fnmatch's `*`
    return any(fnmatch.fnmatchcase(branch, p.replace("**", "*")) for p in patterns)


@pytest.mark.parametrize("branch", DEV_BRANCHES)
def test_push_triggers_on_the_real_development_branches(branch):
    patterns = _push_branch_patterns()
    assert patterns, "push trigger must list branches (or drop the filter)"
    assert _matches(patterns, branch), f"{branch} is not covered by push branches {patterns}"


def test_pull_requests_and_manual_runs_are_enabled():
    block = "\n".join(_on_block())
    assert re.search(r"^  pull_request:", block, re.M)
    assert re.search(r"^  workflow_dispatch:", block, re.M)


def test_permissions_stay_read_only():
    text = WORKFLOW.read_text()
    assert re.search(r"^permissions:\s*\n  contents: read\s*$", text, re.M)
