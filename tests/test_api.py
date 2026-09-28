import pytest
from fastapi.testclient import TestClient

from patch_engine.api import create_app


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(tmp_path / "ws"))


CREATE_A = (
    "--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1,3 @@\n+one\n+two\n+three\n"
)


def test_health(client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_submit_and_read(client) -> None:
    resp = client.post("/batches", json={"patches": [CREATE_A]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["no_change"] is False
    assert "a.txt" in body["versions"]
    assert client.get("/files/a.txt").text == "one\ntwo\nthree\n"
    assert client.get("/versions").json()["a.txt"] == body["versions"]["a.txt"]


def test_reject_reason_is_structured(client) -> None:
    client.post("/batches", json={"patches": [CREATE_A]})
    bad = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n"
        " a\n-WRONG\n+X\n c\n"
    )
    resp = client.post(
        "/batches", json={"patches": [bad], "fuzz_radius": 0}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "patch_rejected"
    assert detail["reason"] == "context_mismatch"


def test_out_of_bounds_reason(client) -> None:
    client.post("/batches", json={"patches": [CREATE_A]})
    bad = "--- a/a.txt\n+++ b/a.txt\n@@ -100,1 +100,1 @@\n-x\n+y\n"
    resp = client.post(
        "/batches", json={"patches": [bad], "fuzz_radius": 0}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "hunk_range_out_of_bounds"


def test_malformed_patch(client) -> None:
    resp = client.post("/batches", json={"patches": ["not a diff"]})
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "malformed_patch"


def test_version_conflict_409(client) -> None:
    client.post("/batches", json={"patches": [CREATE_A]})
    version = client.get("/versions").json()["a.txt"]
    client.post(
        "/batches",
        json={
            "patches": [
                "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-one\n+ONE\n"
            ]
        },
    )
    resp = client.post(
        "/batches",
        json={
            "patches": [
                "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-one\n+TWO\n"
            ],
            "expected_versions": {"a.txt": version},
        },
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "version_conflict"


def test_idempotent_submit(client) -> None:
    first = client.post("/batches", json={"patches": [CREATE_A]}).json()
    second = client.post("/batches", json={"patches": [CREATE_A]}).json()
    assert second["versions"] == first["versions"]
    assert second["no_change"] is True


def test_batch_atomicity_via_api(client) -> None:
    client.post("/batches", json={"patches": [CREATE_A]})
    resp = client.post(
        "/batches",
        json={
            "patches": [
                "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-one\n+ONE\n",
                "--- a/ghost\n+++ b/ghost\n@@ -1,1 +1,1 @@\n-x\n+y\n",
            ]
        },
    )
    assert resp.status_code == 422
    assert client.get("/files/a.txt").text == "one\ntwo\nthree\n"
