"""领域模型：unified diff 的解析结果、拒绝原因与异常。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class FileOperation(StrEnum):
    """补丁对单个文件的操作类型。"""

    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    RENAME = "rename"


class RejectReason(StrEnum):
    """补丁被拒绝时的可区分原因。"""

    MALFORMED_PATCH = "malformed_patch"
    HUNK_RANGE_OUT_OF_BOUNDS = "hunk_range_out_of_bounds"
    CONTEXT_MISMATCH = "context_mismatch"
    HUNKS_OVERLAP = "hunks_overlap"
    ALREADY_APPLIED_DIFFERENTLY = "already_applied_differently"
    TARGET_EXISTS = "target_exists"
    TARGET_MISSING = "target_missing"
    VERSION_CONFLICT = "version_conflict"
    BATCH_CONFLICT = "batch_conflict"
    DEADLINE_EXCEEDED = "deadline_exceeded"


@dataclass(frozen=True)
class HunkLine:
    """hunk 中的一行。``kind`` 为 ' '（上下文）/'-'（删除）/'+'（新增）。

    ``no_newline`` 表示该行之后没有换行符（来自 ``\\ No newline`` 标记），
    只对模式中的最后一行有意义。
    """

    kind: str
    text: str
    no_newline: bool = False


@dataclass(frozen=True)
class Hunk:
    """@@ -old_start,old_count +new_start,new_count @@ 的单个 hunk。"""

    old_start: int  # 1-based，包含 0（空文件 / 纯新增）
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[HunkLine, ...]
    section_heading: str | None = None

    @property
    def old_pattern(self) -> tuple[HunkLine, ...]:
        return tuple(line for line in self.lines if line.kind in (" ", "-"))

    @property
    def new_pattern(self) -> tuple[HunkLine, ...]:
        return tuple(
            HunkLine(
                " " if line.kind == "+" else line.kind,
                line.text,
                line.no_newline,
            )
            for line in self.lines
            if line.kind in (" ", "+")
        )

    @property
    def context_lines(self) -> tuple[HunkLine, ...]:
        return tuple(line for line in self.lines if line.kind == " ")


@dataclass(frozen=True)
class FilePatch:
    """针对单个文件的完整补丁。"""

    operation: FileOperation
    old_path: str | None
    new_path: str | None
    hunks: tuple[Hunk, ...]
    old_mode: str | None = None
    new_mode: str | None = None

    @property
    def target_path(self) -> str:
        return self.new_path if self.new_path is not None else self.old_path  # type: ignore[return-value]


@dataclass(frozen=True)
class Patch:
    """一次补丁提交中解析出的一个 unified diff（可能含多个文件）。"""

    files: tuple[FilePatch, ...]
    source: str = ""


@dataclass
class HunkRejectDetail:
    """单个 hunk 的拒绝细节。"""

    hunk_index: int
    declared_old_start: int
    reason: RejectReason
    message: str


@dataclass
class PatchRejectDetail:
    """单个文件补丁的拒绝细节。"""

    path: str
    hunks: list[HunkRejectDetail] = field(default_factory=list)
    reason: RejectReason | None = None
    message: str = ""


class PatchEngineError(Exception):
    """补丁服务错误基类。"""


class PatchRejected(PatchEngineError):
    """补丁内容无法应用（上下文不匹配、越界等）。"""

    def __init__(self, detail: PatchRejectDetail) -> None:
        self.detail = detail
        reason = detail.reason.value if detail.reason else ""
        super().__init__(f"{detail.path}: {reason} {detail.message}")


class BatchAborted(PatchEngineError):
    """批次已被回滚（崩溃恢复或并发冲突）。"""


__all__ = [
    "BatchAborted",
    "FileOperation",
    "FilePatch",
    "Hunk",
    "HunkLine",
    "HunkRejectDetail",
    "Patch",
    "PatchEngineError",
    "PatchRejectDetail",
    "PatchRejected",
    "RejectReason",
]
