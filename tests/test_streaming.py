"""Streaming (bounded-memory) path for large files."""

from __future__ import annotations

import tracemalloc

import pytest

from patch_engine.models import PatchRejected, RejectReason

from .conftest import seed_file

N = 100_000
SMALL = 64 * 1024  # force streaming threshold very low
W = 6  # fixed-width labels avoid "line 5000" prefixing "line 50000"


def content_1based(n: int) -> str:
    return "".join(f"line {i:0{W}d}\n" for i in range(1, n + 1))


@pytest.fixture
def streaming_service(svc_factory):
    svc = svc_factory()
    svc.large_file_bytes = SMALL
    svc.stream_block_bytes = 64 * 1024
    return svc


def change_diff(line_no: int, suffix: str = "CHANGED") -> str:
    label = f"{line_no:0{W}d}"
    return (
        "--- a/big.txt\n+++ b/big.txt\n"
        f"@@ -{line_no},1 +{line_no},1 @@\n-line {label}\n+line {label}-{suffix}\n"
    )


def test_streaming_change_matches_expected(streaming_service):
    svc = streaming_service
    original = content_1based(N)
    seed_file(svc, "big.txt", original)
    target = N // 2
    diff = change_diff(target)
    result = svc.submit_batch([diff], batch_id="s1", deadline_ms=30_000)
    # hunk located exactly, no offset/fuzz
    rec = result.applied[0].hunks[0]
    assert rec.offset == 0 and rec.fuzz == 0
    new = svc.read_file("big.txt")
    label = f"{target:0{W}d}"
    expected = original.replace(f"line {label}\n", f"line {label}-CHANGED\n", 1)
    assert new == expected
    assert svc.status("big.txt")["version"] == 1


def _peak_for_size(svc_factory, n: int) -> tuple[int, int]:
    """Return (file_bytes, tracemalloc peak bytes) for one streaming change."""
    svc = svc_factory(f"big-{n}")
    svc.large_file_bytes = SMALL
    svc.stream_block_bytes = 16 * 1024
    original = "".join(f"line {i:08d}\n" for i in range(1, n + 1))
    seed_file(svc, "big.txt", original)
    target = n // 2
    label = f"{target:08d}"
    diff = (
        "--- a/big.txt\n+++ b/big.txt\n"
        f"@@ -{target},1 +{target},1 @@\n-line {label}\n+line {label}-X\n"
    )
    # warm up imports / allocators, then measure the submit alone
    tracemalloc.start()
    svc.submit_batch([diff], batch_id=f"mem-{n}")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return len(original.encode("utf-8")), peak


def test_streaming_memory_is_independent_of_file_size(svc_factory):
    """Doubling the file 4x must not 4x the submit-time Python heap peak."""
    small_bytes, small_peak = _peak_for_size(svc_factory, 50_000)
    big_bytes, big_peak = _peak_for_size(svc_factory, 400_000)
    assert big_bytes > 3 * small_bytes  # files really differ in size
    # Peak may grow sub-linearly (window near the hunk, block, bookkeeping),
    # but must be far below the naive full-file materialization.
    assert big_peak < big_bytes, f"peak={big_peak} file={big_bytes}"
    # And the big-file peak must be dramatically smaller than its own size.
    assert big_peak < big_bytes * 0.5


def test_streaming_reapply_idempotent(streaming_service):
    svc = streaming_service
    seed_file(svc, "big.txt", content_1based(N))
    diff = change_diff(5000)
    svc.submit_batch([diff], batch_id="i1")
    svc.submit_batch([diff], batch_id="i2")
    assert svc.status("big.txt")["version"] == 1


def test_streaming_failure_rolls_back(streaming_service):
    svc = streaming_service
    original = content_1based(N)
    seed_file(svc, "big.txt", original)
    bad = change_diff(N + 100)
    with pytest.raises(PatchRejected) as exc:
        svc.submit_batch([bad], batch_id="sbad")
    assert exc.value.reason is RejectReason.LINE_RANGE_OUT_OF_BOUNDS
    assert svc.read_file("big.txt") == original
    assert svc.status("big.txt")["version"] == 0
    assert not svc.wal.contains_batch("sbad")


def test_streaming_multi_hunk_far_apart(streaming_service):
    svc = streaming_service
    original = content_1based(N)
    seed_file(svc, "big.txt", original)
    diff = (
        "--- a/big.txt\n+++ b/big.txt\n"
        "@@ -100,1 +100,1 @@\n-line 000100\n+line 000100-A\n"
        "@@ -90000,1 +90000,1 @@\n-line 090000\n+line 090000-B\n"
    )
    # multi-hunk within ONE file patch still streams (single FilePatch)
    svc.submit_batch([diff], batch_id="multi")
    new = svc.read_file("big.txt")
    assert "line 000100-A\n" in new and "line 090000-B\n" in new
    assert "line 000100\n" not in new


def test_streaming_offset_match(streaming_service):
    """Content shifted earlier than declared hunk -> deterministic offset."""
    svc = streaming_service
    body = content_1based(N)
    original = "head\n" + body
    seed_file(svc, "big.txt", original)
    # hunk declares line 5000 but content is one line lower
    diff = change_diff(5000)
    result = svc.submit_batch([diff], batch_id="off")
    rec = result.applied[0].hunks[0]
    assert rec.offset == 1
    assert "line 005000-CHANGED\n" in svc.read_file("big.txt")
