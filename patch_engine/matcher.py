"""Deterministic location of hunks within target content.

Resolution order for one hunk:

1. **Exact declared position** -- offset 0, fuzz 0.
2. **Exact nearest match** -- the full pre-image must occur somewhere
   in bounds; the candidate with the smallest ``|actual - declared|``
   offset wins; ties resolve to the smaller line number (deterministic).
3. **Fuzzy match** -- up to ``max_fuzz`` leading/trailing *context*
   lines are dropped (GNU-patch style fuzz); lower fuzz wins, then the
   same nearest / smallest-line rule applies.

If the declared line range itself lies outside the file,
:class:`PatchRejected` with ``LINE_RANGE_OUT_OF_BOUNDS`` is raised and no
relocation is attempted; a content mismatch at a valid position raises
``CONTEXT_MISMATCH``.
"""

from __future__ import annotations

from .models import Hunk, HunkApplication, HunkLine, PatchRejected, RejectReason


def old_side(hunk: Hunk) -> list[str]:
    return [l.text for l in hunk.lines if l.kind in (" ", "-")]


def new_side(hunk: Hunk) -> list[str]:
    return [l.text for l in hunk.lines if l.kind in (" ", "+")]


def declared_old_pos(hunk: Hunk) -> int:
    """0-based position the hunk header claims.

    For zero-count insertion hunks ``-l,0`` points *after* line ``l``.
    """
    return hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1


def declared_new_pos(hunk: Hunk) -> int:
    return hunk.new_start if hunk.new_count == 0 else hunk.new_start - 1


def check_old_bounds(n_lines: int, hunk: Hunk, index: int, path: str | None) -> None:
    """Raise LINE_RANGE_OUT_OF_BOUNDS if the declared old range is invalid."""
    if hunk.old_count == 0:
        if not (0 <= hunk.old_start <= n_lines):
            raise PatchRejected(
                RejectReason.LINE_RANGE_OUT_OF_BOUNDS,
                f"insertion point {hunk.old_start} outside file of {n_lines} lines",
                file=path,
                hunk_index=index,
                hunk_start=hunk.old_start,
            )
        return
    if hunk.old_start < 1 or hunk.old_start + hunk.old_count - 1 > n_lines:
        raise PatchRejected(
            RejectReason.LINE_RANGE_OUT_OF_BOUNDS,
            f"hunk range {hunk.old_start},{hunk.old_count} outside file of "
            f"{n_lines} lines",
            file=path,
            hunk_index=index,
            hunk_start=hunk.old_start,
        )


def _find_matches(lines: list[str], pattern: list[str]) -> list[int]:
    """All 0-based positions where *pattern* occurs exactly."""
    n = len(lines)
    m = len(pattern)
    if m == 0:
        return []
    positions: list[int] = []
    for p in range(0, n - m + 1):
        for j in range(m):
            if lines[p + j] != pattern[j]:
                break
        else:
            positions.append(p)
    return positions


def _strip_counts(hunk: Hunk, fuzz: int) -> tuple[int, int]:
    """Number of leading/trailing context lines droppable at this fuzz level."""
    lead = 0
    for l in hunk.lines:
        if l.kind == " " and lead < fuzz:
            lead += 1
        else:
            break
    trail = 0
    for l in reversed(hunk.lines):
        if l.kind == " " and trail < fuzz:
            trail += 1
        else:
            break
    return lead, trail


def effective_body(hunk: Hunk, fuzz: int) -> tuple[HunkLine, ...]:
    """Hunk body after dropping leading/trailing context for *fuzz*."""
    if fuzz == 0:
        return hunk.lines
    lead, trail = _strip_counts(hunk, fuzz)
    if trail == 0:
        return hunk.lines[lead:]
    return hunk.lines[lead:-trail]


