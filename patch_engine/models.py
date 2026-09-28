"""Domain types for the patch engine: errors, parsed diff structures, results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RejectReason(str, Enum):
    """Distinguishable rejection reasons for a failed patch application."""

    MALFORMED_HEADER = "malformed_header"
    MALFORMED_HUNK = "malformed_hunk"
    LINE_RANGE_OUT_OF_BOUNDS = "line_range_out_of_bounds"
    CONTEXT_MISMATCH = "context_mismatch"
    EMPTY_PATCH = "empty_patch"


class PatchError(Exception):
    """Base class for patch engine errors."""


class PatchParseError(PatchError):
    """The unified-diff text cannot be parsed."""

    def __init__(self, reason: RejectReason, message: str, *, line: int | None = None):
        self.reason = reason
        self.line = line
        super().__init__(message)


class PatchRejected(PatchError):
    """A parsed patch cannot be applied to the target content."""

    def __init__(
        self,
        reason: RejectReason,
        message: str,
        *,
        file: str | None = None,
        hunk_index: int | None = None,
        hunk_start: int | None = None,
        candidates: list[int] | None = None,
    ):
        self.reason = reason
        self.file = file
        self.hunk_index = hunk_index
        self.hunk_start = hunk_start
        self.candidates = candidates
        super().__init__(message)


class ConflictError(PatchError):
    """Submit-time conflict: base version is stale or the file is locked."""

    def __init__(self, message: str, *, file: str | None = None, expected=None, actual=None):
        self.file = file
        self.expected = expected
        self.actual = actual
        super().__init__(message)


class DeadlineExceeded(PatchError):
    """Processing took longer than the caller-observable time budget."""


@dataclass(frozen=True)
class HunkLine:
    """One line inside a hunk body."""

    kind: str  # ' ' context | '+' addition | '-' deletion
    text: str  # line content without the leading diff marker, *without* newline


@dataclass(frozen=True)
class Hunk:
    """A single ``@@ -a,b +c,d @@`` section."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[HunkLine, ...]
    section_heading: str | None = None
    old_no_newline: bool = False
    new_no_newline: bool = False


@dataclass(frozen=True)
class FilePatch:
    """All hunks belonging to one ``diff --git`` / ``--- ``/``+++`` section."""

    old_path: str | None
    new_path: str | None
    hunks: tuple[Hunk, ...]
    is_new_file: bool = False
    is_deleted_file: bool = False

    @property
    def target(self) -> str:
        """Path the patch writes to (``/dev/null`` means deletion)."""
        return self.new_path if self.new_path not in (None, "/dev/null") else (self.old_path or "")


@dataclass(frozen=True)
class Patch:
    """A parsed unified diff: one or more file patches."""

    files: tuple[FilePatch, ...]
    raw: str = ""


@dataclass
class HunkApplication:
    """Where and how one hunk was applied."""

    hunk_index: int
    old_start_declared: int
    applied_at: int
    offset: int
    fuzz: int


@dataclass
class FileApplication:
    path: str
    hunks: list[HunkApplication] = field(default_factory=list)
    old_version: int = 0
    new_version: int = 0


@dataclass
class CommitResult:
    batch_id: str
    applied: list[FileApplication] = field(default_factory=list)
    elapsed_ms: float = 0.0
