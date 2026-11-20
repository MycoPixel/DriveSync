"""Conflict-copy review and resolution."""

import os

import pytest

from drivesync.conflicts import list_conflicts, parse_conflict_name, \
    resolve_conflict


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _make_conflict(env):
    """Sync a file, edit both sides, and let the 'both' policy keep a copy."""
    _write(env.local / "a.txt", b"original")
    env.sync()
    _write(env.local / "a.txt", b"local edit")
    st = os.stat(env.local / "a.txt")
    os.utime(env.local / "a.txt", (st.st_atime, 1_000_000_000))
    fid = env.drive._find_by_rel("a.txt")
    env.drive._fs_path(fid).write_bytes(b"remote edit")
    env.drive._touch(fid)
    env.set_policy("both")
    stats, ok = env.sync()
    assert ok and stats.conflicts == 1
    copies = list(env.local.glob("a.conflict-*.txt"))
    assert len(copies) == 1
    return copies[0]


def test_parse_conflict_name_forms():
    name = "a.conflict-20240102-030405.txt"
    assert parse_conflict_name(name) == (name, "a.txt")
    nested = "docs/README.conflict-20240102-030405"
    assert parse_conflict_name(nested) == (nested, "docs/README")
    hidden = ".conflict-20240102-030405.notes.txt"
    assert parse_conflict_name(hidden) == (hidden, ".notes.txt")
    archive = "backup.conflict-20240102-030405.tar.gz"
    assert parse_conflict_name(archive) == (archive, "backup.tar.gz")
    assert parse_conflict_name("plain.txt") is None
    assert parse_conflict_name("a.conflict-2024.txt") is None
    assert parse_conflict_name("") is None


def test_list_conflicts_maps_copy_to_original(env):
    copy = _make_conflict(env)
    items = list_conflicts(env.store, env.pair)
    assert len(items) == 1
    item = items[0]
    assert item["path"] == copy.name
    assert item["original"] == "a.txt"
    assert item["local_exists"] and item["original_local_exists"]
    assert item["remote_id"]
    assert item["size"] == len(b"local edit")


def test_keep_both_changes_nothing(env):
    copy = _make_conflict(env)
    resolve_conflict(env.store, env.pair, env.drive, copy.name, "keep_both")
    assert copy.exists()
    assert env.drive.read_remote(copy.name) == b"local edit"
    assert env.store.base_get(env.pair["id"], copy.name)


def test_keep_original_discards_the_copy_everywhere(env):
    copy = _make_conflict(env)
    resolve_conflict(env.store, env.pair, env.drive, copy.name,
                     "keep_original")
    assert not copy.exists()
    with pytest.raises(KeyError):
        env.drive.read_remote(copy.name)
    assert env.store.base_get(env.pair["id"], copy.name) is None
    assert (env.local / "a.txt").read_bytes() == b"remote edit"
    assert env.drive.read_remote("a.txt") == b"remote edit"
    stats, ok = env.sync()
    assert ok and stats.conflicts == 0


def test_keep_copy_wins_on_both_sides_and_stays_resolved(env):
    copy = _make_conflict(env)
    resolve_conflict(env.store, env.pair, env.drive, copy.name, "keep_copy")
    assert not copy.exists()
    assert (env.local / "a.txt").read_bytes() == b"local edit"
    assert env.drive.read_remote("a.txt") == b"local edit"
    assert env.store.base_get(env.pair["id"], copy.name) is None
    # the base snapshot was refreshed, so the next pass is a no-op
    stats, ok = env.sync()
    assert ok and stats.conflicts == 0 and stats.pushed == 0 \
        and stats.pulled == 0


def test_keep_copy_recreates_a_remote_deleted_original(env):
    # remote deleted + local edited under the 'both' policy: the original is
    # gone on Drive, so keeping the copy has to upload it again by name
    _write(env.local / "a.txt", b"v1")
    env.sync()
    _write(env.local / "a.txt", b"local edit")
    fid = env.drive._find_by_rel("a.txt")
    env.drive.trash(fid)
    env.set_policy("both")
    stats, ok = env.sync()
    assert ok and stats.conflicts == 1
    copy = next(env.local.glob("a.conflict-*.txt"))
    resolve_conflict(env.store, env.pair, env.drive, copy.name, "keep_copy")
    assert (env.local / "a.txt").read_bytes() == b"local edit"
    assert env.drive.read_remote("a.txt") == b"local edit"
    assert not copy.exists()


def test_unknown_copy_is_rejected(env):
    with pytest.raises(ValueError):
        resolve_conflict(env.store, env.pair, env.drive, "a.txt", "keep_copy")
    with pytest.raises(ValueError):
        resolve_conflict(env.store, env.pair, env.drive,
                         "a.conflict-20240102-030405.txt", "keep_both")
    with pytest.raises(ValueError):
        resolve_conflict(env.store, env.pair, env.drive, "x.txt", "bogus")
