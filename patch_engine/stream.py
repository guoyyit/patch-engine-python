"""Bounded-memory transform for large files.

Two passes over the source, both streamed in small blocks:

**Pass 1 -- locate** (:func:`locate_streaming`).  Lines are read via a
block-buffered iterator (default 64 KiB blocks), so only a handful of
partially-delivered line strings exist at once plus a rolling *window*.
The window retains just the lines each still-active hunk can reach:

* a hunk matching *exactly* at its declared position is finalized the
  moment it is read and its region released;
* a hunk that needs nearest/fuzzy resolution keeps the span
  ``[declared - hunk_len, EOF)`` -- memory proportional to the span
  covered by *active* hunks, independent of unrelated file prefixes.

**Pass 2 -- rewrite** (:func:`rewrite_streaming`).  The source is copied
to a temp file block by block; each resolved line interval is replaced by
its post-image.  Only one block plus one hunk's text is held.

Thus peak extra memory is roughly
``O(block_lines + active-hunk line spans)`` -- a function of the block
size and the active patches, not of total file size.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .matcher import (
    _strip_counts,
    check_old_bounds,
    declared_old_pos,
    effective_body,
)
from .models import HunkApplication, PatchRejected, RejectReason
from .textutil import NEWLINE_SENTINEL

DEFAULT_BLOCK = 64 * 1024  # bytes


@dataclass
class Plan:
    index: int
    start: int
    end: int
    post: list[str]
    record: HunkApplication


def _old_body(hunk, fuzz):
    body = effective_body(hunk, fuzz)
    return [l.text for l in body if l.kind in (" ", "-")]


def _new_body(hunk, fuzz):
    body = effective_body(hunk, fuzz)
    out = [l.text for l in body if l.kind in (" ", "+")]
    if out and out[-1] == NEWLINE_SENTINEL and hunk.new_no_newline:
        out.pop()
    return out


def iter_lines(path: str | Path, block_bytes: int = DEFAULT_BLOCK) -> Iterator[str]:
    """Yield logical lines (without ``\\n``) one at a time, block-buffered."""
    carry = ""
    with open(path, "r", encoding="utf-8", newline="") as f:
        while True:
            block = f.read(block_bytes)
            if not block:
                if carry:
                    yield carry
                return
            parts = (carry + block).split("\n")
            carry = parts.pop()
            for line in parts:
                yield line


def count_file_lines(path: str | Path, block_bytes: int = DEFAULT_BLOCK) -> int:
    total = 0
    with open(path, "r", encoding="utf-8", newline="") as f:
        while True:
            block = f.read(block_bytes)
            if not block:
                break
            total += block.count("\n")
    return total


def _matches(buf: list[str], pattern: list[str], pos: int) -> bool:
    if pos < 0 or pos + len(pattern) > len(buf):
        return False
    return all(buf[pos + j] == pattern[j] for j in range(len(pattern)))


def _all_positions(buf: list[str], pattern: list[str]) -> list[int]:
    out: list[int] = []
    m = len(pattern)
    if m == 0:
        return out
    for p in range(0, len(buf) - m + 1):
        if all(buf[p + j] == pattern[j] for j in range(m)):
            out.append(p)
    return out


def _resolve_in_window(buf, hunk, index, declared_global, base, *,
                       max_fuzz, path) -> Plan:
    pattern = _old_body(hunk, 0)
    declared = declared_global - base

    if _matches(buf, pattern, declared):
        return Plan(
            index, declared_global, declared_global + len(pattern),
            _new_body(hunk, 0),
            HunkApplication(index, hunk.old_start, declared_global + 1, 0, 0),
        )
    candidates = _all_positions(buf, pattern)
    if candidates:
        chosen = min(candidates, key=lambda p: (abs(p - declared), p))
        g = chosen + base
        return Plan(
            index, g, g + len(pattern), _new_body(hunk, 0),
            HunkApplication(index, hunk.old_start, g + 1, g - declared_global, 0),
        )

    tried: set[tuple[int, int, int]] = set()
    for fuzz in range(1, max_fuzz + 1):
        lead, trail = _strip_counts(hunk, fuzz)
        if lead == 0 and trail == 0:
            continue
        body = effective_body(hunk, fuzz)
        interior = [l.text for l in body if l.kind in (" ", "-")]
        if not interior or not any(l.kind in ("-", "+") for l in body):
            continue
        key = (lead, trail, len(interior))
        if key in tried:
            continue
        tried.add(key)
        positions = _all_positions(buf, interior)
        if positions:
            chosen = min(positions, key=lambda p: (abs((p - lead) - declared), p))
            g = chosen + base
            return Plan(
                index, g, g + len(interior), _new_body(hunk, fuzz),
                HunkApplication(index, hunk.old_start, g + 1,
                                (g - lead) - declared_global, fuzz),
            )
    raise PatchRejected(
        RejectReason.CONTEXT_MISMATCH,
        "hunk context does not match any location in the target file",
        file=path, hunk_index=index, hunk_start=hunk.old_start,
    )


def locate_streaming(
    source_path, hunks, *, n_lines: int, max_fuzz: int = 2,
    path: str | None = None, block_bytes: int = DEFAULT_BLOCK,
) -> list[Plan]:
    """Pass 1: global ``[start, end)`` plans via a bounded rolling window."""
    for i, hk in enumerate(hunks):
        check_old_bounds(n_lines, hk, i, path)

    declared_positions = {i: declared_old_pos(hk) for i, hk in enumerate(hunks)}

    plans: list[Plan] = []
    remaining = set(range(len(hunks)))
    buf: list[str] = []
    base = 0
    consumed = 0

    for line in iter_lines(source_path, block_bytes):
        buf.append(line)
        consumed += 1

        for i in list(remaining):
            hk = hunks[i]
            if hk.old_count == 0:
                continue
            declared = declared_positions[i]
            local = declared - base
            pattern = _old_body(hk, 0)
            if 0 <= local <= len(buf) - len(pattern) and _matches(buf, pattern, local):
                plans.append(Plan(
                    i, declared, declared + len(pattern),
                    _new_body(hk, 0),
                    HunkApplication(i, hk.old_start, declared + 1, 0, 0),
                ))
                remaining.discard(i)

        if remaining:
            bounds = []
            for i in remaining:
                hk = hunks[i]
                if hk.old_count == 0:
                    bounds.append(declared_positions[i])
                else:
                    bounds.append(declared_positions[i] - len(_old_body(hk, 0)))
            keep_from = max(0, min(bounds))
        else:
            keep_from = consumed
        drop = max(0, min(keep_from - base, len(buf)))
        if drop:
            del buf[:drop]
            base += drop

    for i in sorted(remaining):
        hk = hunks[i]
        declared = declared_positions[i]
        if hk.old_count == 0:
            plans.append(Plan(
                i, declared, declared, _new_body(hk, 0),
                HunkApplication(i, hk.old_start, declared + 1, 0, 0),
            ))
            remaining.discard(i)

    for i in sorted(remaining):
        plans.append(_resolve_in_window(
            buf, hunks[i], i, declared_positions[i], base,
            max_fuzz=max_fuzz, path=path,
        ))

    plans.sort(key=lambda pl: pl.start)
    for a, b in zip(plans, plans[1:]):
        if b.start < a.end:
            raise PatchRejected(
                RejectReason.CONTEXT_MISMATCH,
                f"hunks {a.index} and {b.index} overlap after location",
                file=path, hunk_index=b.index,
            )
    return plans


def hunks_already_applied(
    source_path, hunks, *, n_lines: int, block_bytes: int = DEFAULT_BLOCK,
) -> bool:
    """Bounded idempotency check: each post-image near its declared spot."""
    ordered = sorted(hunks, key=lambda hk: hk.new_start)
    if not ordered:
        return False
    longest = max(
        (len(_old_body(hk, 0)) for hk in ordered if hk.old_count), default=0
    )

    idx = 0
    window: list[str] = []
    wbase = 0
    consumed = 0
    for line in iter_lines(source_path, block_bytes):
        window.append(line)
        consumed += 1
        while idx < len(ordered):
            hk = ordered[idx]
            image = _new_body(hk, 0)
            post_len = len(image)
            decl_new = hk.new_start - 1 if hk.new_count else hk.new_start
            reach = longest + post_len + 1
            if not (consumed >= n_lines or consumed >= decl_new + reach):
                break
            if hk.new_count == 0:
                idx += 1
                continue
            lo = max(0, decl_new - longest - wbase)
            hi = min(len(window), decl_new + longest + post_len - wbase + 1)
            if not _all_positions(window[lo:hi], image):
                return False
            idx += 1
        if idx < len(ordered):
            nxt = ordered[idx]
            post_len = len(_new_body(nxt, 0))
            keep = max(0, nxt.new_start - 1 - longest - post_len)
            drop = max(0, min(keep - wbase, len(window)))
            if drop:
                del window[:drop]
                wbase += drop
    return idx == len(ordered)


def rewrite_streaming(
    source_path, plans, *, source_lines: int,
    work_dir=None, block_bytes: int = DEFAULT_BLOCK,
    drop_final_newline: bool = False,
) -> Path:
    """Pass 2: stream source to a temp file applying *plans*."""
    work_dir = str(work_dir) if work_dir else None
    fd, name = tempfile.mkstemp(prefix="papply-", dir=work_dir)
    os.close(fd)
    out = Path(name)

    line_iter = iter_lines(source_path, block_bytes)
    cursor = 0  # global index of next line available from line_iter

    def emit(f, seg: list[str]) -> None:
        if seg:
            f.write("\n".join(seg))
            f.write("\n")

    with open(out, "w", encoding="utf-8", newline="") as f:
        # Read source line-by-line (iterator is block-buffered internally)
        # and emit chunks; at most a small emit buffer + current plan is held.
        out_buf: list[str] = []
        plan_by_start = {p.start: p for p in sorted(plans, key=lambda p: p.start)}
        ordered = sorted(plans, key=lambda p: p.start)
        pi = 0
        skipping = 0
        inserting: list[list[str]] = []

        for line in line_iter:
            g = cursor
            cursor += 1
            # Insertion-only plan at this exact position (before the line).
            if pi < len(ordered) and g == ordered[pi].start and ordered[pi].end == g:
                plan = ordered[pi]
                emit(f, out_buf)
                out_buf = []
                emit(f, plan.post)
                pi += 1
            if skipping:
                skipping -= 1
                continue
            if pi < len(ordered) and g == ordered[pi].start:
                plan = ordered[pi]
                emit(f, out_buf)
                out_buf = []
                emit(f, plan.post)
                skipping = plan.end - plan.start - 1  # current line also replaced
                pi += 1
                continue
            out_buf.append(line)
            if len(out_buf) >= 4096:
                emit(f, out_buf)
                out_buf = []
        emit(f, out_buf)

        # Insertion plans located exactly at EOF.
        while pi < len(ordered):
            plan = ordered[pi]
            if plan.end != plan.start:
                break  # should not happen: content plans consumed by loop
            emit(f, plan.post)
            pi += 1

        f.flush()
        os.fsync(f.fileno())
        if drop_final_newline and f.tell() > 0:
            f.seek(f.tell() - 1)
            if f.read(1) == "\n":
                f.seek(f.tell() - 1)
                f.truncate()
    return out
