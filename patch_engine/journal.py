"""仅追加的提交日志（write-ahead journal）。

日志文件为 JSON Lines，记录类型：

* ``{"type":"pending","batch":id,"ts":...}`` —— 批次内容已暂存，准备提交；
* ``{"type":"committed","batch":id,"ts":...}`` —— 批次已原子落盘。

每次写入都 ``flush + fsync``，保证崩溃后磁盘状态与日志一致。
尾部半截记录（崩溃时正在写最后一行）在读取时被检测并截断，不会污染恢复。

恢复协议见 :meth:`Journal.recover_state`：

* 最后状态为 ``committed`` 的批次：已提交；
* 最后状态为 ``pending`` 且无对应 ``committed``：未提交（需回滚清理）。
"""

from __future__ import annotations

import enum
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


class JournalState(enum.Enum):
    PENDING = "pending"
    COMMITTED = "committed"


@dataclass(frozen=True)
class JournalRecord:
    type: str
    batch: str
    ts: float


class Journal:
    """基于单个 append-only 文件的批次日志。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---- 底层写入 -------------------------------------------------------

    def _append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        # 以追加模式打开：OS 保证 O_APPEND 的写入位置原子移动，配合文件锁
        # （由 Workspace 提供进程级串行化）使用。
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def append_pending(self, batch_id: str) -> None:
        self._append({"type": "pending", "batch": batch_id, "ts": time.time()})

    def append_committed(self, batch_id: str) -> None:
        self._append({"type": "committed", "batch": batch_id, "ts": time.time()})

    # ---- 读取与修复 -----------------------------------------------------

    def read_records(self) -> list[JournalRecord]:
        """读取全部完整记录；若尾部存在半截记录则截断文件。"""
        if not self.path.exists():
            return []
        data = self.path.read_bytes()
        if not data:
            return []
        raw_records: list[JournalRecord] = []
        valid_bytes = 0
        for chunk in data.split(b"\n"):
            if not chunk:
                # 末尾正常换行后的空元素。
                valid_bytes += 1
                continue
            try:
                obj = json.loads(chunk.decode("utf-8"))
                rec = JournalRecord(
                    type=str(obj["type"]),
                    batch=str(obj["batch"]),
                    ts=float(obj.get("ts", 0.0)),
                )
            except (ValueError, KeyError, UnicodeDecodeError):
                break
            raw_records.append(rec)
            valid_bytes += len(chunk) + 1

        if valid_bytes != len(data):
            # 发现尾部损坏：原子截断到最后一条完整记录之后。
            self._truncate(valid_bytes)
        return raw_records

    def _truncate(self, length: int) -> None:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".journal.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as out:
                with self.path.open("rb") as src:
                    out.write(src.read(length))
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp_name, self.path)
            self._fsync_dir()
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _fsync_dir(self) -> None:
        try:
            dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    # ---- 恢复视图 -------------------------------------------------------

    def recover_state(self) -> dict[str, JournalState]:
        """返回每个批次的最终状态（同一批次以最后一条记录为准）。"""
        states: dict[str, JournalState] = {}
        for rec in self.read_records():
            if rec.type == "pending":
                states[rec.batch] = JournalState.PENDING
            elif rec.type == "committed":
                states[rec.batch] = JournalState.COMMITTED
        return states

    def last_committed_id(self) -> str | None:
        latest: tuple[float, str] | None = None
        for rec in self.read_records():
            if rec.type == "committed" and (
                latest is None or rec.ts >= latest[0]
            ):
                latest = (rec.ts, rec.batch)
        return latest[1] if latest else None

    def batch_ids(self) -> list[str]:
        seen: list[str] = []
        for rec in self.read_records():
            if rec.batch not in seen:
                seen.append(rec.batch)
        return seen
