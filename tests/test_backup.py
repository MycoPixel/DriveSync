"""state.db auto-backup and corruption recovery (S6)."""

import sqlite3
import time

import pytest

from drivesync.db import Store


def _seed(db_path):
    store = Store(db_path)
    store.add_account("a@example.com", {"access_token": "t",
                                        "refresh_token": "r",
                                        "expires_at": time.time() + 3600})
    return store


def test_backup_writes_a_snapshot(tmp_path):
    store = _seed(tmp_path / "state.db")
    out = store.backup()
    assert out == str(tmp_path / "state.db.bak")
    assert (tmp_path / "state.db.bak").stat().st_size > 0
    store.close()


def test_backup_skipped_for_memory_db():
    store = Store(":memory:")
    assert store.backup() == ""
    store.close()


def test_corrupt_db_is_restored_from_backup(tmp_path):
    db = tmp_path / "state.db"
    store = _seed(db)
    store.backup()
    store.close()
    db.write_bytes(b"not a database at all" * 100)
    reopened = Store(db)
    assert reopened.recovered is True
    assert [a["email"] for a in reopened.accounts()] == ["a@example.com"]
    reopened.close()


def test_missing_db_falls_back_to_backup(tmp_path):
    db = tmp_path / "state.db"
    store = _seed(db)
    store.backup()
    store.close()
    db.unlink()
    reopened = Store(db)
    assert reopened.recovered is True
    assert reopened.accounts()
    reopened.close()


def test_corrupt_db_without_backup_raises(tmp_path):
    db = tmp_path / "state.db"
    store = _seed(db)
    store.close()
    db.write_bytes(b"garbage" * 500)
    with pytest.raises(sqlite3.DatabaseError):
        Store(db)


def test_worker_backs_up_after_clean_pass_rate_limited(tmp_path):
    from drivesync.worker import Worker
    store = _seed(tmp_path / "state.db")
    worker = Worker(store, {"sync_interval": 30})
    # inside the 10-minute window: no new backup
    store.kv_set("last_db_backup", str(time.time()))
    worker._maybe_backup_db()
    assert not (tmp_path / "state.db.bak").exists()
    # outside the window: the backup happens
    store.kv_set("last_db_backup", str(time.time() - 601))
    worker._maybe_backup_db()
    assert (tmp_path / "state.db.bak").exists()
    store.close()


def test_repair_note_logged_by_serve(monkeypatch, tmp_path, capsys):
    """cmd_serve reports a recovery to the activity log and stdout."""
    db = tmp_path / "home" / "state.db"
    db.parent.mkdir(parents=True)
    store = _seed(db)
    store.backup()
    store.close()
    db.write_bytes(b"broken" * 99)
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))

    from drivesync.cli import _open_store
    reopened = _open_store()
    assert reopened.recovered is True
    reopened.log(None, "probe", "", "")
    assert any(e["action"] == "probe" for e in reopened.recent_events())
    reopened.close()
