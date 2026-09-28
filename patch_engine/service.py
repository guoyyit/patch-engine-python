"""Transactional patch service.

Commit protocol (all durable steps fsync'd)::

    1. parse + validate every diff        (nothing written)
    2. lock affected files (sorted)        (no deadlocks; no silent overwrite)
    3. load latest states, check base versions
    4. apply everything in memory          (rollback = discard memory)
    5. WAL.append(PREPARE, full new-state manifest)
    6. write new blobs, working files, stage state.json.<batch>
    7. os.replace state pointer
    8. WAL.append(COMMIT)

A crash at any point is resolved on the next :meth:`recover`:

* after 7 but before 8 -> PREPARE exists without COMMIT; the manifest is
  re-promoted idempotently (blobs/working files rewritten, pointer republished);
* before 7 -> no PREPARE, state.json still points at the previous revision;
  staged temp files are garbage collected.

Observability: :meth:`submit_batch` records elapsed time and each
:class:`CommitResult` carries ``elapsed_ms``; a caller-supplied
``deadline_ms`` raises :class:`DeadlineExceeded` before anything is made
durable.
"""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from .applier import apply_file_patch
from .models import (
    CommitResult,
    ConflictError,
    DeadlineExceeded,
    FileApplication,
    PatchRejected,
    RejectReason,
)
from .parser import parse_diff
from .storage import FileState, Storage
from .textutil import join_lines
from .wal import COMMIT, PREPARE, WalRecord, WriteAheadLog


class CrashInjected(RuntimeError):
    """Raised at a configured failpoint to simulate a process crash."""


