"""unified diff 解析器。

支持的 unified diff 要素：

* ``diff --git a/x b/x`` / ``---``/``+++`` 文件头（含 ``/dev/null`` 新建、删除）
* ``rename from``/``rename to``、``new file mode``/``deleted file mode``
* ``@@ -a,b +c,d @@`` hunk 头与可选节标题
* 上下文行 `` ``、删除行 ``-``、新增行 ``+``
* ``\\ No newline at end of file`` 标记

解析错误一律抛出 :class:`~patch_engine.model.PatchRejected`，
原因为 ``MALFORMED_PATCH``，不会产出部分解析结果。
"""

from __future__ import annotations

import re

from .model import (
    FileOperation,
    FilePatch,
    Hunk,
    HunkLine,
    Patch,
    PatchRejectDetail,
    PatchRejected,
    RejectReason,
)

_HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$"
)
_PREFIXES = ("a/", "b/")
_NO_NEWLINE = "\\ No newline at end of file"


def _strip_prefix(path: str) -> str:
    for prefix in _PREFIXES:
        if path.startswith(prefix) and len(path) > 2:
            return path[2:]
    return path


def _reject(message: str, path: str = "<patch>") -> PatchRejected:
    return PatchRejected(
        PatchRejectDetail(
            path=path, reason=RejectReason.MALFORMED_PATCH, message=message
        )
    )


def parse_unified_diff(text: str) -> Patch:
    """解析 unified diff 文本为 :class:`Patch`。"""
    # 统一为 LF，保留“无末尾换行”语义由 \ 标记表达。
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = text.split("\n")
    # text.split 总会产生末尾元素；若原文以 \n 结尾，最后一个元素为空串，应丢弃。
    if raw_lines and raw_lines[-1] == "" and text.endswith("\n"):
        raw_lines.pop()

    files: list[FilePatch] = []

    # 游标式状态机。每个文件块从遇到 diff/--- 头开始。
    i = 0
    n = len(raw_lines)
    while i < n:
        line = raw_lines[i]
        if (
            line.startswith("diff ")
            or line.startswith("--- ")
            or line.startswith("---\t")
        ):
            file_patch, i = _parse_file(raw_lines, i)
            files.append(file_patch)
            continue
        if line.strip() == "":
            i += 1
            continue
        # 仅在尚未解析出任何文件时容忍的头部噪声
        if not files and line in ("index x",) :
            i += 1
            continue
        raise _reject(f"无法识别的补丁行: {line!r}")

    if not files:
        raise _reject("补丁中不包含任何文件块")

    return Patch(files=tuple(files), source=text)


def _parse_path_header(line: str) -> str:
    # --- path 或 ---\tpath
    body = line[4:]
    body = body.removeprefix("\t")
    return _strip_prefix(body)


