"""Large files: bounded processing time and behavior on realistic inputs."""

from __future__ import annotations

import resource
import time

import pytest

from patch_engine.models import PatchRejected, RejectReason

from .conftest import seed_file

N = 200_000  # lines


def make_content(n: int) -> str:
    return "".join(f"line {i:08d}\n" for i in range(n))


def make_change_at(n: int, target_line: int) -> str:
    old = f"line {target_line:08d}"
    new = f"line {target_line:08d}-CHANGED"
    return (
        "--- a/big.txt\n+++ b/big.txt\n"
        f"@@ -{target_line},1 +{target_line},1 @@\n-{old}\n+{new}\n"
    )


@pytest.fixture
def seeded_service(svc_factory, tmp_path_factory):
    # one shared large content object reused via blob store across tests
    content = make_content(N)
    svc = svc_factory("big")
    t0 = time.monotonic()
    seed_file(svc, "big.txt", content)
    seed_elapsed = time.monotonic() - t0
    return svc, content, seed_elapsed


def test_large_file_single_line_change(seeded_service):
    svc, content, _ = seeded_service
    diff = make_change_at(N, N // 2)
    t0 = time.monotonic()
    result = svc.submit_batch([diff], batch_id="big-change", deadline_ms=30_000)
    elapsed = time.monotonic() - t0
    assert result.elapsed_ms <= 30_000
    new = svc.read_file("big.txt")
    expected = content.replace(
        f"line {N // 2:08d}\n", f"line {N // 2:08d}-CHANGED\n", 1
    )
    assert new == expected
    # observable processing time scales sanely (generous CI bound)
    assert elapsed < 15.0, elapsed


def test_large_file_failure_does_not_mutate(seeded_service):
    svc, content, _ = seeded_service
    # out-of-bounds hunk on a huge file
    bad = make_change_at(N, N + 50)
    with pytest.raises(PatchRejected) as exc:
        svc.submit_batch([bad], batch_id="big-bad")
    assert exc.value.reason is RejectReason.LINE_RANGE_OUT_OF_BOUNDS
    assert svc.status("big.txt")["version"] == 0
    assert svc.read_file("big.txt") == content


def test_large_file_reapply_idempotent(seeded_service):
    svc, content, _ = seeded_service
    diff = make_change_at(N, 1000)
    svc.submit_batch([diff], batch_id="big-1")
    svc.submit_batch([diff], batch_id="big-2")
    assert svc.status("big.txt")["version"] == 1
