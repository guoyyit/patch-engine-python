"""Line representation shared across the engine.

A file's lines are the substrings between ``\\n`` characters.  A trailing
``\\n`` is represented by an explicit empty string sentinel::

    ""            -> []                 # empty file
    "a\\n"         -> ["a", ""]
    "a\\nb"        -> ["a", "b"]        # no trailing newline
    "a\\nb\\n"     -> ["a", "b", ""]
"""

from __future__ import annotations

NEWLINE_SENTINEL = ""


def split_lines(content: str) -> list[str]:
    if content == "":
        return []
    lines = content.split("\n")
    return lines  # trailing "" (if any) is the sentinel, kept on purpose


def join_lines(lines: list[str], final_newline: bool | None = None) -> str:
    """Join lines.

    *final_newline=None* (default) infers from the trailing empty-string
    sentinel; ``True``/``False`` force the property, adding/dropping the
    sentinel as needed.
    """
    if not lines:
        return ""
    if final_newline is None:
        final_newline = lines[-1] == NEWLINE_SENTINEL
    if final_newline and lines[-1] != NEWLINE_SENTINEL:
        lines = [*lines, NEWLINE_SENTINEL]
    elif final_newline is False and lines[-1] == NEWLINE_SENTINEL:
        lines = lines[:-1]
    return "\n".join(lines)