def locate_hunk(
    lines: list[str],
    hunk: Hunk,
    hunk_index: int,
    *,
    max_fuzz: int = 2,
    path: str | None = None,
) -> tuple[int, HunkApplication]:
    """Locate *hunk* and return ``(position, application record)``.

    *position* is the 0-based index at which the (possibly fuzz-stripped)
    pre-image starts.
    """
    n = len(lines)
    check_old_bounds(n, hunk, hunk_index, path)
    declared = declared_old_pos(hunk)

    # Pure insertion hunk: no textual anchor, line number is authoritative.
    if hunk.old_count == 0:
        return declared, HunkApplication(
            hunk_index=hunk_index,
            old_start_declared=hunk.old_start,
            applied_at=declared + 1,
            offset=0,
            fuzz=0,
        )

    pattern = old_side(hunk)

    # 1. exact declared position
    if (
        declared >= 0
        and declared + len(pattern) <= n
        and all(lines[declared + j] == pattern[j] for j in range(len(pattern)))
    ):
        return declared, HunkApplication(
            hunk_index=hunk_index,
            old_start_declared=hunk.old_start,
            applied_at=declared + 1,
            offset=0,
            fuzz=0,
        )

    # 2. exact nearest match (full pattern)
    candidates = _find_matches(lines, pattern)
    if candidates:
        chosen = min(candidates, key=lambda p: (abs(p - declared), p))
        return chosen, HunkApplication(
            hunk_index=hunk_index,
            old_start_declared=hunk.old_start,
            applied_at=chosen + 1,
            offset=chosen - declared,
            fuzz=0,
        )

    # 3. fuzzy matches, increasing fuzz
    tried_interiors: set[int] = set()
    for fuzz in range(1, max_fuzz + 1):
        lead, trail = _strip_counts(hunk, fuzz)
        if lead == 0 and trail == 0:
            continue
        body = effective_body(hunk, fuzz)
        interior = [l.text for l in body if l.kind in (" ", "-")]
        if not interior:
            continue  # nothing left to anchor on
        # Refuse fuzzy "matches" that erase every actual change.
        if not any(l.kind in ("-", "+") for l in body):
            continue
        key = (lead, trail, len(interior))
        if key in tried_interiors:
            continue
        tried_interiors.add(key)

        positions = _find_matches(lines, interior)
        if positions:
            chosen = min(
                positions,
                key=lambda p: (abs((p - lead) - declared), p),
            )
            return chosen, HunkApplication(
                hunk_index=hunk_index,
                old_start_declared=hunk.old_start,
                applied_at=chosen + 1,
                offset=(chosen - lead) - declared,
                fuzz=fuzz,
            )

    raise PatchRejected(
        RejectReason.CONTEXT_MISMATCH,
        "hunk context does not match any location in the target file",
        file=path,
        hunk_index=hunk_index,
        hunk_start=hunk.old_start,
        candidates=None,
    )


def hunk_already_applied(lines: list[str], hunk: Hunk) -> bool:
    """Whether the post-image of *hunk* is present at its declared spot."""
    return find_post_image_position(lines, hunk, allow_fallback=False) is not None


def find_post_image_position(
    lines: list[str], hunk: Hunk, *, allow_fallback: bool
) -> int | None:
    """0-based position of the hunk's post-image, or ``None``.

    With *allow_fallback=False* only the declared new-side position is
    accepted.  With ``True`` the nearest occurrence is used as a fallback
    (same deterministic ordering as :func:`locate_hunk`).  This fallback is
    intended solely for idempotent replay detection after pre-image
    location has already failed, never for a first application.
    """
    n = len(lines)
    image = new_side(hunk)
    if hunk.new_count == 0 or not image:
        # Pure deletion: applied iff the declared old content is gone.
        old = old_side(hunk)
        pos = declared_old_pos(hunk)
        if not old:
            return pos
        if pos < 0 or pos + len(old) > n:
            return max(0, min(pos, n))
        if all(lines[pos + j] == old[j] for j in range(len(old))):
            return None
        return pos
    pos = declared_new_pos(hunk)
    if 0 <= pos <= n - len(image) and all(
        lines[pos + j] == image[j] for j in range(len(image))
    ):
        return pos
    if not allow_fallback:
        return None
    positions = _find_matches(lines, image)
    if not positions:
        return None
    return min(positions, key=lambda p: (abs(p - pos), p))
