"""evalFix: ``yuki.eval.dbcopy`` - the real database is only read (``?mode=ro``, the sqlite backup API), copied with
its key into a temp folder, never opened as a store, never written; copies are refused in its folder and deleted
afterwards. "The real database" is a stand-in in the test's own folder.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from test_evalFix_world import digest, hermetic, live  # noqa: F401
from yuki.eval import dbcopy
from yuki.memory.store import KEY_FILENAME, Store


def test_copy_reads_the_live_database_only(live):
    before = digest(live.parent)
    path = dbcopy.copy_db()
    try:
        assert path.parent != live.parent and Path(tempfile.gettempdir()) in path.parents
        assert (path.parent / KEY_FILENAME).read_bytes() == (live.parent / KEY_FILENAME).read_bytes()
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] > 0
        conn.close()
    finally:
        assert dbcopy.remove_copy(path)
    assert not path.parent.exists()
    assert digest(live.parent) == before


def test_the_live_database_is_never_opened_as_a_store(live, monkeypatch):
    opened = []
    real_open = Store.open.__func__

    def spy(cls, p=None):
        opened.append(Path(p).resolve() if p else None)
        return real_open(cls, p)

    monkeypatch.setattr(Store, "open", classmethod(spy))
    with pytest.raises(RuntimeError, match="never the live one"):
        dbcopy.open_copy(live)
    with dbcopy.working_copy() as (store, path):
        assert store.path == path
    assert opened and live.resolve() not in opened
    assert not path.parent.exists()


def test_committed_rows_still_in_the_write_ahead_log_are_copied(live):
    w = sqlite3.connect(live)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("PRAGMA wal_autocheckpoint=0")
    w.execute("INSERT INTO source_checkpoints(name, value, updated_at) VALUES ('evalfix.wal', '1', 0)")
    w.commit()                                     # committed, not checkpointed: only in the -wal file
    try:
        path = dbcopy.copy_db(live)
        try:
            c = sqlite3.connect(path)
            assert c.execute("SELECT value FROM source_checkpoints WHERE name='evalfix.wal'").fetchone() == ("1",)
            c.close()
        finally:
            dbcopy.remove_copy(path)
    finally:
        w.close()


def test_a_copy_never_goes_into_the_live_folder(live):
    with pytest.raises(RuntimeError):
        dbcopy.copy_db(live, live.parent)
    with pytest.raises(RuntimeError):
        dbcopy.remove_copy(live)
    assert live.exists()


def test_a_copy_never_goes_above_or_below_the_live_folder(live, hermetic):
    for dest in (hermetic, live.parent, live.parent / "spool"):
        with pytest.raises(RuntimeError):
            dbcopy.copy_db(live, dest)
    assert not (hermetic / dbcopy.COPY_MARKER).exists() and not (live.parent / "spool").exists()


def test_a_copy_never_overwrites_a_database(live, tmp_path):
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    (theirs / "memory.db").write_bytes(b"someone's database")
    with pytest.raises(RuntimeError, match="overwrites"):
        dbcopy.copy_db(live, theirs)
    assert (theirs / "memory.db").read_bytes() == b"someone's database"


def test_removing_a_copy_in_a_named_folder_deletes_only_the_copy(live, tmp_path):
    notes = tmp_path / "my-notes"
    notes.mkdir()
    (notes / "important.txt").write_text("keep me", encoding="utf-8")
    path = dbcopy.copy_db(live, notes)
    sqlite3.connect(path).close()
    assert dbcopy.remove_copy(path)
    assert (notes / "important.txt").read_text(encoding="utf-8") == "keep me"
    assert sorted(p.name for p in notes.iterdir()) == ["important.txt"]
    empty = dbcopy.copy_db(live, tmp_path / "empty-one")
    assert dbcopy.remove_copy(empty) and not empty.parent.exists()       # a folder left empty goes too


def test_removing_never_touches_the_live_folder_or_a_folder_holding_it(live, hermetic):
    before = digest(live.parent)
    for target in (hermetic, live.parent, live.parent.parent / "memory.db", live.parent / "spool" / "memory.db"):
        with pytest.raises(RuntimeError):
            dbcopy.remove_copy(target)
    assert digest(live.parent) == before and live.exists()


def test_removing_refuses_a_folder_copy_db_did_not_make(tmp_path, hermetic):
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "memory.db").write_bytes(b"not a copy")
    with pytest.raises(RuntimeError, match="no marker"):
        dbcopy.remove_copy(mine / "memory.db")
    assert (mine / "memory.db").exists()
    assert dbcopy.remove_copy(tmp_path / "gone" / "memory.db")           # nothing there: already gone


def test_a_copy_of_your_own_copy_is_copied_again(live, tmp_path):
    mine = dbcopy.copy_db(live, tmp_path / "mine")
    before = digest(mine.parent)
    again = dbcopy.copy_db(mine)
    try:
        assert again.parent != mine.parent
        with pytest.raises(RuntimeError):
            dbcopy.copy_db(mine, mine.parent)
    finally:
        dbcopy.remove_copy(again)
    assert digest(mine.parent) == before


def test_a_copy_without_its_key_is_refused(live, tmp_path):
    path = dbcopy.copy_db(live, tmp_path / "nokey")
    (path.parent / KEY_FILENAME).unlink()
    with pytest.raises(RuntimeError, match="no key"):
        dbcopy.open_copy(path)


def test_eval_files_live_outside_the_repo_and_the_memory_folder(hermetic, monkeypatch):
    assert dbcopy.eval_dir() == hermetic / "eval"
    monkeypatch.delenv("YUKI_EVAL_DIR")
    assert dbcopy.eval_dir() == hermetic / "local" / "Yuki" / "eval"
    assert dbcopy.report_dir() == Path(tempfile.gettempdir())


def test_a_missing_database_is_said_plainly(hermetic):
    with pytest.raises(FileNotFoundError):
        dbcopy.copy_db(hermetic / "nothing" / "memory.db")
