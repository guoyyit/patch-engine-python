import pytest

from patch_engine.model import FileOperation, PatchRejected
from patch_engine.parser import parse_unified_diff


def test_parse_simple_modify() -> None:
    text = (
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        " c\n"
    )
    patch = parse_unified_diff(text)
    assert len(patch.files) == 1
    fp = patch.files[0]
    assert fp.operation is FileOperation.MODIFY
    assert fp.old_path == "f.txt"
    assert fp.target_path == "f.txt"
    h = fp.hunks[0]
    assert (h.old_start, h.old_count) == (1, 3)
    assert (h.new_start, h.new_count) == (1, 3)
    kinds = [(ln.kind, ln.text) for ln in h.lines]
    assert kinds == [(" ", "a"), ("-", "b"), ("+", "B"), (" ", "c")]
    assert len(h.old_pattern) == 3
    assert len(h.new_pattern) == 3


def test_parse_git_create_delete_and_multiple_files() -> None:
    text = (
        "diff --git a/new.txt b/new.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+hello\n"
        "+world\n"
        "diff --git a/gone.txt b/gone.txt\n"
        "deleted file mode 100644\n"
        "--- a/gone.txt\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-x\n"
        "-y\n"
    )
    patch = parse_unified_diff(text)
    assert len(patch.files) == 2
    create, delete = patch.files
    assert create.operation is FileOperation.CREATE
    assert create.old_path is None and create.new_path == "new.txt"
    assert delete.operation is FileOperation.DELETE
    assert delete.old_path == "gone.txt" and delete.new_path is None


def test_parse_rename() -> None:
    text = (
        "diff --git a/old b/new\n"
        "rename from old\n"
        "rename to new\n"
    )
    from patch_engine.model import FileOperation as FO

    fp = parse_unified_diff(text).files[0]
    assert fp.operation is FO.RENAME
    assert fp.old_path == "old" and fp.new_path == "new"


def test_parse_no_newline_marker() -> None:
    text = (
        "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n"
        "-a\n"
        "\\ No newline at end of file\n"
        "+b\n"
        "\\ No newline at end of file\n"
    )
    h = parse_unified_diff(text).files[0].hunks[0]
    assert all(ln.no_newline for ln in h.lines)


def test_count_mismatch_rejected() -> None:
    text = (
        "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n"
        " a\n"
    )
    with pytest.raises(PatchRejected):
        parse_unified_diff(text)


def test_garbage_rejected() -> None:
    with pytest.raises(PatchRejected):
        parse_unified_diff("this is not a diff\n")
