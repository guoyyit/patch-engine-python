"""Fuzzy / nearest-match location and rejection classification."""

from __future__ import annotations

import pytest

from patch_engine.matcher import hunk_already_applied, locate_hunk
from patch_engine.models import PatchRejected, RejectReason
from patch_engine.parser import parse_diff


def h(text: str, index: int = 0):
    return parse_diff(text).files[0].hunks[index]


BASIC = "--- a\n+++ b\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"


def test_exact_declared_position():
    pos, rec = locate_hunk(["a", "b", "c", ""], h(BASIC), 0)
    assert pos == 0
    assert rec.offset == 0 and rec.fuzz == 0


def test_nearest_match_shifts_with_offset():
    pos, rec = locate_hunk(["z", "a", "b", "c", ""], h(BASIC), 0)
    assert pos == 1
    assert rec.offset == 1 and rec.fuzz == 0
    assert rec.applied_at == 2


def test_nearest_choice_is_deterministic_tie_prefers_smaller_line():
    # identical context occurs twice; tie breaks toward smaller line number
    dup = "--- a\n+++ b\n@@ -2,2 +2,2 @@\n x\n-y\n+Y\n"
    lines = ["x", "y", "q", "x", "y", ""]
    pos1, r1 = locate_hunk(lines, h(dup), 0)
    pos2, r2 = locate_hunk(lines, h(dup), 0)
    # matches at 0 and 3 are equidistant from declared position 1 -> take 0
    assert pos1 == pos2 == 0
    assert r1 == r2


def test_line_range_out_of_bounds_distinct_from_mismatch():
    hk = h("--- a\n+++ b\n@@ -10,1 +10,1 @@\n-b\n+B\n")
    with pytest.raises(PatchRejected) as exc:
        locate_hunk(["a", "b", "c", ""], hk, 2, path="f")
    assert exc.value.reason is RejectReason.LINE_RANGE_OUT_OF_BOUNDS
    assert exc.value.hunk_index == 2
    assert exc.value.file == "f"


def test_insertion_point_out_of_bounds():
    hk = h("--- a\n+++ b\n@@ -9,0 +10,1 @@\n+Z\n")
    with pytest.raises(PatchRejected) as exc:
        locate_hunk(["a", ""], hk, 0)
    assert exc.value.reason is RejectReason.LINE_RANGE_OUT_OF_BOUNDS


def test_context_mismatch_when_deletion_target_absent():
    hk = h(BASIC)
    with pytest.raises(PatchRejected) as exc:
        locate_hunk(["X", "Q", "Y", ""], hk, 0, max_fuzz=2)
    assert exc.value.reason is RejectReason.CONTEXT_MISMATCH


def test_fuzzy_requires_then_prefers_lower_fuzz():
    hk = h(BASIC)
    # one side changed -> fuzz 1
    pos, rec = locate_hunk(["X", "b", "c", ""], hk, 0, max_fuzz=2)
    assert pos == 1 and rec.fuzz == 1
    # fuzz disabled -> mismatch
    with pytest.raises(PatchRejected) as exc:
        locate_hunk(["X", "b", "c", ""], hk, 0, max_fuzz=0)
    assert exc.value.reason is RejectReason.CONTEXT_MISMATCH


def test_fuzz_two_context_lines():
    text = "--- a\n+++ b\n@@ -1,4 +1,4 @@\n c1\n c2\n-k\n+K\n c3\n"
    hk = h(text)
    lines = ["X1", "X2", "k", "c3", ""]
    with pytest.raises(PatchRejected):
        locate_hunk(lines, hk, 0, max_fuzz=1)
    pos, rec = locate_hunk(lines, hk, 0, max_fuzz=2)
    assert pos == 2 and rec.fuzz == 2


def test_already_applied_detection():
    hk = h(BASIC)
    assert hunk_already_applied(["a", "B", "c", ""], hk)
    assert not hunk_already_applied(["a", "b", "c", ""], hk)
    assert not hunk_already_applied(["a", "B"], hk)  # post-image truncated
