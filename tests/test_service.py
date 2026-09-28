import threading

import pytest

from patch_engine.model import PatchRejected, RejectReason
from patch_engine.service import PatchService
from patch_engine.store import VersionConflict


@pytest.fixture()
def service(tmp_path):
    svc = PatchService(tmp_path / "ws", fuzz_radius=10, fuzz_level=1)
    svc.recover()
    return svc


CREATE_A = (
    "--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1,3 @@\n+one\n+two\n+three\n"
)


def test_batch_success_and_persistence(service) -> None:
    result = service.submit([CREATE_A])
    assert "a.txt" in result.versions
    assert (service.workspace.wc_dir / "a.txt").read_text() == (
        "one\ntwo\nthree\n"
    )


def test_batch_rollback_on_second_patch_failure(service) -> None:
    service.submit([CREATE_A])
    before = (service.workspace.wc_dir / "a.txt").read_text()
    good = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n"
        "-one\n+ONE\n"
    )
    bad = (
        "--- a/missing.txt\n+++ b/missing.txt\n@@ -1,1 +1,1 @@\n"
        "-x\n+X\n"
    )
    with pytest.raises(PatchRejected) as ei:
        service.submit([good, bad])
    assert ei.value.detail.reason in (
        RejectReason.TARGET_MISSING,
        RejectReason.CONTEXT_MISMATCH,
    )
    # 工作副本必须保持提交前状态
    assert (service.workspace.wc_dir / "a.txt").read_text() == before
    # 暂存目录已清空
    assert not any(service.workspace.staging_dir.iterdir())


def test_idempotent_reapply(service) -> None:
    r1 = service.submit([CREATE_A])
    r2 = service.submit([CREATE_A])
    assert r2.versions["a.txt"] == r1.versions["a.txt"]
    assert r2.no_change is True


def test_independent_patches_reorder_consistent(service) -> None:
    service.submit([CREATE_A])
    create_b = (
        "--- /dev/null\n+++ b/b.txt\n@@ -0,0 +1,1 @@\n+B\n"
    )
    patch_a = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n"
        "-one\n+ONE\n"
    )
    r1 = service.submit([patch_a, create_b]).versions
    # 重建到公共前态再换序
    ws2_dir = service.workspace.root.parent / "ws2"
    svc2 = PatchService(ws2_dir, fuzz_radius=10, fuzz_level=1)
    svc2.submit([CREATE_A])
    r2 = svc2.submit([create_b, patch_a]).versions
    assert r1 == r2


def test_stale_version_submit_rejected_without_overwrite(service) -> None:
    # 基线版本
    service.submit([CREATE_A])
    base_version = service.workspace.current_version("a.txt")

    # 一方基于最新版本成功提交
    service.submit(
        [
            "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-one\n+TWO\n"
        ]
    )
    # 另一方仍持有旧 base_version 后提交：必须版本冲突，不得覆盖。
    with pytest.raises(VersionConflict) as ei:
        service.submit(
            [
                "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-one\n+ONE\n"
            ],
            expected_versions={"a.txt": base_version},
        )
    assert ei.value.expected == base_version
    assert ei.value.actual != base_version
    text = (service.workspace.wc_dir / "a.txt").read_text()
    assert "TWO" in text and "ONE" not in text


def test_stale_version_can_retry_on_latest(service) -> None:
    service.submit([CREATE_A])
    v1 = service.workspace.current_version("a.txt")
    service.submit(
        ["--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-one\n+TWO\n"]
    )
    v2 = service.workspace.current_version("a.txt")
    # 旧版本被拒后，调用方刷新到最新版本并重试（基于新内容）成功。
    with pytest.raises(VersionConflict):
        service.submit(
            [
                "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-one\n+ONE\n"
            ],
            expected_versions={"a.txt": v1},
        )
    result = service.submit(
        ["--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-two\n+TWO2\n"],
        expected_versions={"a.txt": v2},
    )
    assert (service.workspace.wc_dir / "a.txt").read_text().startswith(
        "TWO\nTWO2\n"
    )
    assert result.versions["a.txt"] != v2


def test_concurrent_same_file_one_wins_one_conflicts(service) -> None:
    service.submit([CREATE_A])
    base_version = service.workspace.current_version("a.txt")
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker(change_to: str) -> None:
        barrier.wait()
        try:
            service.submit(
                [
                    "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n"
                    f"-one\n+{change_to}\n"
                ],
                expected_versions={"a.txt": base_version},
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("AAA",)),
        threading.Thread(target=worker, args=("BBB",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 1
    assert isinstance(errors[0], VersionConflict)
    text = (service.workspace.wc_dir / "a.txt").read_text()
    assert ("AAA" in text) ^ ("BBB" in text)


def test_concurrent_modify_different_files_both_succeed(service) -> None:
    service.submit([CREATE_A])
    create_b = (
        "--- /dev/null\n+++ b/b.txt\n@@ -0,0 +1,1 @@\n+B\n"
    )
    service.submit([create_b])
    va = service.workspace.current_version("a.txt")
    vb = service.workspace.current_version("b.txt")
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker(diff: str, path: str, ver: str) -> None:
        barrier.wait()
        try:
            service.submit([diff], expected_versions={path: ver})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(
        target=worker,
        args=(
            "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-one\n+ONE\n",
            "a.txt",
            va,
        ),
    )
    t2 = threading.Thread(
        target=worker,
        args=(
            "--- a/b.txt\n+++ b/b.txt\n@@ -1,1 +1,1 @@\n-B\n+BB\n",
            "b.txt",
            vb,
        ),
    )
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []
    assert (service.workspace.wc_dir / "a.txt").read_text().startswith("ONE\n")
    assert (service.workspace.wc_dir / "b.txt").read_text() == "BB\n"


def test_rename_in_batch(service) -> None:
    service.submit([CREATE_A])
    rename = "diff --git a/a.txt b/renamed.txt\nrename from a.txt\nrename to renamed.txt\n"
    service.submit([rename])
    assert not (service.workspace.wc_dir / "a.txt").exists()
    assert (service.workspace.wc_dir / "renamed.txt").read_text().startswith(
        "one\n"
    )


def test_recover_after_restart_keeps_state(tmp_path) -> None:
    svc = PatchService(tmp_path / "ws", fuzz_radius=10, fuzz_level=1)
    svc.submit([CREATE_A])
    # 用新实例模拟进程重启
    svc2 = PatchService(tmp_path / "ws", fuzz_radius=10, fuzz_level=1)
    result = svc2.recover()
    assert all(state == "committed" for state in result.values())
    assert (svc2.workspace.wc_dir / "a.txt").exists()


def test_reapply_after_restart_idempotent(tmp_path) -> None:
    svc = PatchService(tmp_path / "ws", fuzz_radius=10, fuzz_level=1)
    r1 = svc.submit([CREATE_A])
    svc2 = PatchService(tmp_path / "ws", fuzz_radius=10, fuzz_level=1)
    svc2.recover()
    r2 = svc2.submit([CREATE_A])
    assert r2.versions == r1.versions


def test_delete_with_wrong_context_does_not_delete(service) -> None:
    service.submit([
        "--- /dev/null\n+++ b/g.txt\n@@ -0,0 +1,2 @@\n+x\n+y\n"
    ])
    bad = "--- a/g.txt\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-WRONG\n-y\n"
    with pytest.raises(PatchRejected):
        service.submit([bad])
    assert (service.workspace.wc_dir / "g.txt").read_text() == "x\ny\n"
