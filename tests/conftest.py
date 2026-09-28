"""Shared test helpers."""

from __future__ import annotations

import os

import pytest

from patch_engine.service import PatchService
from patch_engine.storage import FileState


@pytest.fixture
def svc_factory(tmp_path):
    """A fresh PatchService factory with helper to seed tracked files."""
    created: list[PatchService] = []

    def _make(name: str = "engine") -> PatchService:
        service = PatchService(tmp_path / name)
        created.append(service)
        return service

    yield _make

    for service in created:
        pass  # tmp_path cleanup handles files


def seed_file(service: PatchService, path: str, content: str, version: int = 0) -> None:
    """Create a tracked file at a given committed version."""
    service.storage.write_working_file(path, content)
    digest = service.storage.write_blob(content)
    states = service.storage.load_state()
    states[path] = FileState(
        version=version, blob=digest, size=len(content.encode("utf-8"))
    )
    tmp = service.storage.write_state_atomic(states, "seed")
    service.storage.commit_state(tmp)
    # keep WAL empty w.r.t. the seed (simulates pre-existing content)
    assert os.path.isfile(service.storage.state_path)
