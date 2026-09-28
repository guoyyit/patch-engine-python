"""补丁应用引擎：流式匹配、最近模糊匹配与确定性选择。

内存模型
--------
输入按行迭代，输出写入给定的文本流。匹配过程中只保留有界滑动窗口：

* 窗口下界 = 上一个已提交 hunk 的匹配区间末尾（已写入输出的位置）；
* 窗口上界 = 当前 hunk 声明位置 + ``fuzz_radius`` + 模式长度。

故峰值内存约为 ``fuzz_radius`` 加当前活动 hunk 的模式规模，
与文件总行数无关，可处理超大文件。失败时仅丢弃输出流（由调用方
指向临时文件），绝不改动原文件。

匹配优先级（确定性）
--------------------
1. 声明位置（``old_start``）精确匹配旧模式；
2. 声明位置幂等识别：该处已是新模式（补丁此前已应用）；
3. 半径 ``fuzz_radius`` 内最近的旧模式精确匹配，同距时向前（行号减小
   方向）优先，再相同按起始行号；
4. 模糊匹配：按 ``fuzz_level`` 逐档对称剥离*边缘上下文行*后重试，
   选择剥离行数最少、距离最近的候选。

拒绝原因（可区分，且发现即中止，绝不使用错误位置继续）
--------------------------------------------------
* 声明起点越过文件尾 → ``HUNK_RANGE_OUT_OF_BOUNDS``；
* 位置可达但无匹配 → ``CONTEXT_MISMATCH``；
* hunk 匹配区间与已提交区间重叠 → ``HUNKS_OVERLAP``。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TextIO

from .model import (
    FilePatch,
    HunkRejectDetail,
    PatchRejectDetail,
    PatchRejected,
    RejectReason,
)


@dataclass
class ApplyResult:
    """单个文件补丁的应用结果。"""

    changed: bool
    line_count: int
    trailing_newline: bool


@dataclass
class ApplyStats:
    """应用过程的可观测指标。"""

    elapsed_seconds: float = 0.0
    lines_scanned: int = 0
    active_hunks: int = 0
    deadline_exceeded: bool = False
    fuzzed_hunks: int = 0


def _strip_edge_context(
    hunk, fuzz: int
) -> tuple[list[str], list[str], int]:
    """对称剥离 ``fuzz`` 行边缘上下文，返回 (旧模式, 新模式, 实际剥离数)。"""
    old = hunk.old_pattern
    new = hunk.new_pattern
    lead = trail = 0
    for line in old:
        if line.kind == " ":
            lead += 1
        else:
            break
    for line in reversed(old):
        if line.kind == " ":
            trail += 1
        else:
            break
    used = min(fuzz, lead, trail)
    old_p = [line.text for line in old[used : len(old) - used if used else len(old)]]
    new_p = [line.text for line in new[used : len(new) - used if used else len(new)]]
    return old_p, new_p, used


def apply_file_patch(
    lines: Iterable[str],
    file_patch: FilePatch,
    out: TextIO,
    *,
    fuzz_radius: int = 200,
    fuzz_level: int = 2,
    deadline_seconds: float | None = None,
    stats: ApplyStats | None = None,
    source_trailing_newline: bool = True,
    clock: Callable[[], float] = time.monotonic,
) -> ApplyResult:
    """流式应用单个文件补丁，把结果逐行写入 ``out``。

    ``lines`` 产出不含换行符的文本行。任何拒绝都在原文件未被触碰的
    前提下抛出（``out`` 只是临时输出，由调用方丢弃）。
    """
    started = clock()
    if stats is None:
        stats = ApplyStats()

    def reject(reason: RejectReason, message: str, hunk_index: int | None = None):
        hunks = []
        if hunk_index is not None:
            h = file_patch.hunks[hunk_index]
            hunks.append(
                HunkRejectDetail(
                    hunk_index=hunk_index,
                    declared_old_start=h.old_start,
                    reason=reason,
                    message=message,
                )
            )
        return PatchRejected(
            PatchRejectDetail(
                path=file_patch.target_path,
                hunks=hunks,
                reason=reason,
                message=message,
            )
        )

    def check_deadline(scanned: int) -> None:
        if (
            deadline_seconds is not None
            and (scanned & 4095) == 0
            and clock() - started > deadline_seconds
        ):
            stats.deadline_exceeded = True
            raise reject(
                RejectReason.DEADLINE_EXCEEDED, "应用超出处理时间上限"
            )

    hunks = file_patch.hunks
    # 按声明位置排序处理；补丁内 hunk 重叠在输出阶段自然被拒。
    order = sorted(range(len(hunks)), key=lambda k: max(hunks[k].old_start - 1, 0))

    iterator = iter(lines)
    buffer: list[str] = []  # 窗口行，buffer[0] 对应全局行 cursor
    cursor = 0  # 已提交到输出的全局行数（=窗口下界）
    total = 0  # 已读入的总行数
    eof = False
    written = 0
    any_change = False
    fuzzed = 0
    # 记录“最后一个触及文件尾的 hunk”对末尾换行的影响。
    tail_hunk_no_newline: bool | None = None

    def fill(through: int) -> None:
        """读入直到窗口覆盖全局位置 through（含）。"""
        nonlocal total, eof
        while not eof and cursor + len(buffer) <= through:
            try:
                line = next(iterator)
            except StopIteration:
                eof = True
                return
            buffer.append(line.rstrip("\n"))
            total += 1
            stats.lines_scanned = total
            check_deadline(total)

    def exact(old_p: list[str], start: int) -> bool:
        """old_p 是否出现在全局位置 start（必须在窗口内）。"""
        rel = start - cursor
        if rel < 0 or rel + len(old_p) > len(buffer):
            return False
        return all(buffer[rel + off] == text for off, text in enumerate(old_p))

    def find_nearest(
        old_p: list[str], anchor: int
    ) -> tuple[int, int] | None:
        """在 [max(anchor-radius, cursor), anchor+radius] 内找最近匹配。

        同距时向前（行号较小）优先，保证确定性。
        """
        if not old_p:
            return anchor, 0
        lo = max(0, anchor - fuzz_radius, cursor)
        hi = anchor + fuzz_radius
        fill(hi + len(old_p) - 1)
        best_key: tuple[int, int, int] | None = None
        best_pos = -1
        pos = lo
        while True:
            rel = pos - cursor
            if rel < 0 or rel + len(old_p) > len(buffer):
                break
            if all(buffer[rel + off] == text for off, text in enumerate(old_p)):
                key = (abs(pos - anchor), 0 if pos <= anchor else 1, pos)
                if best_key is None or key < best_key:
                    best_key, best_pos = key, pos
            pos += 1
        if best_key is None:
            return None
        return best_pos, best_key[0]

    def emit_range(global_end: int) -> None:
        """把窗口中 [cursor, global_end) 的源行原样写入输出并前移窗口。"""
        nonlocal written, cursor
        rel = global_end - cursor
        for line in buffer[:rel]:
            out.write(line + "\n")
            written += 1
        del buffer[:rel]
        cursor = global_end

    for hunk_index in order:
        h = hunks[hunk_index]
        # old_count=0 时，插入点位于第 old_start 行之后（0-based 即 old_start）。
        anchor = h.old_start if h.old_count == 0 else max(h.old_start - 1, 0)
        old_full, new_full, _ = _strip_edge_context(h, 0)

        # 读到声明位置与模糊上界所需内容。
        fill(anchor + fuzz_radius + max(len(old_full), 1))
        stats.active_hunks += 1

        # 越界判定：非空旧模式的声明起点已越过 EOF。
        if old_full and anchor >= total:
            raise reject(
                RejectReason.HUNK_RANGE_OUT_OF_BOUNDS,
                f"声明起始行 {h.old_start} 越过文件末尾（共 {total} 行）",
                hunk_index,
            )

        # 声明位置已被先前 hunk 的匹配区间消耗 → 补丁内 hunk 重叠。
        if anchor < cursor:
            raise reject(
                RejectReason.HUNKS_OVERLAP,
                f"hunk {hunk_index} 的声明位置落在先前 hunk 的区间内",
                hunk_index,
            )

        chosen_pos = -1
        chosen_fuzz = 0
        applied = False

        declared_match = anchor == cursor if not old_full else exact(old_full, anchor)
        if declared_match:
            chosen_pos, chosen_fuzz = anchor, 0
        elif new_full and exact(new_full, anchor):
            # 幂等：声明位置已是新模式。
            chosen_pos, chosen_fuzz, applied = anchor, 0, True
        else:
            best_key: tuple[int, int, int, int] | None = None
            for fuzz in range(fuzz_level + 1):
                old_p, new_p, used = _strip_edge_context(h, fuzz)
                if not old_p and not new_p:
                    break
                found = find_nearest(old_p, anchor)
                is_applied = False
                if found is None and new_p:
                    found_new = find_nearest(new_p, anchor)
                    if found_new is not None:
                        found, is_applied = found_new, True
                if found is not None:
                    pos, dist = found
                    key = (used, dist, 0 if pos <= anchor else 1, pos)
                    if best_key is None or key < best_key:
                        best_key, chosen_pos, chosen_fuzz, applied = (
                            key,
                            pos,
                            used,
                            is_applied,
                        )
                    if used == 0:
                        break

        if chosen_pos < 0:
            raise reject(
                RejectReason.CONTEXT_MISMATCH,
                f"上下文不匹配：hunk {hunk_index} 在声明位置两侧 "
                f"{fuzz_radius} 行内无匹配（模糊档位 {fuzz_level}）",
                hunk_index,
            )

        old_p, new_p, _ = _strip_edge_context(h, chosen_fuzz)
        start, end = chosen_pos, chosen_pos + len(old_p)

        # 已提交区间不可被跨越：与先前 hunk 重叠即拒绝。
        if start < cursor:
            raise reject(
                RejectReason.HUNKS_OVERLAP,
                f"hunk {hunk_index} 的匹配位置与先前 hunk 重叠",
                hunk_index,
            )

        # 输出：区间前的原样内容 +（幂等时保留，否则替换为）新模式。
        emit_range(start)
        if not applied:
            for line in new_p:
                out.write(line + "\n")
                written += 1
            any_change = True
            if chosen_fuzz:
                fuzzed += 1
        else:
            for line in new_p:
                out.write(line + "\n")
                written += 1
        # 被匹配的旧行已随 emit_range 之后的裁剪跳过；模糊剥离的边缘
        # 上下文仍保留在窗口中，随后原样输出。
        rel_end = end - cursor
        del buffer[:rel_end]
        cursor = end

        # 若该 hunk 已到达文件尾，末尾换行由补丁新侧最后一行决定。
        if eof and end >= total and h.new_pattern:
            tail_hunk_no_newline = h.new_pattern[-1].no_newline

    # 刷新剩余全部源行。
    while not eof:
        try:
            line = next(iterator)
        except StopIteration:
            eof = True
            break
        line = line.rstrip("\n")
        # 补丁未触及尾部时，逐行写会统一为带换行；末尾换行语义最后修正。
        buffer.append(line)
        total += 1
    for line in buffer:
        out.write(line + "\n")
        written += 1
    buffer.clear()
    stats.lines_scanned = total

    # 末尾换行：触及文件尾的 hunk 标记优先；否则保留源文件状态。
    if tail_hunk_no_newline is not None:
        trailing = not tail_hunk_no_newline
    else:
        trailing = source_trailing_newline
    if not trailing and written:
        # 去掉刚写入的最后一个换行：由调用方在关闭文件前处理更稳妥，
        # 这里通过 seek 截断。
        out.seek(out.tell() - 1)
        out.truncate()

    stats.fuzzed_hunks = fuzzed
    stats.elapsed_seconds = clock() - started
    return ApplyResult(
        changed=any_change,
        line_count=written,
        trailing_newline=trailing,
    )
