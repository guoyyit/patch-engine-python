"""Crash recovery: committed vs uncommitted batches are distinguishable."""

from __future__ import annotations

import json

import pytest

from patch_engine.service import CrashInjected, PatchService

from .conftest import seed_file

CHANGE_B = "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
CHANGE_C = "--- a/g.txt\n+++ b/g.txt\n@@ -1,3 +1,3 @@\n p\n-q\n+Q\n r\n"


@pytest.mark.parametrize(
    "failpoint,expect_applied",
    [
        ("after_prepare", True),    # PREPARE durable -> recovered forward
        ("after_workfiles", True),  # state pointer still old -> recovered forward
        ("after_state", True),      # state new, COMMIT missing -> repaired
    ],
)
def test_prepared_but_uncommitted_batch_recovers_forward(
    svc_factory, tmp_path, failpoint, expect_applied
):
    svc = PatchService(tmp_path / "eng", crash_at=failpoint)
    seed_file(svc, "f.txt", "a\nb\nc\n")
    seed_file(svc, "g.txt", "p\nq\nr\n")
    with pytest.raises(CrashInjected):
        svc.submit_batch([CHANGE_B, CHANGE_C], batch_id="crash-1")

    # simulate a fresh process
    svc2 = PatchService(tmp_path / "eng")
    result = svc2.recover()
    assert result["recovered"] == "crash-1"
    assert svc2.read_file("f.txt") == "a\nB\nc\n"
    assert svc2.read_file("g.txt") == "p\nQ\nr\n"
    assert svc2.status("f.txt")["version"] == 1
    assert "crash-1" in svc2.wal.committed_batches()
    # recovery is idempotent: a second process finds nothing pending
    svc3 = PatchService(tmp_path / "eng")
    assert svc3.recover()["recovered"] == ""
    assert svc3.read_file("f.txt") == "a\nB\nc\n"


def test_crash_after_commit_is_already_consistent(tmp_path):
    svc = PatchService(tmp_path / "eng", crash_at="after_commit")
    seed_file(svc, "f.txt", "a\nb\nc\n")
    with pytest.raises(CrashInjected):
        svc.submit_batch([CHANGE_B], batch_id="done")
    svc2 = PatchService(tmp_path / "eng")
    # COMMIT record already present; nothing to recover, content correct
    assert svc2.recover()["recovered"] == ""
    assert svc2.read_file("f.txt") == "a\nB\nc\n"
    assert "done" in svc2.wal.committed_batches()


def test_torn_wal_record_dropped_then_recovery(tmp_path):
    svc = PatchService(tmp_path / "eng")
    seed_file(svc, "f.txt", "a\nb\nc\n")
    svc.submit_batch([CHANGE_B], batch_id="ok")

    # a second batch reaches PREPARE, then a torn COMMIT line appears
    change2 = "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n a\n-B\n+X\n c\n"
    crashed = PatchService(tmp_path / "eng", crash_at="after_prepare")
    with pytest.raises(CrashInjected):
        crashed.submit_batch([change2], batch_id="pending")
    with open(crashed.wal.path, "ab") as f:
        f.write(b'{"seq": 99, "batch_id": "pending", "kind": "CO')  # torn

    svc2 = PatchService(tmp_path / "eng")
    res = svc2.recover()
    assert res["recovered"] == "pending"
    assert svc2.read_file("f.txt") == "a\nX\nc\n"
    # WAL reparsed cleanly
    kinds = [(r.batch_id, r.kind) for r in svc2.wal.read_all()]
    assert ("pending", "COMMIT") in kinds


def test_prepare_manifest_survives_process_restart(tmp_path):
    svc = PatchService(tmp_path / "eng", crash_at="after_state")
    seed_file(svc, "f.txt", "a\nb\nc\n")
    with pytest.raises(CrashInjected):
        svc.submit_batch([CHANGE_B], batch_id="b")
    # manifest content is fully described by the PREPARE payload; blobs persist
    prepare = [r for r in svc.wal.read_all() if r.kind == "PREPARE"][-1]
    blob = prepare.payload["changes"]["f.txt"]["blob"]
    assert svc.storage.has_blob(blob)

    fresh = PatchService(tmp_path / "eng")
    fresh.recover()
    assert fresh.status("f.txt")["blob"] == blob
