"""Unified-diff parser: headers, ranges, context, add/delete lines, errors."""

from __future__ import annotations

import pytest

from patch_engine.models import PatchParseError, RejectReason
from patch_engine.parser import parse_diff


def test_basic_header_and_hunk():
    text = (
        "diff --git a/f.txt b/f.txt\n"
        "--- a/f.txt\t2024-01-01 00:00:00\n"
        "+++ b/f.txt\t2024-01-01 00:00:00\n"
        "@@ -1,3 +1,3 @@ section heading\n"
        " a\n"
        "-b\n"
        "+B\n"
        " c\n"
    )
    patch = parse_diff(text)
    assert len(patch.files) == 1
    fp = patch.files[0]
    assert fp.old_path == "f.txt"
    assert fp.new_path == "f.txt"
    h = fp.hunks[0]
    assert (h.old_start, h.old_count, h.new_start, h.new_count) == (1, 3, 1, 3)
    assert h.section_heading == "section heading"
    assert [(l.kind, l.text) for l in h.lines] == [
        (" ", "a"),
        ("-", "b"),
        ("+", "B"),
        (" ", "c"),
    ]


def test_multiple_files_and_hunks():
    text = (
        "--- a\n+++ b\n@@ -1,1 +1,1 @@\n-x\n+X\n"
        "--- c\n+++ d\n@@ -10,0 +11,2 @@\n+p\n+q\n"
        "@@ -20,1 +22,1 @@\n-m\n+M\n"
    )
    p = parse_diff(text)
    assert [f.target for f in p.files] == ["b", "d"]
    assert len(p.files[1].hunks) == 2
    h = p.files[1].hunks[0]
    assert h.old_count == 0 and h.new_count == 2


def test_new_and_deleted_file_markers():
    p = parse_diff(
        "new file mode 100644\n--- /dev/null\n+++ b/n\n@@ -0,0 +1,1 @@\n+hi\n"
    )
    assert p.files[0].is_new_file
    assert p.files[0].old_path == "/dev/null"
    assert p.files[0].target == "n"

    p2 = parse_diff(
        "deleted file mode 100644\n--- a/gone\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-bye\n"
    )
    assert p2.files[0].is_deleted_file
    assert p2.files[0].new_path == "/dev/null"
    assert p2.files[0].target == "gone"


def test_no_newline_markers():
    p = parse_diff(
        "--- a\n+++ b\n@@ -1,1 +1,1 @@\n-a\n\\ No newline at end of file\n"
        "+b\n\\ No newline at end of file\n"
    )
    h = p.files[0].hunks[0]
    assert h.old_no_newline and h.new_no_newline


@pytest.mark.parametrize(
    "text,reason",
    [
        ("", RejectReason.EMPTY_PATCH),
        ("   \n", RejectReason.EMPTY_PATCH),
        ("garbage with no headers\n", RejectReason.EMPTY_PATCH),
        ("--- a\n+++ b\n@@ -1,2 +1,2 @@\n only-one-old\n",
         RejectReason.MALFORMED_HUNK),
        ("@@ -1,1 +1,1 @@\n-a\n+A\n", RejectReason.MALFORMED_HEADER),
        ("--- a\n+++ b\n@@ -bogus @@\n", RejectReason.MALFORMED_HUNK),
        ("+++ b\n@@ -1,1 +1,1 @@\n", RejectReason.MALFORMED_HEADER),
        ("--- a\n+++ b\n@@ -1,1 +1,1 @@\n?bad line\n",
         RejectReason.MALFORMED_HUNK),
    ],
)
def test_parse_errors_are_distinguishable(text, reason):
    with pytest.raises(PatchParseError) as exc:
        parse_diff(text)
    assert exc.value.reason is reason


def test_error_carries_source_line():
    with pytest.raises(PatchParseError) as exc:
        parse_diff("--- a\n+++ b\n@@ -1,2 +1,2 @@\n only-one\n")
    assert exc.value.line == 3
