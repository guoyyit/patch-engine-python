"""Concurrent commits to the same target file."""

from __future__ import annotations

import threading

import pytest

from patch_engine.models import ConflictError

from .conftest import seed_file


def replace_diff(old_line: str, new_line: str) -> str:
    return (
        "--- a/f.txt\n+++ b/f.txt\n"
        f"@@ -1,3 +1,3 @@\n a\n-{old_line}\n+{new_line}\n c\n"
    )


def insert_diff_at_line(line_no: int, marker: str) -> str:
    return (
        "--- a/notes.txt\n+++ b/notes.txt\n"
        f"@@ -{line_no},0 +{line_no + 1},1 @@\n+{marker}\n"
    )


def test_serialized_writes_lose_nothing(svc_factory):
    """Many threads append to the same file; every insert must survive."""
    svc = svc_factory()
    seed_file(svc, "notes.txt", "base\n")
    n = 20
    errors: list[Exception] = []
    barrier = threading.Barrier(n)

    def worker(i: int):
        barrier.wait()
        try:
            # retry loop: on rejection, re-read latest and retry append at EOF
            for _attempt in range(20):
                version = svc.status("notes.txt")["version"]
                content = svc.read_file("notes.txt")
                line_no = content.count("\n")
                diff = insert_diff_at_line(line_no, f"line-{i}")
                try:
                    svc.submit_batch(
                        [diff],
                        base_versions={"notes.txt": version},
                        batch_id=f"ins-{i}-{_attempt}",
                    )
                    return
                except ConflictError:
                    continue
            errors.append(RuntimeError(f"worker {i} exhausted retries"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    final = svc.read_file("notes.txt")
    for i in range(n):
        assert f"line-{i}\n" in final, f"insertion {i} lost: {final}"
    assert svc.status("notes.txt")["version"] == n


def test_stale_writer_rejected_under_concurrency(svc_factory):
    svc = svc_factory()
    seed_file(svc, "f.txt", "a\nb\nc\n")
    base_version = svc.status("f.txt")["version"]

    win_event = threading.Event()

    def winner():
        svc.submit_batch([replace_diff("b", "B")], batch_id="winner")
        win_event.set()

    def loser():
        win_event.wait()
        # still believes the file is at v0
        with pytest.raises(ConflictError) as exc:
            svc.submit_batch(
                [replace_diff("b", "C")],
                base_versions={"f.txt": base_version},
                batch_id="loser",
            )
        assert exc.value.actual == 1

    tw = threading.Thread(target=winner)
    tl = threading.Thread(target=loser)
    tl.start()
    tw.start()
    tw.join()
    tl.join()

    # the winner's change is present; the loser must never silently overwrite
    assert svc.read_file("f.txt") == "a\nB\nc\n"


def test_parallel_independent_files_all_commit(svc_factory):
    svc = svc_factory()
    for i in range(10):
        seed_file(svc, f"d/f{i}.txt", "x\ny\nz\n")

    errors: list[Exception] = []

    def worker(i: int):
        try:
            diff = (
                f"--- a/d/f{i}.txt\n+++ b/d/f{i}.txt\n"
                "@@ -1,3 +1,3 @@\n x\n-y\n+Y\n z\n"
            )
            svc.submit_batch([diff], batch_id=f"b-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    for i in range(10):
        assert svc.read_file(f"d/f{i}.txt") == "x\nY\nz\n"
