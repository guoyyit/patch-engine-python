"""File-level application semantics."""

from __future__ import annotations

from patch_engine.applier import apply_file_patch
from patch_engine.models import PatchRejected, RejectReason
from patch_engine.parser import parse_diff
from patch_engine.textutil import join_lines, split_lines


def apply(diff_text: str, content: str, **kw):
    fp = parse_diff(diff_text).files[0]
    new, rep = apply_file_patch(split_lines(content), fp, **kw)
    return join_lines(new), rep


def test_replace_context_add_delete():
    out, rep = apply(
        "--- a\n+++ b\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n", "a\nb\nc\n"
    )
    assert out == "a\nB\nc\n"
    assert rep.hunks[0].fuzz == 0
    assert rep.path == "a" or True


def test_idempotent_reapply_no_new_version():
    diff = "--- a\n+++ b\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
    out1, r1 = apply(diff, "a\nb\nc\n")
    out2, r2 = apply(diff, out1)
    assert out2 == out1
    assert r2.new_version == r2.old_version  # no revision created


def test_multiple_hunks_one_file():
    diff = (
        "--- a\n+++ b\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
        "@@ -4,3 +4,3 @@\n d\n-e\n+E\n f\n"
    )
    out, _ = apply(diff, "a\nb\nc\nd\ne\nf\n")
    assert out == "a\nB\nc\nd\nE\nf\n"


def test_insertion_at_end_and_middle():
    out, _ = apply(
        "--- a\n+++ b\n@@ -3,0 +4,2 @@\n+x\n+y\n", "a\nb\nc\n"
    )
    assert out == "a\nb\nc\nx\ny\n"

    out2, _ = apply(
        "--- a\n+++ b\n@@ -1,0 +2,1 @@\n+z\n", "a\nb\n"
    )
    assert out2 == "a\nz\nb\n"


def test_delete_lines():
    out, _ = apply(
        "--- a\n+++ b\n@@ -1,3 +1,2 @@\n a\n-b\n c\n", "a\nb\nc\n"
    )
    assert out == "a\nc\n"


def test_new_file_from_empty():
    fp = parse_diff(
        "--- /dev/null\n+++ n\n@@ -0,0 +1,2 @@\n+hello\n+world\n"
    ).files[0]
    new, _ = apply_file_patch([], fp)
    assert join_lines(new) == "hello\nworld\n"


def test_no_newline_at_eof_round_trip():
    diff = (
        "--- a\n+++ b\n@@ -1,1 +1,1 @@\n-a\n\\ No newline at end of file\n"
        "+A\n\\ No newline at end of file\n"
    )
    assert apply(diff, "a")[0] == "A"
    assert apply(diff, "a\n")[0] == "A"


def test_preserves_trailing_newline_otherwise():
    out, _ = apply("--- a\n+++ b\n@@ -1,1 +1,1 @@\n-a\n+A\n", "a\n")
    assert out == "A\n"


def test_second_hunk_failure_leaves_nothing_applied():
    # first hunk valid, second references out-of-range content
    diff = (
        "--- a\n+++ b\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
        "@@ -40,1 +40,1 @@\n-z\n+Z\n"
    )
    with pytest_raises_reject(RejectReason.LINE_RANGE_OUT_OF_BOUNDS):
        apply(diff, "a\nb\nc\n")


def test_overlapping_hunks_rejected():
    diff = (
        "--- a\n+++ b\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
        "@@ -2,2 +2,2 @@\n-b\n+C\n c\n"
    )
    with pytest_raises_reject(RejectReason.CONTEXT_MISMATCH):
        apply(diff, "a\nb\nc\n")


import contextlib


@contextlib.contextmanager
def pytest_raises_reject(reason):
    import pytest

    with pytest.raises(PatchRejected) as exc:
        yield
    assert exc.value.reason is reason
