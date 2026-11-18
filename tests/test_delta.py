"""Incremental sync via the Drive changes feed (delta scans).

Covers: the delta path producing the same result as a full scan for the
common file-level changes, the deliberate fallback to a full scan for
structural changes (dir rename), and the worker's token lifecycle
(subsequent passes must not re-list the whole tree).
"""

import hashlib
import os
import time

from drivesync import engine
from drivesync.config import DEFAULT_CONFIG
from drivesync.db import Store
from drivesync.worker import Worker
from mock_drive import MockDrive


def test_kv_roundtrip(tmp_path):
    s = Store(tmp_path / "s.db")
    assert s.kv_get("k") is None
    s.kv_set("k", "v1")
    assert s.kv_get("k") == "v1"
    s.kv_set("k", "v2")                     # upsert
    assert s.kv_get("k") == "v2"
    s.kv_delete("k")
    assert s.kv_get("k") is None
    s.close()


def test_base_rows_carry_remote_metadata(env):
    (env.local / "a.txt").write_text("alpha")
    env.sync()
    row = dict(env.store.base_get(env.pair["id"], "a.txt"))
    assert row["remote_mime"]
    assert row["remote_size"] == 5
    assert row["remote_mtime"] > 0


def test_delta_matches_full_scan(env):
    (env.local / "a.txt").write_text("alpha")
    (env.local / "b.txt").write_text("beta")
    os.makedirs(env.local / "sub")
    (env.local / "sub" / "c.txt").write_text("gamma")
    token = env.drive.changes_start_token()   # worker captures it pre-scan
    env.sync()

    # remote activity while the client was away:
    # (1) edit a.txt, (2) new file sub/d.txt, (3) delete b.txt
    fid_a = env.drive._find_by_rel("a.txt")
    data = b"alpha-v2"
    env.drive._fs_path(fid_a).write_bytes(data)
    env.drive.files[fid_a]["md5Checksum"] = hashlib.md5(data).hexdigest()
    env.drive.files[fid_a]["size"] = str(len(data))
    env.drive._touch(fid_a)
    env.drive.put_remote_file("sub/d.txt", "delta")
    env.drive.trash(env.drive._find_by_rel("b.txt"))

    items, _new = env.drive.changes_list(token)
    # the feed also contains the first pass's own uploads/mkdir — decide()
    # treats them as no-ops because the base already matches
    stats, ok = engine.sync_pair(env.store, env.cfg, env.pair, env.drive,
                                 changes=items)
    assert ok
    assert (env.local / "a.txt").read_text() == "alpha-v2"      # pulled
    assert (env.local / "sub" / "d.txt").read_text() == "delta"  # pulled
    assert not (env.local / "b.txt").exists()                    # deleted
    assert stats.pulled == 2 and stats.deleted == 1

    # a following FULL scan must find nothing left to do: the delta pass
    # left the pair in exactly the state a full scan would have produced
    stats2, ok2 = env.sync()
    assert ok2
    assert (stats2.pushed, stats2.pulled, stats2.deleted,
            stats2.renamed, stats2.conflicts) == (0, 0, 0, 0, 0)


def test_delta_pushes_local_edit(env):
    (env.local / "a.txt").write_text("v1")
    token = env.drive.changes_start_token()
    env.sync()
    (env.local / "a.txt").write_text("v2")   # local edit while idle
    st = os.stat(env.local / "a.txt")
    os.utime(env.local / "a.txt", (st.st_atime + 10, st.st_mtime + 10))
    items, _ = env.drive.changes_list(token)  # only the pass-1 upload: remote unchanged
    stats, ok = engine.sync_pair(env.store, env.cfg, env.pair, env.drive,
                                 changes=items)
    assert ok and stats.pushed == 1
    assert env.drive.read_remote("a.txt") == b"v2"


def test_dir_rename_falls_back_to_full(env):
    os.makedirs(env.local / "d1")
    (env.local / "d1" / "keep.txt").write_text("precious")
    token = env.drive.changes_start_token()
    env.sync()
    fid = next(f["id"] for f in env.drive.files.values()
               if f["name"] == "d1")
    env.drive.rename(fid, "d2")               # structural: dir renamed remotely
    items, _ = env.drive.changes_list(token)
    stats, ok = engine.sync_pair(env.store, env.cfg, env.pair, env.drive,
                                 changes=items)
    assert ok
    assert (env.local / "d2" / "keep.txt").read_text() == "precious"
    assert not (env.local / "d1").exists()


def test_worker_subsequent_passes_skip_full_relist(tmp_path):
    store = Store(tmp_path / "s.db")
    drive = MockDrive(tmp_path / "cloud")
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.txt").write_text("hello")
    store.add_pair(1, str(local), drive.root_id, "My Drive")
    store.add_account("t@example.com", {
        "access_token": "x", "refresh_token": "r",
        "client_id": "c", "client_secret": "s",
        "expires_at": time.time() + 9999,
    })
    cfg = dict(DEFAULT_CONFIG)
    cfg["ignore_patterns"] = []

    w = Worker(store, cfg)
    w.make_drive = lambda account: drive      # the worker builds its own
    w.run_pass()                              # first pass: full scan + token
    assert store.kv_get("changes_token:1")
    assert drive.calls["list_tree"] == 1
    assert drive.calls["changes_list"] == 0

    w.run_pass()                              # idle pass: delta only
    assert drive.calls["list_tree"] == 1      # NO second tree listing
    assert drive.calls["changes_list"] == 1

    # a remote edit is picked up by the delta pass
    fid = drive._find_by_rel("a.txt")
    data = b"changed"
    drive._fs_path(fid).write_bytes(data)
    drive.files[fid]["md5Checksum"] = hashlib.md5(data).hexdigest()
    drive.files[fid]["size"] = str(len(data))
    drive._touch(fid)
    w.run_pass()
    assert (local / "a.txt").read_text() == "changed"
    assert drive.calls["list_tree"] == 1      # still no full relist
    store.close()


def test_worker_single_pair_pass_keeps_token(tmp_path):
    """A filtered pass must not advance the feed token: other pairs of the
    same account were not swept, so their deltas must be re-read next time."""
    store = Store(tmp_path / "s.db")
    drive = MockDrive(tmp_path / "cloud")
    local = tmp_path / "local"
    local.mkdir()
    (local / "a.txt").write_text("hello")
    p1 = store.add_pair(1, str(local), drive.root_id, "My Drive")
    store.add_account("t@example.com", {
        "access_token": "x", "refresh_token": "r",
        "client_id": "c", "client_secret": "s",
        "expires_at": time.time() + 9999,
    })
    cfg = dict(DEFAULT_CONFIG)
    cfg["ignore_patterns"] = []

    w = Worker(store, cfg)
    w.make_drive = lambda account: drive      # the worker builds its own
    w.run_pass()
    tok = store.kv_get("changes_token:1")
    (local / "a.txt").write_text("edited")
    w.run_pass(p1["id"])                      # filtered pass
    assert store.kv_get("changes_token:1") == tok   # token NOT advanced
    store.close()
