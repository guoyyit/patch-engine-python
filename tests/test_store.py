import pytest

from patch_engine.store import FileContent, VersionConflict, Workspace


@pytest.fixture()
def ws(tmp_path):
    workspace = Workspace(tmp_path / "store")
    workspace.setup()
    return workspace


def test_stage_and_commit_materializes(ws) -> None:
    staging = ws.prepare_staging("b1")
    blob = ws.write_staged_blob(staging, FileContent(["a", "b"], True))
    with ws.transaction_lock():
        ws.journal.append_pending("b1")
        ws.commit_manifest("b1", staging, {"f.txt": blob}, {"f.txt": None})
        ws.journal.append_committed("b1")
    assert (ws.wc_dir / "f.txt").read_text() == "a\nb\n"
    assert ws.current_version("f.txt") == blob


def test_version_conflict_rejects_stale_submitter(ws) -> None:
    staging = ws.prepare_staging("b1")
    blob = ws.write_staged_blob(staging, FileContent(["x"], True))
    with ws.transaction_lock():
        ws.commit_manifest("b1", staging, {"f": blob}, {"f": None})
    # 第二方仍以为 f 不存在
    staging2 = ws.prepare_staging("b2")
    blob2 = ws.write_staged_blob(staging2, FileContent(["y"], True))
    with ws.transaction_lock(), pytest.raises(VersionConflict):
        ws.commit_manifest("b2", staging2, {"f": blob2}, {"f": None})
    # 被拒后工作副本不变
    assert ws.read_content("f").lines == ["x"]


def test_recover_pending_after_manifest_switch_is_committed(ws) -> None:
    staging = ws.prepare_staging("b1")
    blob = ws.write_staged_blob(staging, FileContent(["v1"], True))
    with ws.transaction_lock():
        ws.journal.append_pending("b1")
        ws.commit_manifest("b1", staging, {"f": blob}, {"f": None})
        # 崩溃发生在 append_committed 之前
    result = ws.recover()
    assert result["b1"] == "committed"
    assert ws.read_content("f").lines == ["v1"]


def test_recover_pending_before_switch_is_aborted(ws) -> None:
    # 先前已有一个已提交批次
    s0 = ws.prepare_staging("b0")
    b0 = ws.write_staged_blob(s0, FileContent(["old"], True))
    with ws.transaction_lock():
        ws.journal.append_pending("b0")
        ws.commit_manifest("b0", s0, {"f": b0}, {"f": None})
        ws.journal.append_committed("b0")
    # b1 写了 pending 与暂存，但没切换清单就崩溃
    s1 = ws.prepare_staging("b1")
    ws.write_staged_blob(s1, FileContent(["new"], True))
    with ws.transaction_lock():
        ws.journal.append_pending("b1")
    result = ws.recover()
    assert result["b0"] == "committed"
    assert result["b1"] == "aborted"
    assert ws.read_content("f").lines == ["old"]
    assert not s1.exists()


def test_orphan_staging_cleaned(ws) -> None:
    s = ws.prepare_staging("ghost")
    ws.write_staged_blob(s, FileContent(["g"], True))
    ws.recover()
    assert not s.exists()


def test_path_traversal_rejected(ws) -> None:
    with pytest.raises(ValueError):
        ws._wc_file("../escape.txt")
