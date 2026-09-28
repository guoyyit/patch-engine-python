import io

import pytest

from patch_engine.engine import ApplyStats, apply_file_patch
from patch_engine.model import PatchRejected, RejectReason
from patch_engine.parser import parse_unified_diff


def apply(text: str, diff: str, **kw):
    fp = parse_unified_diff(diff).files[0]
    lines = text.split("\n") if text != "" else []
    # split 对 "a\nb\n" 会产生末尾空串；这里统一按“带末尾换行”的行输入。
    if text.endswith("\n"):
        lines = text[:-1].split("\n")
    out = io.StringIO()
    stats = ApplyStats()
    kw.setdefault("source_trailing_newline", text.endswith("\n"))
    result = apply_file_patch(iter(lines), fp, out, stats=stats, **kw)
    return out.getvalue(), result, stats


def test_basic_replace() -> None:
    text = "a\nb\nc\n"
    diff = (
        "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n"
        " a\n-b\n+B\n c\n"
    )
    out, result, _ = apply(text, diff)
    assert out == "a\nB\nc\n"
    assert result.changed is True


def test_insert_at_end() -> None:
    text = "x\ny\n"
    diff = "--- a/f\n+++ b/f\n@@ -2,0 +3,1 @@\n+z\n"
    out, result, _ = apply(text, diff)
    assert out == "x\ny\nz\n"


def test_create_new_file() -> None:
    diff = "--- /dev/null\n+++ b/f\n@@ -0,0 +1,2 @@\n+hello\n+world\n"
    out, result, _ = apply("", diff)
    assert out == "hello\nworld\n"


def test_delete_line() -> None:
    text = "a\nb\nc\n"
    diff = "--- a/f\n+++ b/f\n@@ -1,3 +1,2 @@\n a\n-b\n c\n"
    out, result, _ = apply(text, diff)
    assert out == "a\nc\n"


def test_idempotent_reapply() -> None:
    text = "a\nB\nc\n"
    diff = (
        "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n"
        " a\n-b\n+B\n c\n"
    )
    out, result, _ = apply(text, diff)
    assert out == "a\nB\nc\n"
    assert result.changed is False


def test_context_mismatch_distinct_from_bounds() -> None:
    text = "a\nX\nc\n"
    diff = (
        "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n"
        " a\n-b\n+B\n c\n"
    )
    with pytest.raises(PatchRejected) as ei:
        apply(text, diff, fuzz_radius=0)
    assert ei.value.detail.reason is RejectReason.CONTEXT_MISMATCH


def test_range_out_of_bounds() -> None:
    text = "a\nb\n"
    diff = (
        "--- a/f\n+++ b/f\n@@ -10,2 +10,2 @@\n"
        "-b\n+B\n c\n"
    )
    with pytest.raises(PatchRejected) as ei:
        apply(text, diff, fuzz_radius=0)
    assert ei.value.detail.reason is RejectReason.HUNK_RANGE_OUT_OF_BOUNDS


def test_fuzz_shift_exact() -> None:
    # 声明位置错位（+1），半径内存在精确匹配 -> 最近匹配。
    text = "junk\na\nb\nc\n"
    diff = (
        "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n"
        " a\n-b\n+B\n c\n"
    )
    out, result, stats = apply(text, diff, fuzz_radius=5, fuzz_level=0)
    assert out == "junk\na\nB\nc\n"
    assert result.changed is True
    assert stats.fuzzed_hunks == 0


def test_fuzz_strip_edge_context() -> None:
    # 边缘上下文不一致、核心删除行一致，剥离 1 行边缘上下文后可匹配。
    text = "a\nb\nTAIL\n"
    diff = (
        "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n"
        " X\n-b\n+B\n c\n"
    )
    # fuzz_level=0：核心含边缘 -> 不匹配
    with pytest.raises(PatchRejected):
        apply(text, diff, fuzz_radius=5, fuzz_level=0)
    out, result, stats = apply(text, diff, fuzz_radius=5, fuzz_level=1)
    # 两侧各剥离 1 行上下文，只替换核心 b->B；真实上下文保持不变
    assert out == "a\nB\nTAIL\n"
    assert result.changed is True
    assert stats.fuzzed_hunks == 1


def test_fuzz_prefers_closer_and_backward() -> None:
    # 旧模式 "K\n-b\nK" 出现两次：距声明位置 -1 与 +2，应取更近且向前者。
    text = "K\nb\nK\nmid\nK\nb\nK\n"
    diff = (
        "--- a/f\n+++ b/f\n@@ -2,3 +2,3 @@\n"
        " K\n-b\n+B\n K\n"
    )
    out, _, _ = apply(text, diff, fuzz_radius=10, fuzz_level=0)
    # 声明锚点为第 2 行，第一次出现恰好在声明位置 -> 精确命中第一处
    assert out.startswith("K\nB\nK\n")


def test_hunks_overlap_rejected() -> None:
    text = "a\na\nb\n"
    diff = (
        "--- a/f\n+++ b/f\n"
        "@@ -1,2 +1,2 @@\n a\n-a\n+A\n"
        "@@ -2,2 +2,2 @@\n a\n-b\n+B\n"
    )
    with pytest.raises(PatchRejected) as ei:
        apply(text, diff, fuzz_radius=5)
    assert ei.value.detail.reason is RejectReason.HUNKS_OVERLAP


def test_no_newline_at_eof() -> None:
    diff = (
        "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n"
        "-a\n\\ No newline at end of file\n"
        "+b\n\\ No newline at end of file\n"
    )
    out, result, _ = apply("a", diff, source_trailing_newline=False)
    assert out == "b"
    assert result.trailing_newline is False


def test_large_file_bounded_memory() -> None:
    # 50 万行文件，在中部打一个补丁；校验功能正确且处理耗时有界（可观测）。
    size = 500_000
    lines = [f"line{i}" for i in range(size)]
    lines[size // 2] = "NEEDLE-OLD"
    diff = (
        "--- a/f\n+++ b/f\n"
        f"@@ -{size // 2 + 1},1 +{size // 2 + 1},1 @@\n"
        "-NEEDLE-OLD\n+NEEDLE-NEW\n"
    )
    fp = parse_unified_diff(diff).files[0]
    out = io.StringIO()
    stats = ApplyStats()
    result = apply_file_patch(
        iter(lines), fp, out, fuzz_radius=50, stats=stats
    )
    assert "NEEDLE-NEW" in out.getvalue()
    assert "NEEDLE-OLD" not in out.getvalue()
    assert stats.lines_scanned == size
    assert stats.elapsed_seconds < 10.0


def test_deadline_enforced() -> None:
    size = 200_000
    lines = [f"line{i}" for i in range(size)]
    diff = (
        "--- a/f\n+++ b/f\n"
        f"@@ -{size},1 +{size},1 @@\n"
        "-nope\n+x\n"
    )
    fp = parse_unified_diff(diff).files[0]
    out = io.StringIO()
    fake_clock = iter([0.0, 100.0])

    def clock():
        return next(fake_clock)

    with pytest.raises(PatchRejected):
        apply_file_patch(
            iter(lines),
            fp,
            out,
            fuzz_radius=50,
            deadline_seconds=1.0,
            clock=clock,
        )


def test_pure_delete_wrong_context_rejected() -> None:
    text = "x\ny\n"
    diff = "--- a/f\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-WRONG\n-y\n"
    with pytest.raises(PatchRejected) as ei:
        apply(text, diff, fuzz_radius=0)
    assert ei.value.detail.reason is RejectReason.CONTEXT_MISMATCH
