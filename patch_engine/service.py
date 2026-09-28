"""批次提交服务：校验、流式应用、原子发布、回滚与并发控制。

语义
----
* 一次 ``submit`` 是一个**事务批次**：所有补丁全部可应用才发布，
  任一失败则工作副本保持提交前状态（暂存目录直接丢弃）。
* 同一批次内补丁按提交顺序应用到“批次视图”上；对同一文件的多个
  补丁顺序生效。不同文件互相独立，重排批次顺序结果一致。
* 重复提交内容相同的补丁：引擎识别“声明位置已是新模式”，结果不变，
  故重复应用幂等。
* 并发：全局提交锁串行化发布；后提交方在锁内基于*最新清单*校验
  ``expected_versions``，冲突抛出版本冲突错误，绝不静默覆盖。
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .engine import ApplyStats, apply_file_patch
from .model import (
    FileOperation,
    Patch,
    PatchRejectDetail,
    PatchRejected,
    RejectReason,
)
from .parser import parse_unified_diff
from .store import FileContent, VersionConflict, Workspace


@dataclass
class BatchResult:
    batch_id: str
    versions: dict[str, str | None]
    stats: dict[str, ApplyStats] = field(default_factory=dict)
    no_change: bool = False


class PatchService:
    """对外的补丁应用服务。"""

    def __init__(
        self,
        workspace: Workspace | Path | str,
        *,
        fuzz_radius: int = 200,
        fuzz_level: int = 2,
    ) -> None:
        if isinstance(workspace, Workspace):
            self.workspace = workspace
        else:
            self.workspace = Workspace(Path(workspace))
        self.fuzz_radius = fuzz_radius
        self.fuzz_level = fuzz_level
        self.workspace.setup()

    # ---- 生命周期 --------------------------------------------------------

    def recover(self) -> dict[str, str]:
        """进程启动时调用：对账未完成批次并修复工作副本。"""
        return self.workspace.recover()

    # ---- 提交 ------------------------------------------------------------

    def submit(
        self,
        patches: list[Patch | str],
        *,
        expected_versions: dict[str, str | None] | None = None,
        fuzz_radius: int | None = None,
        fuzz_level: int | None = None,
        deadline_seconds: float | None = None,
        batch_id: str | None = None,
    ) -> BatchResult:
        """以一个批次提交多个补丁。全成功或全回滚。"""
        if not patches:
            raise ValueError("批次至少包含一个补丁")
        fuzz_radius = self.fuzz_radius if fuzz_radius is None else fuzz_radius
        fuzz_level = self.fuzz_level if fuzz_level is None else fuzz_level

        parsed: list[Patch] = [
            p if isinstance(p, Patch) else parse_unified_diff(p) for p in patches
        ]
        batch_id = batch_id or uuid.uuid4().hex
        expected = expected_versions or {}

        with self.workspace.transaction_lock():
            # 提交开始时的版本快照：批次内所有文件的版本基准。
            versions = self.workspace.current_versions()
            # 批次视图：path -> FileContent 或 None（删除）；惰性读取。
            view: dict[str, FileContent | None] = {}

            def _get(path: str) -> FileContent | None:
                if path not in view:
                    view[path] = self.workspace.read_content(path)
                return view[path]

            # 预检：调用方显式给出的期望版本必须与最新清单一致，
            # 使并发竞争在引擎应用（可能先抛上下文不匹配）之前被识别。
            for path, exp in expected.items():
                actual = versions.get(path)
                if actual != exp:
                    raise VersionConflict(path, exp, actual)

            staging = self.workspace.prepare_staging(batch_id)
            stats_by_path: dict[str, ApplyStats] = {}
            changed_paths: set[str] = set()

            try:
                for patch in parsed:
                    for file_patch in patch.files:
                        op = file_patch.operation
                        target = file_patch.target_path

                        if op is FileOperation.RENAME:
                            src = file_patch.old_path
                            dst = file_patch.new_path
                            current = _get(src)
                            if current is None:
                                raise PatchRejected(
                                    PatchRejectDetail(
                                        path=src,
                                        reason=RejectReason.TARGET_MISSING,
                                        message=f"重命名源 {src} 不存在",
                                    )
                                )
                            dst_content = view.get(
                                dst, self.workspace.read_content(dst)
                            )
                            if src != dst and dst_content is not None:
                                raise PatchRejected(
                                    PatchRejectDetail(
                                        path=dst,
                                        reason=RejectReason.TARGET_EXISTS,
                                        message=f"重命名目标 {dst} 已存在",
                                    )
                                )
                            if file_patch.hunks:
                                moved = self._stream_apply(
                                    file_patch,
                                    current,
                                    staging,
                                    fuzz_radius,
                                    fuzz_level,
                                    deadline_seconds,
                                    stats_by_path,
                                )
                            else:
                                moved = current
                            view[src] = None
                            view[dst] = moved
                            changed_paths.add(dst)
                            continue

                        current = _get(target)
                        before = current.blob_id if current is not None else None
                        new_content = self._apply_one(
                            file_patch,
                            current,
                            staging,
                            fuzz_radius,
                            fuzz_level,
                            deadline_seconds,
                            stats_by_path,
                        )
                        after = (
                            new_content.blob_id if new_content is not None else None
                        )
                        if before != after:
                            changed_paths.add(target)
                        view[target] = new_content

                # 组装新版本表：从快照出发应用批次视图。
                new_versions = dict(versions)
                for path, content in view.items():
                    if content is None:
                        new_versions.pop(path, None)
                    else:
                        new_versions[path] = self.workspace.write_staged_blob(
                            staging, content
                        )

                # 发布点锁内校验：调用方显式期望优先，否则以批次开始快照为基准。
                check = dict(versions)
                check.update(expected)

                self.workspace.journal.append_pending(batch_id)
                manifest = self.workspace.commit_manifest(
                    batch_id, staging, new_versions, check
                )
                self.workspace.journal.append_committed(batch_id)
                # 已发布：暂存副本不再需要（blob 已进入对象库）。
                self.workspace.discard_staging(batch_id)
                return BatchResult(
                    batch_id=batch_id,
                    versions=dict(manifest.versions),
                    stats=stats_by_path,
                    no_change=not changed_paths,
                )
            except BaseException:
                # 任何失败：丢弃暂存，绝不触碰工作副本与对象库。
                self.workspace.discard_staging(batch_id)
                raise

    # ---- 单文件应用 ------------------------------------------------------

    def _apply_one(
        self,
        file_patch,
        current: FileContent | None,
        staging: Path,
        fuzz_radius: int,
        fuzz_level: int,
        deadline_seconds: float | None,
        stats_by_path: dict[str, ApplyStats],
    ) -> FileContent | None:
        op = file_patch.operation
        target = file_patch.target_path

        if op is FileOperation.CREATE:
            if current is not None:
                # 幂等：目标已存在且内容与补丁新增内容一致，视为已应用。
                candidate = self._stream_apply(
                    file_patch,
                    None,
                    staging,
                    fuzz_radius,
                    fuzz_level,
                    deadline_seconds,
                    stats_by_path,
                )
                if candidate.blob_id != current.blob_id:
                    raise PatchRejected(
                        PatchRejectDetail(
                            path=target,
                            reason=RejectReason.TARGET_EXISTS,
                            message="新建补丁的目标文件已存在且内容不同",
                        )
                    )
                return current
            return self._stream_apply(file_patch, None, staging, fuzz_radius,
                                      fuzz_level, deadline_seconds, stats_by_path)

        if op is FileOperation.DELETE:
            if current is None:
                # 幂等：删除一个不存在的文件视为无操作（已删除）。
                return None
            if file_patch.hunks:
                # 带删除 hunk：必须通过引擎的上下文校验才允许删除，
                # 防止用错误补丁静默删除不相关内容。
                self._stream_apply(file_patch, current, staging, fuzz_radius,
                                   fuzz_level, deadline_seconds, stats_by_path)
            return None

        # MODIFY
        if current is None:
            raise PatchRejected(
                PatchRejectDetail(
                    path=target,
                    reason=RejectReason.TARGET_MISSING,
                    message="修改补丁的目标文件不存在",
                )
            )
        return self._stream_apply(file_patch, current, staging, fuzz_radius,
                                  fuzz_level, deadline_seconds, stats_by_path)

    def _stream_apply(
        self,
        file_patch,
        current: FileContent | None,
        staging: Path,
        fuzz_radius: int,
        fuzz_level: int,
        deadline_seconds: float | None,
        stats_by_path: dict[str, ApplyStats],
    ) -> FileContent | None:
        target = file_patch.target_path
        if file_patch.operation is FileOperation.RENAME and not file_patch.hunks:
            return current

        # 输出直接流式写入暂存临时文件，避免整文件驻留内存。
        out_fd, out_name = tempfile.mkstemp(
            dir=str(staging), prefix="out-", suffix=".tmp"
        )
        stats = ApplyStats()
        try:
            with os.fdopen(out_fd, "w", encoding="utf-8", newline="") as out:
                source_lines = iter(current.lines) if current is not None else iter(())
                trailing = current.trailing_newline if current is not None else True
                apply_file_patch(
                    source_lines,
                    file_patch,
                    out,
                    fuzz_radius=fuzz_radius,
                    fuzz_level=fuzz_level,
                    deadline_seconds=deadline_seconds,
                    stats=stats,
                    source_trailing_newline=trailing,
                )
        except BaseException:
            if os.path.exists(out_name):
                os.unlink(out_name)
            raise
        stats_by_path[target] = stats

        data = Path(out_name).read_bytes()
        os.unlink(out_name)
        return FileContent.decode(data)


_sentinel = object()
