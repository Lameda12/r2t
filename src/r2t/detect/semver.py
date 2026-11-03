"""A small npm-semver range parser: just enough to pick a Node major version.

Supports `>=`, `>`, `<=`, `<`, `=`, `^`, `~`, `x`/`X`/`*` wildcards, partial versions,
hyphen ranges, space-separated AND sets, and `||`. Anything else raises RangeError.
"""

from __future__ import annotations

import re

Version = tuple[int, int, int]
INF: Version = (10**9, 0, 0)
_PART = r"(?:\d+|[xX*])"
_VER = re.compile(
    rf"^[vV=]?\s*({_PART})(?:\.({_PART}))?(?:\.({_PART}))?(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class RangeError(ValueError):
    pass


def _parse_partial(text: str) -> tuple[int | None, int | None, int | None]:
    m = _VER.match(text.strip())
    if not m:
        raise RangeError(f"cannot parse version {text!r}")
    out: list[int | None] = []
    for part in m.groups():
        out.append(None if part is None or part in "xX*" else int(part))
    # a wildcard truncates everything after it
    for i, v in enumerate(out):
        if v is None:
            out[i:] = [None] * (3 - i)
            break
    return out[0], out[1], out[2]


def _bump(p: tuple[int | None, int | None, int | None]) -> Version:
    """Exclusive upper bound of a partial version (18 -> 19.0.0, 18.2 -> 18.3.0)."""
    major, minor, patch = p
    if major is None:
        return INF
    if minor is None:
        return (major + 1, 0, 0)
    if patch is None:
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def _floor(p: tuple[int | None, int | None, int | None]) -> Version:
    return (p[0] or 0, p[1] or 0, p[2] or 0)


def _comparator(text: str) -> tuple[Version, Version]:
    """Return a half-open interval [lo, hi) for one comparator."""
    text = text.strip()
    if text in {"", "*", "x", "X"}:
        return (0, 0, 0), INF
    m = re.match(r"^(>=|<=|>|<|=|\^|~>?)?\s*(.+)$", text)
    if not m:
        raise RangeError(f"cannot parse comparator {text!r}")
    op, rest = m.group(1) or "", m.group(2)
    p = _parse_partial(rest)
    lo, hi = _floor(p), _bump(p)
    if op in {"", "="}:
        return lo, hi
    if op == ">=":
        return lo, INF
    if op == ">":
        return hi, INF
    if op == "<":
        return (0, 0, 0), lo
    if op == "<=":
        return (0, 0, 0), hi
    if op in {"~", "~>"}:
        major, minor, _ = p
        if major is None:
            return (0, 0, 0), INF
        if minor is None:
            return (major, 0, 0), (major + 1, 0, 0)
        return lo, (major, minor + 1, 0)
    if op == "^":
        major, minor, patch = p
        if major is None:
            return (0, 0, 0), INF
        if major > 0 or minor is None:
            return lo, (major + 1, 0, 0)
        if minor > 0 or patch is None:
            return lo, (0, minor + 1, 0)
        return lo, (0, 0, patch + 1)
    raise RangeError(f"unsupported operator {op!r}")


def _set(text: str) -> tuple[Version, Version]:
    hy = re.match(r"^\s*(\S+)\s+-\s+(\S+)\s*$", text)
    if hy:
        lo = _floor(_parse_partial(hy.group(1)))
        hi = _bump(_parse_partial(hy.group(2)))
        return lo, hi
    # glue "op version" written with a space, e.g. ">= 18"
    glued = re.sub(r"(>=|<=|>|<|=|\^|~>?)\s+", r"\1", text.strip())
    lo, hi = (0, 0, 0), INF
    for token in glued.split():
        t_lo, t_hi = _comparator(token)
        lo, hi = max(lo, t_lo), min(hi, t_hi)
    return lo, hi


def parse_range(text: str) -> list[tuple[Version, Version]]:
    """Parse a range into a union of half-open intervals."""
    text = text.strip()
    if not text:
        raise RangeError("empty range")
    if len(text) > 200:
        raise RangeError("range too long")
    return [(lo, hi) for lo, hi in (_set(part) for part in text.split("||")) if lo < hi]


def intersect(
    a: list[tuple[Version, Version]], b: list[tuple[Version, Version]]
) -> list[tuple[Version, Version]]:
    """Intersection of two unions of half-open intervals (empty list means unsatisfiable)."""
    out = []
    for lo1, hi1 in a:
        for lo2, hi2 in b:
            lo, hi = max(lo1, lo2), min(hi1, hi2)
            if lo < hi:
                out.append((lo, hi))
    return sorted(out)


def satisfies_major(intervals: list[tuple[Version, Version]], major: int) -> bool:
    lo_m, hi_m = (major, 0, 0), (major + 1, 0, 0)
    return any(max(lo, lo_m) < min(hi, hi_m) for lo, hi in intervals)
