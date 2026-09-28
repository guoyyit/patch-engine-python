"""Unified-diff parsing.

Supported grammar (subset of GNU ``diff -u`` / ``git diff`` output)::

    "diff --git a/p b/p"        (zero or more)
    "new file mode <mode>"      (sets is_new_file)
    "deleted file mode <mode>"  (sets is_deleted_file)
    "--- <path>"                (exactly one per section; "/dev/null" for new files)
    "+++ <path>"                (exactly one per section; "/dev/null" for deletions)
    "@@ -l,c +l,c @@ [heading]" (one or more)
    body lines starting with ' ' (context), '+' (added) or '-' (deleted)
    "\\ No newline at end of file" markers

Counts declared in hunk headers are validated against the body.  Every
structural violation raises :class:`PatchParseError` carrying a distinct
:class:`RejectReason` and the 1-based source line of the failing text.
"""

from __future__ import annotations

import re

from .models import (
    FilePatch,
    Hunk,
    HunkLine,
    Patch,
    PatchParseError,
    RejectReason,
)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_PATH_TS_RE = re.compile(r"^(.*?)(?:\t\d{4}-\d\d-\d\d[ T].*)?$")


def _strip_path(raw: str) -> str:
    """Turn a ``---``/``+++`` operand into a filesystem path."""
    raw = raw.strip()
    m = _PATH_TS_RE.match(raw)
    if m:
        raw = m.group(1)
    if raw == "/dev/null":
        return "/dev/null"
    if raw.startswith(('"', "'")) and len(raw) >= 2 and raw[-1] == raw[0]:
        inner = raw[1:-1]
        try:
            inner = bytes(inner, "utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            pass
        raw = inner
    if raw[:2] in ("a/", "b/"):
        raw = raw[2:]
    return raw


def _split_lines(text: str) -> list[str]:
    """Split preserving content but dropping one trailing empty element."""
    if text == "":
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def parse_diff(text: str) -> Patch:
    """Parse unified-diff text into a :class:`Patch`."""
    if not text or not text.strip():
        raise PatchParseError(RejectReason.EMPTY_PATCH, "empty patch text", line=1)

    lines = _split_lines(text)
    file_patches: list[FilePatch] = []

    # Per-section parser state.
    old_path: str | None = None
    new_path: str | None = None
    is_new = False
    is_deleted = False
    seen_headers = False  # --- / +++ pair seen for current section
    hunks: list[Hunk] = []

    # Metadata ("new file mode"/"deleted file mode") precedes the "---"
    # header that opens the next section; hold it until that section starts.
    pending_is_new = False
    pending_is_deleted = False

    # Per-hunk parser state.
    body: list[HunkLine] | None = None
    old_no_nl = False
    new_no_nl = False
    hunk_decl = (0, 0, 0, 0)
    hunk_lineno = 0
    hunk_heading: str | None = None

    def flush_hunk(end_line: int) -> None:
        nonlocal body, old_no_nl, new_no_nl
        assert body is not None
        os_, oc, ns, nc = hunk_decl
        got_old = sum(1 for l in body if l.kind in (" ", "-"))
        got_new = sum(1 for l in body if l.kind in (" ", "+"))
        if got_old != oc or got_new != nc:
            raise PatchParseError(
                RejectReason.MALFORMED_HUNK,
                f"hunk header declares -{os_},{oc} +{ns},{nc} but body has "
                f"{got_old} old / {got_new} new lines",
                line=hunk_lineno,
            )
        hunks.append(
            Hunk(
                old_start=os_,
                old_count=oc,
                new_start=ns,
                new_count=nc,
                lines=tuple(body),
                section_heading=hunk_heading,
                old_no_newline=old_no_nl,
                new_no_newline=new_no_nl,
            )
        )
        body = None
        old_no_nl = False
        new_no_nl = False

    def flush_section() -> None:
        nonlocal old_path, new_path, is_new, is_deleted, seen_headers, hunks
        if seen_headers:
            if old_path is None or new_path is None:
                raise PatchParseError(
                    RejectReason.MALFORMED_HEADER,
                    "file section missing '---' or '+++' header",
                )
            file_patches.append(
                FilePatch(
                    old_path=old_path,
                    new_path=new_path,
                    hunks=tuple(hunks),
                    is_new_file=is_new,
                    is_deleted_file=is_deleted,
                )
            )
        old_path = None
        new_path = None
        is_new = False
        is_deleted = False
        seen_headers = False
        hunks = []

    i = 0
    while i < len(lines):
        line = lines[i]
        lineno = i + 1

        if body is not None:
            if line.startswith("\\"):
                if line.strip() != r"\ No newline at end of file":
                    raise PatchParseError(
                        RejectReason.MALFORMED_HUNK,
                        f"unrecognized marker line: {line!r}",
                        line=lineno,
                    )
                if not body:
                    raise PatchParseError(
                        RejectReason.MALFORMED_HUNK,
                        "'no newline' marker without preceding body line",
                        line=lineno,
                    )
                last = body[-1]
                if last.kind in (" ", "-"):
                    old_no_nl = True
                if last.kind in (" ", "+"):
                    new_no_nl = True
                i += 1
                continue
            # Consume exactly the counts declared in the hunk header.  This
            # disambiguates body lines (e.g. a deleted line starting "---")
            # from the next section's "--- path" header.
            _, oc, _, nc = hunk_decl
            got_old = sum(1 for l in body if l.kind in (" ", "-"))
            got_new = sum(1 for l in body if l.kind in (" ", "+"))
            need_old = got_old < oc
            need_new = got_new < nc
            marker = line[:1]
            is_body = False
            if need_old and need_new and marker in (" ", "-", "+"):
                is_body = True
            elif need_old and not need_new and marker in (" ", "-"):
                is_body = True
            elif need_new and not need_old and marker in (" ", "+"):
                is_body = True
            if is_body:
                body.append(HunkLine(kind=marker, text=line[1:]))
                i += 1
                continue
            if need_old or need_new:
                raise PatchParseError(
                    RejectReason.MALFORMED_HUNK,
                    f"expected hunk body line (' '/'+'/'-') but got {line!r}",
                    line=lineno,
                )
            # Hunk counts fully consumed: terminate and reprocess this line.
            flush_hunk(lineno)

        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if not m:
                raise PatchParseError(
                    RejectReason.MALFORMED_HUNK,
                    f"malformed hunk header: {line!r}",
                    line=lineno,
                )
            if not seen_headers:
                raise PatchParseError(
                    RejectReason.MALFORMED_HEADER,
                    "hunk before any '---'/'+++' file header",
                    line=lineno,
                )
            os_ = int(m.group(1))
            oc = int(m.group(2)) if m.group(2) is not None else 1
            ns = int(m.group(3))
            nc = int(m.group(4)) if m.group(4) is not None else 1
            hunk_decl = (os_, oc, ns, nc)
            hunk_lineno = lineno
            hunk_heading = m.group(5).lstrip() or None
            body = []
            i += 1
            continue

        if line.startswith("--- "):
            if body is not None:
                flush_hunk(lineno)
            flush_section()
            is_new = pending_is_new
            is_deleted = pending_is_deleted
            pending_is_new = False
            pending_is_deleted = False
            seen_headers = True
            old_path = _strip_path(line[4:])
            i += 1
            continue

        if line.startswith("+++ "):
            if not seen_headers or new_path is not None:
                raise PatchParseError(
                    RejectReason.MALFORMED_HEADER,
                    "'+++' without preceding '---'",
                    line=lineno,
                )
            new_path = _strip_path(line[4:])
            i += 1
            continue

        if line.startswith(("diff --git", "Index: ", "---", "+++")):
            # E.g. a bare "---" (no space) or git index lines we do not support.
            if line.startswith("---"):
                raise PatchParseError(
                    RejectReason.MALFORMED_HEADER,
                    f"malformed old-file header: {line!r}",
                    line=lineno,
                )
            # "diff --git ..." / "Index: ..." separate sections, but carry no path.
            if body is not None:
                flush_hunk(lineno)
            flush_section()
            i += 1
            continue

        if line.startswith("new file mode"):
            pending_is_new = True
            i += 1
            continue
        if line.startswith("deleted file mode"):
            pending_is_deleted = True
            i += 1
            continue

        # Ignore git-specific metadata and stray preamble lines outside hunks.
        i += 1

    if body is not None:
        flush_hunk(len(lines) + 1)
    flush_section()

    if not file_patches:
        raise PatchParseError(
            RejectReason.EMPTY_PATCH, "no valid file sections found", line=1
        )
    return Patch(files=tuple(file_patches), raw=text)
