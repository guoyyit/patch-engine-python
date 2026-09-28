"""工作副本存储：内容寻址对象库、清单、暂存区与崩溃恢复。

目录布局::

    <root>/
      wc/                工作副本（对外可见的真实文件）
      objects/<sha256>   不可变内容 blob（含末尾换行的完整文件字节）
      manifest.json      当前版本清单：{path: blob_id}，含 last_batch 标记
      staging/<bid>/     批次暂存：payload.json + blobs/<sha> + manifest.json
      locks/global.lock  全局排他文件锁（串行化所有提交）

原子性
------
一次批次的所有新内容先落在暂存区；清单通过同目录临时文件 ``rename``
原子发布。因此工作副本与清单要么停留在提交前、要么已是提交后，
不存在“部分文件已写入”的可见中间状态。恢复依据清单中的批次标记与
:class:`~patch_engine.journal.Journal` 对账（见 :meth:`Workspace.recover`)。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from .journal import Journal, JournalState


class VersionConflict(Exception):
    """调用方携带的期望版本与当前版本不一致（并发竞争）。"""

    def __init__(self, path: str, expected: str | None, actual: str | None) -> None:
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{path}: 期望版本 {expected}，当前版本 {actual}"
        )


@dataclass(frozen=True)
class FileContent:
    """工作副本中一个文件的逻辑内容。"""

    lines: list[str]
    trailing_newline: bool

    def encode(self) -> bytes:
        data = "".join(line + "\n" for line in self.lines)
        if not self.trailing_newline and self.lines:
            data = data[:-1]
        return data.encode("utf-8")

    @staticmethod
    def decode(data: bytes) -> FileContent:
        if not data:
            return FileContent(lines=[], trailing_newline=True)
        trailing = data.endswith(b"\n")
        text = data.decode("utf-8")
        if trailing:
            text = text[:-1]
        lines = text.split("\n") if text else []
        return FileContent(lines=lines, trailing_newline=trailing)

    @property
    def blob_id(self) -> str:
        return hashlib.sha256(self.encode()).hexdigest()


class _Manifest:
    def __init__(self, versions: dict[str, str], last_batch: str | None) -> None:
        self.versions = versions
        self.last_batch = last_batch

    def to_dict(self) -> dict:
        return {"version": 1, "last_batch": self.last_batch, "files": self.versions}

    @staticmethod
    def from_dict(data: dict) -> _Manifest:
        return _Manifest(
            versions=dict(data.get("files", {})),
            last_batch=data.get("last_batch"),
        )


class Workspace:
    """管理工作副本目录与全部持久化元数据。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.wc_dir = self.root / "wc"
        self.objects_dir = self.root / "objects"
        self.staging_dir = self.root / "staging"
        self.locks_dir = self.root / "locks"
        self.manifest_path = self.root / "manifest.json"
        self._journal = Journal(self.root / "journal.log")

    @property
    def journal(self) -> Journal:
        return self._journal

    # ---- 初始化与锁 -----------------------------------------------------

    def setup(self) -> None:
        for directory in (
            self.root,
            self.wc_dir,
            self.objects_dir,
            self.staging_dir,
            self.locks_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        if not self.manifest_path.exists():
            self._write_manifest(_Manifest({}, None))
        self._fsync_dir(self.root)

    @contextlib.contextmanager
    def transaction_lock(self):
        """进程内可重入 + 跨进程排他的全局提交锁。"""
        self.setup()
        lock = self.locks_dir / "global.lock"
        with _InterProcessLock(lock):
            yield

    # ---- 读取 -----------------------------------------------------------

    def _read_manifest(self) -> _Manifest:
        if not self.manifest_path.exists():
            return _Manifest({}, None)
        with self.manifest_path.open("r", encoding="utf-8") as fh:
            return _Manifest.from_dict(json.load(fh))

    def _write_manifest(self, manifest: _Manifest) -> None:
        tmp = self.manifest_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(manifest.to_dict(), fh, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.manifest_path)
        self._fsync_dir(self.root)

    def current_versions(self) -> dict[str, str]:
        return dict(self._read_manifest().versions)

    def current_version(self, path: str) -> str | None:
        return self._read_manifest().versions.get(path)

    def exists(self, path: str) -> bool:
        return path in self._read_manifest().versions

    def read_content(self, path: str) -> FileContent | None:
        blob_id = self._read_manifest().versions.get(path)
        if blob_id is None:
            return None
        return self._read_blob(blob_id)

    def _blob_path(self, blob_id: str) -> Path:
        return self.objects_dir / blob_id

    def _read_blob(self, blob_id: str) -> FileContent:
        return FileContent.decode(self._blob_path(blob_id).read_bytes())

    # ---- 暂存与提交 ------------------------------------------------------

    def stage_dir(self, batch_id: str) -> Path:
        return self.staging_dir / batch_id

    def prepare_staging(self, batch_id: str) -> Path:
        """创建（或清空重建）批次暂存目录，返回其路径。"""
        staging = self.stage_dir(batch_id)
        if staging.exists():
            shutil.rmtree(staging)
        (staging / "blobs").mkdir(parents=True)
        return staging

    def write_staged_blob(self, staging: Path, content: FileContent) -> str:
        blob_id = content.blob_id
        data = content.encode()
        # 先写暂存副本，提交时再发布到对象库（对象不可变，重复内容无害）。
        staged_copy = staging / "blobs" / blob_id
        if not staged_copy.exists():
            tmp = staged_copy.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, staged_copy)
        return blob_id

    def commit_manifest(
        self,
        batch_id: str,
        staging: Path,
        new_versions: dict[str, str],
        expected: dict[str, str | None],
    ) -> _Manifest:
        """持锁校验期望版本并原子发布新清单，发布 blob 并物化工作副本。

        必须在 :meth:`transaction_lock` 内调用。
        """
        current = self._read_manifest()
        for path, exp in expected.items():
            actual = current.versions.get(path)
            if actual != exp:
                raise VersionConflict(path, exp, actual)

        manifest = _Manifest(dict(new_versions), batch_id)
        manifest_tmp = staging / "manifest.json"
        with manifest_tmp.open("w", encoding="utf-8") as fh:
            json.dump(manifest.to_dict(), fh, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())

        # 1) 发布 blob（不可变；已存在则跳过）。
        for blob_path in (staging / "blobs").iterdir():
            target = self.objects_dir / blob_path.name
            if not target.exists():
                tmp = target.with_suffix(".tmp")
                tmp.write_bytes(blob_path.read_bytes())
                os.replace(tmp, target)
        self._fsync_dir(self.objects_dir)

        # 2) 原子切换清单（提交点）。
        os.replace(manifest_tmp, self.manifest_path)
        self._fsync_dir(self.root)

        # 3) 物化工作副本。崩溃只可能留下 *.tmp，不影响已发布状态。
        self._materialize(manifest)
        return manifest

    def _materialize(self, manifest: _Manifest) -> None:
        wanted = set(manifest.versions)
        for path in list(self._wc_paths()):
            if path not in wanted:
                self._wc_file(path).unlink(missing_ok=True)
        for path, blob_id in manifest.versions.items():
            target = self._wc_file(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            data = self._blob_path(blob_id).read_bytes()
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        self._prune_empty_dirs()
        self._fsync_dir(self.wc_dir)

    def _wc_paths(self) -> list[str]:
        if not self.wc_dir.exists():
            return []
        return [
            str(p.relative_to(self.wc_dir))
            for p in self.wc_dir.rglob("*")
            if p.is_file() and p.suffix != ".tmp"
        ]

    def _wc_file(self, path: str) -> Path:
        # 禁止路径逃逸工作副本。
        candidate = (self.wc_dir / path).resolve()
        wc_root = self.wc_dir.resolve()
        if wc_root not in candidate.parents and candidate != wc_root:
            raise ValueError(f"非法路径（逃逸工作副本）: {path}")
        return candidate

    def _prune_empty_dirs(self) -> None:
        for directory in sorted(
            (p for p in self.wc_dir.rglob("*") if p.is_dir()),
            reverse=True,
        ):
            with contextlib.suppress(OSError):
                directory.rmdir()

    def discard_staging(self, batch_id: str) -> None:
        staging = self.stage_dir(batch_id)
        if staging.exists():
            shutil.rmtree(staging)

    # ---- 崩溃恢复 --------------------------------------------------------

    def recover(self) -> dict[str, str]:
        """启动时对账并清理，返回 ``{batch_id: 最终状态}``。

        * manifest 已携带某 pending 批次标记 → 提交实际已完成，补记 committed；
        * 否则该 pending 批次未发布，丢弃暂存（工作副本保持上个一致状态）。
        """
        self.setup()
        with self.transaction_lock():
            states = self._journal.recover_state()
            manifest = self._read_manifest()
            result: dict[str, str] = {}
            for batch_id, state in states.items():
                if state is JournalState.COMMITTED:
                    result[batch_id] = "committed"
                    # blob 已发布，暂存副本可安全清理。
                    self.discard_staging(batch_id)
                    continue
                # pending：
                if manifest.last_batch == batch_id:
                    # 清单已切换但 committed 记录没来得及落盘：视为已提交。
                    self._journal.append_committed(batch_id)
                    result[batch_id] = "committed"
                else:
                    self.discard_staging(batch_id)
                    result[batch_id] = "aborted"
            # 清理任何无主暂存目录（崩溃发生在 pending 记录之前）。
            known = set(states)
            for child in self.staging_dir.iterdir():
                if child.is_dir() and child.name not in known:
                    shutil.rmtree(child)
            # 确保工作副本与清单一致（修复物化阶段崩溃）。
            self._materialize(manifest)
            return result

    # ---- 工具 ------------------------------------------------------------

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            fd = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class _InterProcessLock:
    """跨进程排他锁（``fcntl.flock``），进程内用可重入锁串行化。

    ``flock`` 绑定打开文件描述的锁，同一进程内多个线程会互相竞争；
    故再叠加一个进程级 ``RLock``，保证同进程多线程串行、同线程可重入，
    不同进程之间仍由内核锁互斥。
    """

    _process_lock = threading.RLock()

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh = None

    def __enter__(self):
        import fcntl

        self._process_lock.acquire()
        self._fh = self._path.open("a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        import fcntl

        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        self._process_lock.release()
        return False
