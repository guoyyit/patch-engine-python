"""HTTP surface: status codes, rejection reasons, retryable conflicts."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from patch_engine.api import create_app

from .conftest import seed_file

CHANGE_B = "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"


@pytest.fixture
def client(svc_factory):
    svc = svc_factory()
    return TestClient(create_app(svc)), svc


def test_health(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "metrics" in r.json()


def test_submit_and_read(client):
    c, svc = client
    seed_file(svc, "f.txt", "a\nb\nc\n")
    r = c.post("/batches", json={"diffs": [CHANGE_B], "batch_id": "h1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_id"] == "h1"
    assert body["applied"][0]["path"] == "f.txt"
    assert c.get("/files/f.txt").json()["content"] == "a\nB\nc\n"
    st = c.get("/files/f.txt/status").json()
    assert st["version"] == 1


def test_out_of_bounds_reason(client):
    c, svc = client
    seed_file(svc, "f.txt", "a\nb\nc\n")
    bad = "--- a/g.txt\n+++ b/g.txt\n@@ -9,1 +9,1 @@\n-z\n+Z\n"
    r = c.post("/batches", json={"diffs": [bad]})
    assert r.status_code == 409
    assert r.json()["reason"] == "line_range_out_of_bounds"
    assert r.json()["file"] == "g.txt"


def test_context_mismatch_reason(client):
    c, svc = client
    seed_file(svc, "f.txt", "a\nb\nc\n")
    bad = "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n x\n-y\n+Y\n z\n"
    r = c.post("/batches", json={"diffs": [bad]})
    assert r.status_code == 409
    assert r.json()["reason"] == "context_mismatch"


def test_malformed_patch_422(client):
    c, _ = client
    r = c.post("/batches", json={"diffs": ["not a diff"]})
    assert r.status_code == 422
    assert r.json()["reason"] == "empty_patch"


def test_stale_base_conflict_then_retry(client):
    c, svc = client
    seed_file(svc, "f.txt", "a\nb\nc\n")
    c.post("/batches", json={"diffs": [CHANGE_B], "batch_id": "v1"})
    ok2 = "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n a\n-B\n+X\n c\n"
    r = c.post("/batches", json={"diffs": [ok2], "base_versions": {"f.txt": 0}})
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "conflict" and body["retryable"] is True
    r2 = c.post("/batches", json={"diffs": [ok2], "base_versions": {"f.txt": 1}})
    assert r2.status_code == 200
    assert c.get("/files/f.txt").json()["content"] == "a\nX\nc\n"


def test_batch_partial_failure_leaves_no_write(client):
    c, svc = client
    seed_file(svc, "f.txt", "a\nb\nc\n")
    bad = "--- a/g.txt\n+++ b/g.txt\n@@ -9,1 +9,1 @@\n-z\n+Z\n"
    r = c.post("/batches", json={"diffs": [CHANGE_B, bad], "batch_id": "atomic"})
    assert r.status_code == 409
    assert c.get("/files/f.txt").json()["content"] == "a\nb\nc\n"


def test_recover_endpoint(client):
    c, _ = client
    r = c.post("/recover")
    assert r.status_code == 200
    assert "recovered" in r.json()


def test_unknown_file_404(client):
    c, _ = client
    assert c.get("/files/nope.txt").status_code == 404