def _parse_file(
    lines: list[str], start: int
) -> tuple[FilePatch, int]:
    n = len(lines)
    i = start
    old_header: str | None = None
    new_header: str | None = None
    old_mode: str | None = None
    new_mode: str | None = None
    rename_from: str | None = None
    rename_to: str | None = None
    hunks: list[Hunk] = []
    new_file = False
    deleted_file = False

    if lines[i].startswith("diff "):
        # diff --git a/foo b/foo：取 b/ 侧作为提示路径
        m = re.match(r"^diff --git (.+) (.+)$", lines[i])
        if not m:
            raise _reject(f"无法解析 diff 头: {lines[i]!r}")
        i += 1

    while i < n:
        line = lines[i]
        if line.startswith("--- ") or line.startswith("---\t"):
            old_header = _parse_path_header(line)
            i += 1
            if i < n and (
                lines[i].startswith("+++ ") or lines[i].startswith("+++\t")
            ):
                new_header = _parse_path_header(lines[i])
                i += 1
            else:
                raise _reject("'---' 后缺少 '+++' 头")
            break
        if line.startswith("new file mode"):
            new_file = True
            new_mode = line.split()[-1]
            i += 1
            continue
        if line.startswith("deleted file mode"):
            deleted_file = True
            old_mode = line.split()[-1]
            i += 1
            continue
        if line.startswith("old mode"):
            old_mode = line.split()[-1]
            i += 1
            continue
        if line.startswith("new mode"):
            new_mode = line.split()[-1]
            i += 1
            continue
        if line.startswith("rename from "):
            rename_from = line[len("rename from ") :]
            i += 1
            continue
        if line.startswith("rename to "):
            rename_to = line[len("rename to ") :]
            i += 1
            continue
        if line.startswith("index "):
            i += 1
            continue
        if line.startswith("similarity index ") or line.startswith(
            "dissimilarity index "
        ):
            i += 1
            continue
        if line.startswith("copy from ") or line.startswith("copy to "):
            i += 1
            continue
        if line.startswith("@@ "):
            raise _reject("缺少文件头即出现 hunk")
        if line.startswith("diff ") or (
            line.startswith("--- ") or line.startswith("---\t")
        ):
            break
        raise _reject(f"无法识别的文件头行: {line!r}")

    # 解析 hunks
    while i < n:
        line = lines[i]
        if line.startswith("@@ "):
            hunk, i = _parse_hunk(lines, i)
            hunks.append(hunk)
            continue
        if line.startswith("diff ") or line.startswith("--- ") or line.startswith(
            "---\t"
        ):
            break
        if line.strip() == "":
            i += 1
            continue
        raise _reject(f"hunk 之后的非法行: {line!r}")

    old_path: str | None
    new_path: str | None
    old_path = None if old_header == "/dev/null" else old_header
    new_path = None if new_header == "/dev/null" else new_header

    if rename_from is not None and rename_to is not None:
        operation = FileOperation.RENAME
        old_path = rename_from
        new_path = rename_to
    elif new_file or (old_path is None and new_path is not None):
        operation = FileOperation.CREATE
        if new_path is None:
            raise _reject("新建文件缺少 '+++' 路径")
    elif deleted_file or (new_path is None and old_path is not None):
        operation = FileOperation.DELETE
        if old_path is None:
            raise _reject("删除文件缺少 '---' 路径")
    else:
        operation = FileOperation.MODIFY
        if old_path is None or new_path is None:
            raise _reject("修改文件需要同时给出 '---' 与 '+++' 路径")

    if operation is FileOperation.MODIFY and not hunks:
        raise _reject(f"文件 {new_path} 的补丁不包含任何 hunk")

    return (
        FilePatch(
            operation=operation,
            old_path=old_path,
            new_path=new_path,
            hunks=tuple(hunks),
            old_mode=old_mode,
            new_mode=new_mode,
        ),
        i,
    )


def _parse_hunk(lines: list[str], start: int) -> tuple[Hunk, int]:
    m = _HUNK_HEADER_RE.match(lines[start])
    if not m:
        raise _reject(f"无法解析 hunk 头: {lines[start]!r}")
    old_start = int(m.group(1))
    old_count = int(m.group(2)) if m.group(2) is not None else 1
    new_start = int(m.group(3))
    new_count = int(m.group(4)) if m.group(4) is not None else 1
    heading = m.group(5).strip() or None

    body: list[HunkLine] = []
    i = start + 1
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("@@ ") or line.startswith("diff ") or line.startswith(
            "--- "
        ):
            break
        if line == _NO_NEWLINE:
            if not body:
                raise _reject("'No newline' 标记前没有内容行")
            body[-1] = HunkLine(body[-1].kind, body[-1].text, True)
            i += 1
            continue
        if line == "" and i == n - 1:
            # 末尾允许一个空行（无内容），不当作上下文空行，避免误吞后续块。
            break
        if line and line[0] in (" ", "-", "+"):
            body.append(HunkLine(line[0], line[1:]))
            i += 1
            continue
        if line == "":
            # unified diff 中上下文空行为单个空格；裸空行视为块结束。
            break
        raise _reject(f"hunk 内非法行: {line!r}")

    actual_old = sum(1 for ln in body if ln.kind in (" ", "-"))
    actual_new = sum(1 for ln in body if ln.kind in (" ", "+"))
    if actual_old != old_count or actual_new != new_count:
        raise _reject(
            f"hunk 行计数与内容不符: 头声明 -{old_count},+{new_count}，"
            f"实际 -{actual_old},+{actual_new}"
        )

    return (
        Hunk(
            old_start=old_start,
            old_count=old_count,
            new_start=new_start,
            new_count=new_count,
            lines=tuple(body),
            section_heading=heading,
        ),
        i,
    )
