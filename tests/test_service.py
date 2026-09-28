"""Transactional service: batching, rollback, idempotency, ordering, versions."""

from __future__ import annotations

import pytest

from patch_engine.models import (
    ConflictError,
    DeadlineExceeded,
    PatchParseError,
    PatchRejected,
    RejectReason,
)

from .conftest import seed_file


CHANGE_B = "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
CHANGE_X = "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n a\n-B\n+X\n c\n"
CHANGE_C = "--- a/g.txt\n+++ b/g.txt\n@@ -1,3 +1,3 @@\n p\n-q\n+Q\n r\n"


def test_successful_commit_bumps_version(svc_factory):
    svc = svc_factory()
    seed_file(svc, "f.txt", "a\nb\nc\n")
    result = svc.submit_batch([CHANGE_B], batch_id="b1")
    assert result.batch_id == "b1"
    assert svc.read_file("f.txt") == "a\nB\nc\n"
    assert svc.status("f.txt")["version"] == 1
    assert result.elapsed_ms >= 0


def test_batch_rollback_on_second_patch_failure(svc_factory):
    svc = svc_factory()
    seed_file(svc, "f.txt", "a\nb\nc\n")
    bad = "--- a/missing.txt\n+++ b/missing.txt\n@@ -5,1 +5,1 @@\n-z\n+Z\n"
    with pytest.raises(PatchRejected) as exc:
        svc.submit_batch([CHANGE_B, bad], batch_id="rollback")
    assert exc.value.reason is RejectReason.LINE_RANGE_OUT_OF_BOUNDS
    # no partial write from the first patch
    assert svc.read_file("f.txt") == "a\nb\nc\n"
    assert svc.status("f.txt")["version"] == 0
    # nothing durable recorded about the failed batch
    assert not svc.wal.contains_batch("rollback")
    assert not list(svc.storage.list_staged_state_files())


def test_parse_failure_rolls_back_entire_batch(svc_factory):
    svc = svc_factory()
    seed_file(svc, "f.txt", "a\nb\nc\n")
    with pytest.raises(PatchParseError):
        svc.submit_batch([CHANGE_B, "not a diff at all"])
    assert svc.read_file("f.txt") == "a\nb\nc\n"
    assert svc.status("f.txt")["version"] == 0


def test_repeated_patch_is_idempotent(svc_factory):
    svc = svc_factory()
    seed_file(svc, "f.txt", "a\nb\nc\n")
    svc.submit_batch([CHANGE_B], batch_id="b1")
    # same diff content, different batch id -> no-op, version unchanged
    r = svc.submit_batch([CHANGE_B], batch_id="b2")
    assert svc.status("f.txt")["version"] == 1
    assert r.applied[0].new_version == r.applied[0].old_version
    # replaying the same batch id -> same success, no double application
    r2 = svc.submit_batch([CHANGE_B], batch_id="b1")
    assert svc.read_file("f.txt") == "a\nB\nc\n"


def test_independent_patches_order_invariant(svc_factory):
    svc = svc_factory()
    seed_file(svc, "f.txt", "a\nb\nc\n")
    seed_file(svc, "g.txt", "p\nq\nr\n")
    svc.submit_batch([CHANGE_B, CHANGE_C], batch_id="order-1")
    state1 = (svc.read_file("f.txt"), svc.read_file("g.txt"))

    svc2 = svc_factory("engine2")
    seed_file(svc2, "g.txt", "p\nq\nr\n")
    seed_file(svc2, "f.txt", "a\nb\nc\n")
    svc2.submit_batch([CHANGE_C, CHANGE_B], batch_id="order-2")
    state2 = (svc2.read_file("f.txt"), svc2.read_file("g.txt"))
    assert state1 == state2 == ("a\nB\nc\n", "p\nQ\nr\n")


def test_two_patches_same_file_in_one_batch_chain(svc_factory):
    svc = svc_factory()
    seed_file(svc, "f.txt", "a\nb\nc\n")
    svc.submit_batch([CHANGE_B, CHANGE_X], batch_id="chain")
    assert svc.read_file("f.txt") == "a\nX\nc\n"
    # one batch = one revision per file, regardless of chained diffs
    assert svc.status("f.txt")["version"] == 1


def test_stale_base_version_rejected_then_retry_succeeds(svc_factory):
    svc = svc_factory()
    seed_file(svc, "f.txt", "a\nb\nc\n")
    svc.submit_batch([CHANGE_B], batch_id="v1")
    with pytest.raises(ConflictError) as exc:
        svc.submit_batch([CHANGE_X], base_versions={"f.txt": 0}, batch_id="stale")
    assert exc.value.file == "f.txt"
    assert exc.value.expected == 0 and exc.value.actual == 1
    # retry against latest version succeeds
    svc.submit_batch([CHANGE_X], base_versions={"f.txt": 1}, batch_id="fresh")
    assert svc.read_file("f.txt") == "a\nX\nc\n"


def test_deadline_budget_enforced_before_durable_write(svc_factory):
    svc = svc_factory()
    seed_file(svc, "f.txt", "a\nb\nc\n")
    with pytest.raises(DeadlineExceeded):
        svc.submit_batch([CHANGE_B], batch_id="slow", deadline_ms=0.0)
    assert svc.read_file("f.txt") == "a\nb\nc\n"
    assert not svc.wal.contains_batch("slow")


def test_metrics_track_commits(svc_factory):
    svc = svc_factory()
    seed_file(svc, "f.txt", "a\nb\nc\n")
    svc.submit_batch([CHANGE_B], batch_id="m1")
    assert svc.metrics["batches_committed"] == 1
    assert svc.metrics["last_elapsed_ms"] >= 0
    bad = "--- a/x.txt\n+++ b/x.txt\n@@ -9,1 +9,1 @@\n-z\n+Z\n"
    with pytest.raises(PatchRejected):
        svc.submit_batch([bad], batch_id="m2")
    assert svc.metrics["batches_rolled_back"] == 1


def test_create_and_delete_file(svc_factory):
    svc = svc_factory()
    create = "--- /dev/null\n+++ n.txt\n@@ -0,0 +1,2 @@\n+hi\n+there\n"
    svc.submit_batch([create], batch_id="new")
    assert svc.read_file("n.txt") == "hi\nthere\n"
    delete = "--- a/n.txt\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-hi\n-there\n"
    svc.submit_batch([delete], batch_id="del")
    assert svc.status("n.txt")["exists"] is False
    assert not svc.storage.working_file_exists("n.txt")
