import pytest

from r2t.detect.semver import RangeError, parse_range, satisfies_major


def node_versions_for(text):
    intervals = parse_range(text)
    return {m for m in range(10, 40) if satisfies_major(intervals, m)}


@pytest.mark.parametrize(
    "text,expected_members,expected_non_members",
    [
        ("22", {22}, {20, 21, 23, 24}),
        ("v22.1.0", {22}, {21, 23}),
        ("22.x", {22}, {21, 23}),
        (">=18", {18, 22, 24, 26, 39}, {16, 17}),
        (">=18 <21", {18, 19, 20}, {17, 21, 22}),
        ("^20.19.0 || >=22.12.0", {20, 22, 24, 26}, {18, 19, 21}),
        ("^18.17.0", {18}, {17, 19}),
        ("~20.9", {20}, {19, 21}),
        ("^0.12", set(), {12, 13}),
        ("*", {10, 22, 39}, set()),
        ("x", {22}, set()),
        (">= 20", {20, 24}, {19}),
        ("20 - 22", {20, 21, 22}, {19, 23}),
        ("<23", {10, 22}, {23, 24}),
        ("=24.1.2", {24}, {23, 25}),
        (">22", {23, 24}, {21, 22}),  # npm: a partial ">22" means ">=23.0.0"
        ("^22.0.0 || ^24.0.0", {22, 24}, {23, 26}),
    ],
)
def test_ranges(text, expected_members, expected_non_members):
    majors = node_versions_for(text)
    assert expected_members <= majors
    assert not (expected_non_members & majors)


@pytest.mark.parametrize("text", ["", "lts/*", "node", "latest", "foo", ">=", "1.2.3.4", "a || b"])
def test_unparseable(text):
    with pytest.raises(RangeError):
        node_versions_for(text)


def test_intersect_uses_full_versions():
    from r2t.detect.semver import intersect

    assert intersect(parse_range("22.1.0"), parse_range(">=22.5.0")) == []
    assert intersect(parse_range("22"), parse_range(">=22.5.0")) == [((22, 5, 0), (23, 0, 0))]
    assert intersect(parse_range("^22 || ^24"), parse_range(">=24.2.0")) == [
        ((24, 2, 0), (25, 0, 0))
    ]