class PatchService:
    def __init__(
        self,
        root: str | Path,
        *,
        max_fuzz: int = 2,
        crash_at: str | None = None,
        large_file_bytes: int = 4 * 1024 * 1024,
        stream_block_bytes: int = 1 << 20,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.storage = Storage(self.root / "store")
        self.wal = WriteAheadLog(self.root / "wal" / "log.jsonl")
        self.max_fuzz = max_fuzz
        self.crash_at = crash_at  # failpoint name, consumed on first trigger
        self.large_file_bytes = large_file_bytes
        self.stream_block_bytes = stream_block_bytes
        self._global = threading.RLock()
        self._file_locks: dict[str, threading.Lock] = {}
        self._file_locks_guard = threading.Lock()
        self.metrics: dict[str, float] = {
            "batches_committed": 0,
            "batches_rolled_back": 0,
            "batches_recovered": 0,
            "last_elapsed_ms": 0.0,
            "max_elapsed_ms": 0.0,
        }

    # ---- large-file streaming ---------------------------------------
    def _apply_file_patch_streaming(
        self, path: str, state: FileState | None, file_patch, version: int
    ) -> tuple[Path, FileApplication]:
        """Two-pass bounded-memory application; returns output file path.

        The caller streams the result into the blob store without loading it
        whole into memory.
        """
        from . import stream

        report = FileApplication(path=path, old_version=version, new_version=version)

        work_path = self.storage.work_dir / path
        # Stream the committed blob to the working file (no whole-file read).
        work_path.parent.mkdir(parents=True, exist_ok=True)
        if state is not None and self.storage.has_blob(state.blob):
            self.storage.copy_blob_to_working(
                state.blob, path, block_bytes=self.stream_block_bytes
            )
        else:
            with open(work_path, "w", encoding="utf-8", newline=""):
                pass

        n_lines = stream.count_file_lines(
            work_path, block_bytes=self.stream_block_bytes
        )
        hunks = list(file_patch.hunks)
        try:
            plans = stream.locate_streaming(
                work_path, hunks, n_lines=n_lines, max_fuzz=self.max_fuzz,
                path=path, block_bytes=self.stream_block_bytes,
            )
        except PatchRejected as exc:
            # Idempotent replay: pre-image no longer matches, but every
            # post-image is already present -> no change (content hash below
            # stays equal to the old blob, so no revision is created).
            if exc.reason is RejectReason.CONTEXT_MISMATCH and stream.hunks_already_applied(
                work_path, hunks, n_lines=n_lines,
                block_bytes=self.stream_block_bytes,
            ):
                return work_path, report
            raise
        drop_nl = any(h.new_no_newline for h in hunks)
        tmp_out = stream.rewrite_streaming(
            work_path, plans, source_lines=n_lines,
            work_dir=self.storage.work_dir, block_bytes=self.stream_block_bytes,
            drop_final_newline=drop_nl,
        )
        for plan in plans:
            report.hunks.append(plan.record)
        return tmp_out, report

    # ---- locking -----------------------------------------------------
    def _crash_point(self, name: str) -> None:
        if self.crash_at == name:
            self.crash_at = None  # one-shot
            raise CrashInjected(f"simulated crash at {name}")

    def _lock_for(self, path: str) -> threading.Lock:
        with self._file_locks_guard:
            lock = self._file_locks.get(path)
            if lock is None:
                lock = threading.Lock()
                self._file_locks[path] = lock
            return lock

    @contextlib.contextmanager
    def _lock_files(self, paths: list[str]):
        ordered = sorted(set(paths))
        acquired: list[threading.Lock] = []
        try:
            for p in ordered:
                lock = self._lock_for(p)
                lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    # ---- recovery ----------------------------------------------------
    def recover(self) -> dict[str, str]:
        """Reconcile state pointer and WAL after a crash/restart.

        Returns ``{"recovered": batch_id}`` for a re-promoted batch or
        ``{"recovered": ""}`` when no intervention was needed.
        """
        with self._global:
            self.storage.cleanup_staged_state()
            records = self.wal.read_all()
            committed = {r.batch_id for r in records if r.kind == COMMIT}
            pending: OrderedDict[str, WalRecord] = OrderedDict()
            for rec in records:
                if rec.kind == PREPARE and rec.batch_id not in committed:
                    pending[rec.batch_id] = rec

            result = {"recovered": ""}
            for batch_id, rec in pending.items():
                self._promote_manifest(rec.payload, batch_id)
                self.wal.append(batch_id, COMMIT, {"recovered": True})
                self.metrics["batches_recovered"] += 1
                result["recovered"] = batch_id
            return result

    def _promote_manifest(self, payload: dict, batch_id: str) -> None:
        """Idempotently materialize a PREPARE payload onto disk."""
        changes: dict = payload.get("changes", {})
        states = self.storage.load_state()

        for path, ch in sorted(changes.items()):
            digest = ch["blob"]
            if ch.get("deleted"):
                self.storage.remove_working_file(path)
                states.pop(path, None)
                continue
            self.storage.copy_blob_to_working(
                digest, path, block_bytes=self.stream_block_bytes
            )
            states[path] = FileState(
                version=int(ch["new_version"]),
                blob=digest,
                size=int(ch["size"]),
            )

        tmp = self.storage.write_state_atomic(states, batch_id)
        self.storage.commit_state(tmp)

    # ---- queries -----------------------------------------------------
    def status(self, path: str) -> dict:
        with self._global:
            states = self.storage.load_state()
            st = states.get(path)
            if st is None:
                return {"path": path, "exists": False, "version": 0, "blob": None, "size": 0}
            return {
                "path": path,
                "exists": True,
                "version": st.version,
                "blob": st.blob,
                "size": st.size,
            }

    def read_file(self, path: str) -> str:
        with self._global:
            states = self.storage.load_state()
            st = states.get(path)
            if st is None:
                raise FileNotFoundError(path)
            return self.storage.content_for_state(st)

    def current_versions(self) -> dict[str, int]:
        return {p: s.version for p, s in self.storage.load_state().items()}

    # ---- commit ------------------------------------------------------
    def submit_batch(
        self,
        diffs: list[str],
        *,
        base_versions: dict[str, int] | None = None,
        batch_id: str | None = None,
        deadline_ms: float | None = None,
    ) -> CommitResult:
        started = time.monotonic()

        def elapsed_ms() -> float:
            return (time.monotonic() - started) * 1000.0

        def check_deadline() -> None:
            if deadline_ms is not None and elapsed_ms() > deadline_ms:
                self.metrics["batches_rolled_back"] += 1
                raise DeadlineExceeded(
                    f"batch exceeded time budget of {deadline_ms} ms"
                )

        batch_id = batch_id or uuid.uuid4().hex
        if not diffs:
            raise ValueError("empty batch: at least one diff is required")

        # 1. parse everything first; malformed input rejects before any lock/IO.
        parsed = [parse_diff(d) for d in diffs]
        targets: list[str] = []
        for patch in parsed:
            for fp in patch.files:
                targets.append(fp.target)
        check_deadline()

        with self._global, self._lock_files(targets):
            states = self.storage.load_state()

            # duplicate / replayed batch -> idempotent success
            if self.wal.contains_batch(batch_id):
                committed = batch_id in self.wal.committed_batches()
                return CommitResult(
                    batch_id=batch_id,
                    applied=[],
                    elapsed_ms=elapsed_ms(),
                ) if committed else _conflict_replay(batch_id)

            # base-version (optimistic concurrency) checks
            base_versions = base_versions or {}
            for path, expected in base_versions.items():
                actual = states[path].version if path in states else 0
                if actual != int(expected):
                    raise ConflictError(
                        f"stale base version for {path!r}: expected {expected}, "
                        f"current is {actual}",
                        file=path,
                        expected=expected,
                        actual=actual,
                    )

            # 2. group file patches by target (a diff may touch several files)
            grouped: "OrderedDict[str, list]" = OrderedDict()
            for patch in parsed:
                for fp in patch.files:
                    grouped.setdefault(fp.target, []).append(fp)

            # 3. apply per file; on ANY error nothing has been persisted.
            # Large files are carried as temp output *paths* (streamed into
            # blobs later); small files as in-memory strings.
            new_contents: dict[str, str] = {}
            large_outputs: dict[str, Path] = {}
            deleted: set[str] = set()
            applied_reports: list[FileApplication] = []

            for path, file_patches in grouped.items():
                check_deadline()
                state = states.get(path)
                version = state.version if state else 0
                old_blob = state.blob if state else None
                merged_report = FileApplication(
                    path=path, old_version=version, new_version=version
                )

                if any(fp.is_deleted_file or fp.new_path == "/dev/null"
                       for fp in file_patches):
                    deleted.add(path)
                    applied_reports.append(merged_report)
                    continue

                use_stream = (
                    state is not None
                    and state.size >= self.large_file_bytes
                    and len(file_patches) == 1
                )
                try:
                    if use_stream:
                        out_path, rep = self._apply_file_patch_streaming(
                            path, state, file_patches[0], version
                        )
                        merged_report.hunks.extend(rep.hunks)
                        digest, size = self.storage.write_blob_from_file(
                            out_path, block_bytes=self.stream_block_bytes
                        )
                        try:
                            out_path.unlink()
                        except FileNotFoundError:
                            pass
                        changed = digest != old_blob
                        if changed:
                            large_outputs[path] = (digest, size)
                    else:
                        old_content = (
                            self.storage.content_for_state(state) if state else ""
                        )
                        lines = self.storage.lines_for_state(state)
                        for fp in file_patches:
                            if (fp.is_new_file or fp.old_path == "/dev/null") and state is None:
                                lines = []
                            lines, rep = apply_file_patch(
                                lines, fp,
                                max_fuzz=self.max_fuzz,
                                old_version=merged_report.new_version,
                                path_override=path,
                            )
                            merged_report.hunks.extend(rep.hunks)
                            check_deadline()
                        content = join_lines(lines)
                        changed = content != old_content
                        if changed:
                            new_contents[path] = content
                except PatchRejected as exc:
                    current = self.storage.load_state().get(path)
                    current_version = current.version if current else 0
                    if current_version != version:
                        self.metrics["batches_rolled_back"] += 1
                        raise ConflictError(
                            f"{path!r} changed concurrently (v{version} -> "
                            f"v{current_version}); retry against latest version",
                            file=path, expected=version, actual=current_version,
                        ) from exc
                    self.metrics["batches_rolled_back"] += 1
                    raise

                merged_report.new_version = version + 1 if changed else version
                applied_reports.append(merged_report)

            # 4. build the manifest + persist blobs BEFORE the PREPARE record,
            #    so recovery can always re-read every referenced blob.
            changes: dict[str, dict] = {}
            for report in applied_reports:
                path = report.path
                if path in deleted:
                    changes[path] = {
                        "deleted": True,
                        "old_version": report.old_version,
                        "new_version": 0,
                        "blob": "",
                        "size": 0,
                    }
                    continue
                # Idempotent no-op: no new blob, no state change.
                if report.new_version == report.old_version:
                    continue
                if path in large_outputs:
                    digest, size = large_outputs[path]
                else:
                    content = new_contents[path]
                    digest = self.storage.write_blob(content)
                    size = len(content.encode("utf-8"))
                changes[path] = {
                    "old_version": report.old_version,
                    "new_version": report.new_version,
                    "blob": digest,
                    "size": size,
                    "deleted": False,
                }
            check_deadline()

            payload = {
                "changes": changes,
                "base_versions": {
                    p: (states[p].version if p in states else 0)
                    for p in sorted(set(targets))
                },
            }
            self.wal.append(batch_id, PREPARE, payload)
            self._crash_point("after_prepare")

            # 5. promote working files + state atomically-ish (WAL covers crashes)
            for path, ch in changes.items():
                if ch.get("deleted"):
                    self.storage.remove_working_file(path)
                else:
                    self.storage.copy_blob_to_working(
                        ch["blob"], path, block_bytes=self.stream_block_bytes
                    )
            self._crash_point("after_workfiles")

            final_states = dict(states)
            for path, ch in changes.items():
                if ch.get("deleted"):
                    final_states.pop(path, None)
                else:
                    final_states[path] = FileState(
                        version=ch["new_version"],
                        blob=ch["blob"],
                        size=ch["size"],
                    )
            tmp = self.storage.write_state_atomic(final_states, batch_id)
            self.storage.commit_state(tmp)
            self._crash_point("after_state")

            self.wal.append(batch_id, COMMIT, {})
            self._crash_point("after_commit")
            elapsed = elapsed_ms()
            self.metrics["batches_committed"] += 1
            self.metrics["last_elapsed_ms"] = elapsed
            self.metrics["max_elapsed_ms"] = max(self.metrics["max_elapsed_ms"], elapsed)
            return CommitResult(
                batch_id=batch_id,
                applied=applied_reports,
                elapsed_ms=elapsed,
            )


def _conflict_replay(batch_id: str):
    raise ConflictError(f"batch {batch_id} was prepared but never committed; "
                        "run recover() or submit again")
