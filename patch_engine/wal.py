"""Append-only write-ahead commit log.

Two record kinds per batch::

    {"seq": N, "batch_id": "...", "kind": "PREPARE", "payload": {...}}
    {"seq": N+1, ...,                 "kind": "COMMIT",  "payload": {...}}

* PREPARE is written and fsync'd *before* any new blob/state is
  promoted; its payload fully describes the intended new state.
* COMMIT is written and fsync'd only after ``state.json`` (and working
  files) reached the new state.

Recovery therefore only needs three cases:

================  ====================================================
records on disk   recovery action
================  ====================================================
last batch COMMITed  state.json is authoritative; nothing to do
PREPARE w/o COMMIT   re-promote blob+state from the PREPARE payload
(partial last record) drop the trailing garbage line
================  ====================================================

Re-running PREPARE promotion is safe: blobs are content-addressed and
the state pointer promotion is atomic.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PREPARE = "PREPARE"
COMMIT = "COMMIT"


@dataclass(frozen=True)
class WalRecord:
    seq: int
    batch_id: str
    kind: str
    payload: dict[str, Any]


def encode_record(rec: WalRecord) -> str:
    return json.dumps(
        {
            "seq": rec.seq,
            "batch_id": rec.batch_id,
            "kind": rec.kind,
            "payload": rec.payload,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def parse_record(obj: dict[str, Any]) -> WalRecord:
    return WalRecord(
        seq=int(obj["seq"]),
        batch_id=str(obj["batch_id"]),
        kind=str(obj["kind"]),
        payload=dict(obj.get("payload") or {}),
    )


class WriteAheadLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        if self.path.exists():
            self._drop_torn_tail()
            for rec in self._read_all_locked():
                if rec.seq > self._seq:
                    self._seq = rec.seq

    # ---- reading -----------------------------------------------------
    def _read_all_locked(self) -> list[WalRecord]:
        if not self.path.exists():
            return []
        records: list[WalRecord] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                records.append(parse_record(json.loads(line)))
        return records

    def read_all(self) -> list[WalRecord]:
        with self._lock:
            return self._read_all_locked()

    def last_record(self) -> WalRecord | None:
        recs = self.read_all()
        return recs[-1] if recs else None

    def _drop_torn_tail(self) -> None:
        """Remove a possibly-truncated final line after a crash."""
        with open(self.path, "rb+") as f:
            data = f.read()
        if not data:
            return
        lines = data.split(b"\n")
        # Last element is '' when the file ends with newline.
        tail = lines[-1]
        bad = False
        if tail:
            try:
                json.loads(tail.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                bad = True
        if bad:
            kept = data[: len(data) - len(tail)]
            with open(self.path, "wb") as f:
                f.write(kept)
                f.flush()
                os.fsync(f.fileno())

    # ---- writing -----------------------------------------------------
    def append(self, batch_id: str, kind: str, payload: dict[str, Any]) -> WalRecord:
        with self._lock:
            self._seq += 1
            rec = WalRecord(seq=self._seq, batch_id=batch_id, kind=kind, payload=payload)
            line = encode_record(rec) + "\n"
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            return rec

    def contains_batch(self, batch_id: str) -> bool:
        return any(r.batch_id == batch_id for r in self.read_all())

    def committed_batches(self) -> set[str]:
        return {r.batch_id for r in self.read_all() if r.kind == COMMIT}
