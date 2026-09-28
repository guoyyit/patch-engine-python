"""Apply one :class:`FilePatch` to a line list.

Hunks are *located and validated in order* before any output is produced,
so a failure on a later hunk never leaves earlier hunks half applied.
Each located hunk is applied to an evolving in-memory copy, which also
lets a hunk whose post-image is already present (idempotent replay of an
identical patch) be skipped instead of rejected.
"""

from __future__ import annotations

from .matcher import (
    effective_body,
    find_post_image_position,
    hunk_already_applied,
    locate_hunk,
)
from .models import (
    FileApplication,
    FilePatch,
    HunkApplication,
    PatchRejected,
    RejectReason,
)
from .textutil import NEWLINE_SENTINEL


def _post_image(hunk, fuzz: int) -> list[str]:
    """New-side lines actually written (fuzz-stripped, sentinel adjusted)."""
    body = effective_body(hunk, fuzz)
    out = [l.text for l in body if l.kind in (" ", "+")]
    if out and out[-1] == NEWLINE_SENTINEL and hunk.new_no_newline:
        out.pop()
    return out


def _pre_image(hunk, fuzz: int) -> list[str]:
    body = effective_body(hunk, fuzz)
    out = [l.text for l in body if l.kind in (" ", "-")]
    if out and out[-1] == NEWLINE_SENTINEL and hunk.old_no_newline:
        out.pop()
    return out


def is_noop_idempotent(lines: list[str], file_patch: FilePatch) -> bool:
    """True when every hunk's post-image already sits at its declared spot."""
    if not file_patch.hunks:
        return False
    return all(hunk_already_applied(lines, h) for h in file_patch.hunks)


def apply_file_patch(
    lines: list[str],
    file_patch: FilePatch,
    *,
    max_fuzz: int = 2,
    old_version: int = 0,
    path_override: str | None = None,
) -> tuple[list[str], FileApplication]:
    """Apply *file_patch* and return ``(new_lines, application report)``.

    Raises :class:`PatchRejected` (bounds / context); *lines* is never
    mutated on failure.
    """
    path = path_override or file_patch.target
    report = FileApplication(
        path=path, old_version=old_version, new_version=old_version
    )

    # Fast path: the whole patch is already present at declared positions.
    if is_noop_idempotent(lines, file_patch):
        return list(lines), report

    working = list(lines)
    applied_any = False
    drop_trailing_newline = False
    applied_records: list[tuple[int, int, HunkApplication]] = []  # pos, fuzz, rec

    for index, hunk in enumerate(file_patch.hunks):
        # Skip hunks already present at their exact declared positions.
        if hunk_already_applied(working, hunk):
            continue
        try:
            pos, rec = locate_hunk(
                working, hunk, index, max_fuzz=max_fuzz, path=path
            )
        except PatchRejected as exc:
            # First application never lands here when the pre-image exists.
            # On an idempotent replay the pre-image is gone but the post-image
            # is present (possibly drifted by earlier hunks) -> skip silently.
            if exc.reason is RejectReason.CONTEXT_MISMATCH:
                already_pos = find_post_image_position(
                    working, hunk, allow_fallback=True
                )
                if already_pos is not None:
                    continue
            raise
        fuzz = rec.fuzz
        pre = _pre_image(hunk, fuzz)
        post = _post_image(hunk, fuzz)

        # Overlap guard against already-composed edits.
        for ppos, ff, arec in applied_records:
            existing_pre = _pre_image(file_patch.hunks[arec.hunk_index], ff)
            e0, e1 = ppos, ppos + len(existing_pre)
            if pos < e1 and pos + len(pre) > e0:
                raise PatchRejected(
                    RejectReason.CONTEXT_MISMATCH,
                    f"hunks {arec.hunk_index} and {index} overlap after location",
                    file=path,
                    hunk_index=index,
                )

        working[pos : pos + len(pre)] = post
        applied_records.append((pos, fuzz, rec))
        report.hunks.append(rec)
        applied_any = True
        if hunk.new_no_newline:
            drop_trailing_newline = True

    if not applied_any:
        return list(lines), report

    # Keep a final newline unless the patch explicitly removes it.  New
    # files (empty input) default to ending with a newline as well.
    had_final_newline = not lines or lines[-1] == NEWLINE_SENTINEL
    if (
        working
        and had_final_newline
        and not drop_trailing_newline
        and working[-1] != NEWLINE_SENTINEL
    ):
        working.append(NEWLINE_SENTINEL)
    if drop_trailing_newline and working and working[-1] == NEWLINE_SENTINEL:
        working.pop()

    return working, report
