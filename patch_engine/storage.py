"""Durable storage: content-addressed blobs, working files, state pointer.

On-disk layout under *root*::

    blobs/<sha256>       immutable content objects
    work/<relpath>       current working copy of each tracked file
    state.json           committed state pointer (atomic rename)
    state.json.tmp.*     staged (uncommitted) pointers, cleaned on recovery

Every write that must survive a crash is fsync'd; ``state.json`` is
published exclusively through a temp-file + ``os.replace`` so readers
either see the previous or the next complete state, never a torn one.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from .textutil import split_lines


@dataclass(frozen=True)
class FileState:
    version: int
    blob: str
    size: int


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def safe_join(root: Path, rel: str) -> Path:
    """Join *rel* under *root*, refusing absolute paths and ``..`` escapes."""
    if not rel or rel.strip() in (".", ""):
        raise ValueError(f"invalid path: {rel!r}")
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"path escapes storage root: {rel!r}")
    full = (root / p).resolve()
    root_resolved = root.resolve()
    if full != root_resolved and root_resolved not in full.parents:
        raise ValueError(f"path escapes storage root: {rel!r}")
    return full


class Storage:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.blobs_dir = self.root / "blobs"
        self.work_dir = self.root / "work"
        self.state_path = self.root / "state.json"
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ---- state pointer ----------------------------------------------
    def load_state(self) -> dict[str, FileState]:
        if not self.state_path.exists():
            return {}
        with open(self.state_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {
            path: FileState(version=int(v["version"]), blob=v["blob"], size=int(v["size"]))
            for path, v in raw.get("files", {}).items()
        }

    def serialize_state(self, states: dict[str, FileState]) -> str:
        return json.dumps(
            {"files": {p: asdict(s) for p, s in sorted(states.items())}},
            sort_keys=True,
            indent=2,
        )

    def write_state_atomic(
        self, states: dict[str, FileState], tmp_suffix: str = ""
    ) -> Path:
        """Stage a new state pointer at ``state.json.tmp[.suffix]``."""
        tmp = self.state_path.with_name(
            "state.json.tmp" + (f".{tmp_suffix}" if tmp_suffix else "")
        )
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(self.serialize_state(states))
            f.flush()
            os.fsync(f.fileno())
        return tmp

    def commit_state(self, tmp_path: Path) -> None:
        os.replace(tmp_path, self.state_path)
        _fsync_dir(self.root)

    def list_staged_state_files(self) -> list[Path]:
        return sorted(self.root.glob("state.json.tmp*"))

    def cleanup_staged_state(self) -> None:
        for tmp in self.list_staged_state_files():
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    # ---- blobs -------------------------------------------------------
    def blob_path(self, digest: str) -> Path:
        if not digest or "/" in digest or ".." in digest:
            raise ValueError(f"bad blob digest: {digest!r}")
        return self.blobs_dir / digest

    def read_blob(self, digest: str) -> str:
        with open(self.blob_path(digest), "r", encoding="utf-8", newline="") as f:
            return f.read()

    def has_blob(self, digest: str) -> bool:
        return self.blob_path(digest).is_file()

    def write_blob(self, content: str) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        path = self.blob_path(digest)
        if path.exists():
            return digest
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(self.blobs_dir)
        return digest

    def write_blob_from_file(self, src: str | Path, *, block_bytes: int = 1 << 20) -> tuple[str, int]:
        """Stream *src* into the blob store; return ``(digest, byte_size)``."""
        src = Path(src)
        size = 0
        hasher = hashlib.sha256()
        with open(src, "rb") as f:
            while True:
                block = f.read(block_bytes)
                if not block:
                    break
                hasher.update(block)
                size += len(block)
        digest = hasher.hexdigest()
        dest = self.blob_path(digest)
        if not dest.exists():
            tmp = dest.with_name(dest.name + ".tmp")
            with open(src, "rb") as fin, open(tmp, "wb") as fout:
                while True:
                    block = fin.read(block_bytes)
                    if not block:
                        break
                    fout.write(block)
                fout.flush()
                os.fsync(fout.fileno())
            os.replace(tmp, dest)
            _fsync_dir(self.blobs_dir)
        return digest, size

    # ---- working copies ---------------------------------------------
    def read_text(self, rel: str) -> str:
        with open(safe_join(self.work_dir, rel), "r", encoding="utf-8", newline="") as f:
            return f.read()

    def read_lines(self, rel: str) -> list[str]:
        return split_lines(self.read_text(rel))

    def write_working_file(self, rel: str, content: str) -> None:
        path = safe_join(self.work_dir, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)

    def copy_blob_to_working(
        self, digest: str, rel: str, *, block_bytes: int = 1 << 20
    ) -> None:
        """Stream a blob into a working copy without loading it fully."""
        path = safe_join(self.work_dir, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(self.blob_path(digest), "rb") as fin, open(tmp, "wb") as fout:
            while True:
                block = fin.read(block_bytes)
                if not block:
                    break
                fout.write(block)
            fout.flush()
            os.fsync(fout.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)

    def remove_working_file(self, rel: str) -> None:
        path = safe_join(self.work_dir, rel)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        parent = path.parent
        while parent != self.work_dir and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    def working_file_exists(self, rel: str) -> bool:
        return safe_join(self.work_dir, rel).exists()

    # ---- helpers -----------------------------------------------------
    def content_for_state(self, state: FileState | None) -> str:
        if state is None:
            return ""
        return self.read_blob(state.blob)

    def lines_for_state(self, state: FileState | None) -> list[str]:
        return split_lines(self.content_for_state(state))
