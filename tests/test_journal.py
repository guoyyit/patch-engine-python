from patch_engine.journal import Journal, JournalState


def make(tmp_path):
    return Journal(tmp_path / "journal.log")


def test_pending_committed_roundtrip(tmp_path) -> None:
    j = make(tmp_path)
    j.append_pending("b1")
    j.append_committed("b1")
    states = j.recover_state()
    assert states == {"b1": JournalState.COMMITTED}
    assert j.last_committed_id() == "b1"
    assert j.batch_ids() == ["b1"]


def test_pending_without_commit_is_incomplete(tmp_path) -> None:
    j = make(tmp_path)
    j.append_pending("b2")
    assert j.recover_state() == {"b2": JournalState.PENDING}
    assert j.last_committed_id() is None


def test_torn_tail_truncated(tmp_path) -> None:
    j = make(tmp_path)
    j.append_pending("b1")
    j.append_committed("b1")
    good = j.path.read_bytes()
    # 模拟崩溃时写入了半截 JSON（无换行）。
    with j.path.open("ab") as fh:
        fh.write(b'{"type": "pending", "batch": "b2", "ts": 1.')
    states = j.recover_state()
    assert states == {"b1": JournalState.COMMITTED}
    assert j.path.read_bytes() == good


def test_torn_tail_with_newline_dropped(tmp_path) -> None:
    j = make(tmp_path)
    j.append_pending("b1")
    good = j.path.read_bytes()
    with j.path.open("ab") as fh:
        fh.write(b'not-a-json-line\n')
    assert j.recover_state() == {"b1": JournalState.PENDING}
    assert j.path.read_bytes() == good
