"""DriveSync's own data directory (tokens, client secret) must never sync."""

import pytest

from drivesync import engine
from drivesync.db import Store
from drivesync.security import data_dir_ignore_patterns, is_data_dir


def _remote_has(drive, rel):
    try:
        drive._find_by_rel(rel)
        return True
    except KeyError:
        return False


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake $HOME whose DriveSync data dir lives inside it, as in real use."""
    data = tmp_path / "home" / ".local" / "share" / "drivesync"
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(data))
    return tmp_path / "home"


def test_is_data_dir(home):
    data = home / ".local" / "share" / "drivesync"
    data.mkdir(parents=True)
    assert is_data_dir(data)
    assert not is_data_dir(home)
    assert not is_data_dir(data / "trash")


def test_add_pair_refuses_the_data_dir(home, tmp_path):
    data = home / ".local" / "share" / "drivesync"
    data.mkdir(parents=True)
    store = Store(tmp_path / "state.db")
    try:
        store.add_account("a@x.com", {})
        with pytest.raises(ValueError, match="own data directory"):
            store.add_pair(1, str(data), "root")
        ok = store.add_pair(1, str(home / "Documents"), "root")
        with pytest.raises(ValueError, match="own data directory"):
            store.update_pair(ok["id"], local_path=str(data))
    finally:
        store.close()


def test_patterns_only_when_data_dir_is_below_root(home):
    assert data_dir_ignore_patterns(home) == [
        ".local/share/drivesync", ".local/share/drivesync/*"]
    assert data_dir_ignore_patterns(home / "Documents") == []
    assert data_dir_ignore_patterns(home / ".local" / "share" / "drivesync"
                                    / "trash") == []


def test_ancestor_root_never_uploads_tokens(env, monkeypatch, tmp_path):
    """A pair rooted at $HOME must not push state.db or config.json."""
    data = env.local / ".local" / "share" / "drivesync"
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(data))
    _write(data / "state.db", "refresh-tokens")
    _write(data / "config.json", "client-secret")
    _write(env.local / "notes.txt", "keep me")

    stats, ok = env.sync()

    # (empty parent folders such as .local/ may be mirrored; no file inside
    # the data dir may be)
    assert ok
    assert env.drive.read_remote("notes.txt") == b"keep me"
    assert not _remote_has(env.drive, ".local/share/drivesync/state.db")
    assert not _remote_has(env.drive, ".local/share/drivesync/config.json")


def test_stale_remote_copy_is_not_pulled_over_live_data(env, monkeypatch):
    """A state.db an older version uploaded must not overwrite the real one."""
    data = env.local / ".local" / "share" / "drivesync"
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(data))
    _write(data / "state.db", "live-database")
    env.drive.put_remote_file(".local/share/drivesync/state.db", "stale-copy")

    stats, ok = env.sync()

    assert ok
    assert (data / "state.db").read_text() == "live-database"


def test_existing_pair_on_the_data_dir_is_not_scanned(env, monkeypatch):
    """Pairs created before the guard existed fail closed at scan time."""
    data = env.local / "dsdata"
    data.mkdir()
    _write(data / "state.db", "refresh-tokens")
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(data))
    # bypass add_pair/update_pair, as a row from an older version would
    env.store._exec("UPDATE pairs SET local_path=? WHERE id=?",
                    (str(data), env.pair["id"]))
    pair = env.store.pair(env.pair["id"])

    result = engine.scan_pair_state(env.store, env.cfg, pair, env.drive)

    assert result.error and "own data directory" in result.error
    assert not _remote_has(env.drive, "state.db")
