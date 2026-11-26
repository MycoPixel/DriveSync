"""Phase 2.6 — Recovery interface.

Roadmap Step 6 scenarios: accidental deletion, mass-deletion detection,
simultaneous modification (conflicts), interrupted passes and recovery
after restart. The engine tests use the shared `env` fixture (mock Drive);
recovery-module tests point DRIVE_SYNC_HOME at a tmp dir so the trash and
history dirs are isolated.
"""

import json
import os

import pytest

from drivesync import recovery


def wfile(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())


# ---- mass-deletion protection (the safety valve becomes a gate) -------

def _seed_pair(env, n):
    """Sync n files so the base is healthy, then delete them locally."""
    for i in range(n):
        wfile(env.local / f"doc{i}.txt", f"content-{i}-unique")
    env.sync()
    for i in range(n):
        os.remove(env.local / f"doc{i}.txt")


def test_mass_local_deletion_is_held_not_applied(env):
    """Accidental mass deletion: the pass must not propagate it, must keep
    a hold with the paths, and must record one recovery row."""
    env.cfg["delete_safety_limit"] = 3
    _seed_pair(env, 8)
    stats, ok = env.sync()
    assert not ok and stats.deleted == 0
    # files are gone locally, but the remote copies are untouched
    assert env.drive.read_remote("doc0.txt") == b"content-0-unique"
    hold = recovery.pending_deletions(env.store, env.pair["id"])
    assert hold["count"] == 8 and hold["limit"] == 3
    assert set(hold["paths"]) == {f"doc{i}.txt" for i in range(8)}
    assert set(env.base()) == set() or all(
        not env.base()[p].get("deleted_local") for p in env.base())
    rows = env.store.recent_recovery()
    assert [r["action"] for r in rows] == ["safety_stop"]


def test_repeated_trips_update_the_hold_without_new_history_rows(env):
    """A blocked pass runs again (interval worker): the hold refreshes but
    the recovery history records the incident once."""
    env.cfg["delete_safety_limit"] = 3
    _seed_pair(env, 8)
    env.sync()
    env.sync()
    holds = [r for r in env.store.recent_recovery()
             if r["action"] == "safety_stop"]
    assert len(holds) == 1
    assert recovery.pending_deletions(env.store, env.pair["id"])["count"] == 8


def test_approval_applies_the_deletions_for_one_pass(env):
    """Confirm on the Recovery page: the next pass propagates, the approval
    and the hold are consumed, and the action is recorded."""
    env.cfg["delete_safety_limit"] = 3
    _seed_pair(env, 8)
    env.sync()
    recovery.allow_pending_deletions(env.store, env.pair["id"])
    stats, ok = env.sync()
    assert ok and stats.deleted == 8
    assert env.store.kv_get(f"deletes_approved:{env.pair['id']}") is None
    assert recovery.pending_deletions(env.store, env.pair["id"]) is None
    actions = [r["action"] for r in env.store.recent_recovery()]
    assert actions[0] == "deletions_allowed"
    # and the state stays stable afterwards (no resurrection churn)
    stats2, ok2 = env.sync()
    assert ok2 and stats2.deleted == 0


def test_keep_files_switches_deletes_off_and_tombstones(env):
    """'Keep my files': the pair goes deletes-off, the hold is released and
    the next pass acknowledges instead of deleting."""
    env.cfg["delete_safety_limit"] = 3
    _seed_pair(env, 8)
    env.sync()
    recovery.keep_pending_files(env.store, env.pair["id"])
    env.pair = env.store.pair(env.pair["id"])
    assert env.pair["allow_deletes"] == 0
    assert recovery.pending_deletions(env.store, env.pair["id"]) is None
    stats, ok = env.sync()
    assert ok and stats.deleted == 0          # nothing was destroyed
    base = env.base()
    assert all(row["deleted_local"] == 1 for row in base.values())
    # remote copies still exist and the local trash still holds the files
    assert env.drive.read_remote("doc3.txt") == b"content-3-unique"
    actions = [r["action"] for r in env.store.recent_recovery()]
    assert actions[0] == "deletions_kept"


def test_hold_is_released_when_the_risky_pass_resolves(env):
    """Files come back (or the scope changes): the hold clears itself and
    the recovery history records the release."""
    env.cfg["delete_safety_limit"] = 3
    _seed_pair(env, 8)
    env.sync()
    assert recovery.pending_deletions(env.store, env.pair["id"]) is not None
    for i in range(8):                        # the "user restored them" case
        wfile(env.local / f"doc{i}.txt", f"content-{i}-unique")
    stats, ok = env.sync()
    assert ok
    assert recovery.pending_deletions(env.store, env.pair["id"]) is None
    actions = [r["action"] for r in env.store.recent_recovery()]
    assert actions[0] == "pending_cleared"


def test_hold_survives_a_restart(tmp_path):
    """Recovery after application restart: the hold lives in the store, so
    a freshly opened Store sees it (the worker can die any time)."""
    from drivesync.db import Store

    store = Store(tmp_path / "state.db")
    local = tmp_path / "local"
    local.mkdir()
    drive_id = "root-test"
    pair = store.add_pair(1, str(local), drive_id, "My Drive")
    for i in range(6):
        wfile(local / f"f{i}.txt", f"v-{i}")
    base_rows = {f"f{i}.txt": {"kind": "file", "local_md5": "x",
                               "remote_id": f"r{i}", "remote_md5": "x"}
                 for i in range(6)}
    for rel, row in base_rows.items():
        store.base_put(pair["id"], rel, "file",
                       **{k: v for k, v in row.items() if k != "kind"})
    store.kv_set(f"pair_synced:{pair['id']}", "6")
    store.kv_set(f"pending_deletes:{pair['id']}", json.dumps(
        {"count": 6, "limit": 5, "kinds": {"delete_remote": 6},
         "paths": [f"f{i}.txt" for i in range(6)], "more": 0}))
    store.close()

    store2 = Store(tmp_path / "state.db")     # the "restart"
    hold = recovery.pending_deletions(store2, pair["id"])
    assert hold and hold["count"] == 6 and hold["paths"][0] == "f0.txt"
    store2.close()


def test_corrupted_hold_reads_as_none(tmp_path):
    from drivesync.db import Store
    store = Store(tmp_path / "state.db")
    pair = store.add_pair(1, str(tmp_path), "root-x", "My Drive")
    store.kv_set(f"pending_deletes:{pair['id']}", "{not json")
    assert recovery.pending_deletions(store, pair["id"]) is None
    store.close()


# ---- local trash restore ----------------------------------------------

def test_restore_from_trash_round_trip(env, tmp_path, monkeypatch):
    """A remote deletion propagates as a local one; the bytes land in the
    recoverable local trash and restoring puts them back at the path."""
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "apphome"))
    wfile(env.local / "keepme.txt", "precious bytes")
    env.sync()
    fid = env.drive._find_by_rel("keepme.txt")
    env.drive.trash(fid)                      # the file is deleted on Drive
    env.sync()                                # propagates: local deletion
    assert env.store.recent_events(limit=5)[0]["action"] == "delete-local"
    assert not (env.local / "keepme.txt").exists()
    items = recovery.list_local_trash(env.store)
    assert len(items) == 1 and items[0]["rel"] == "keepme.txt"
    stamp = items[0]["stamp"]
    result = recovery.restore_from_trash(env.store, env.pair["id"],
                                         stamp, "keepme.txt")
    assert result["renamed"] is False
    assert (env.local / "keepme.txt").read_bytes() == b"precious bytes"
    assert recovery.list_local_trash(env.store) == []
    actions = [r["action"] for r in env.store.recent_recovery()]
    assert actions[0] == "trash_restored"


def test_restore_from_trash_renames_when_path_is_occupied(
        env, tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "apphome"))
    wfile(env.local / "report.txt", "v1")
    env.sync()
    env.drive.trash(env.drive._find_by_rel("report.txt"))
    env.sync()
    items = recovery.list_local_trash(env.store)
    assert items and items[0]["rel"] == "report.txt"
    wfile(env.local / "report.txt", "a new file took the path")
    result = recovery.restore_from_trash(
        env.store, env.pair["id"], items[0]["stamp"], "report.txt")
    assert result["renamed"] is True
    assert ".restored-" in result["rel"]
    assert (env.local / "report.txt").read_bytes() == b"a new file took the path"
    assert (env.local / result["rel"]).read_bytes() == b"v1"


def test_restore_from_trash_rejects_unknown_items(env, tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "apphome"))
    with pytest.raises(ValueError):
        recovery.restore_from_trash(env.store, env.pair["id"],
                                    "20260101-000000", "ghost.txt")


@pytest.mark.parametrize("stamp", ["..", "../..", "../../etc", "a/b", ""])
def test_restore_from_trash_rejects_traversal_stamps(
        env, tmp_path, monkeypatch, stamp):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "apphome"))
    with pytest.raises(ValueError, match="invalid trash entry"):
        recovery.restore_from_trash(env.store, env.pair["id"], stamp, "x.txt")


def test_restore_from_trash_rejects_traversal_rels(env, tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "apphome"))
    with pytest.raises(ValueError):
        recovery.restore_from_trash(env.store, env.pair["id"],
                                    "20260101-000000", "../../etc/passwd")


def test_list_versions_rejects_traversal(env, tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "apphome"))
    with pytest.raises(ValueError):
        recovery.list_versions(env.pair["id"], "../../etc")


# ---- Drive trash --------------------------------------------------------

def test_drive_trash_round_trip(env):
    """A remote deletion goes to Drive's trash; untrashing hands it back
    and the next pass re-downloads it."""
    env.drive.put_remote_file("cloud.txt", "from drive")
    env.sync()
    fid = env.drive._find_by_rel("cloud.txt")
    env.drive.trash(fid)
    env.sync()                                # remote deletion propagates
    assert not (env.local / "cloud.txt").exists()
    trashed = recovery.list_drive_trash(env.pair, env.drive)
    assert [it["id"] for it in trashed] == [fid]
    result = recovery.restore_drive_trash(env.store, env.pair, env.drive, fid)
    assert result["file_id"] == fid
    stats, ok = env.sync()                    # next pass pulls it again
    assert ok and stats.pulled == 1
    assert (env.local / "cloud.txt").read_bytes() == b"from drive"
    actions = [r["action"] for r in env.store.recent_recovery()]
    assert actions[0] == "drive_trash_restored"


# ---- previous versions --------------------------------------------------

def test_version_history_restores_an_overwritten_file(env, tmp_path,
                                                      monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "apphome"))
    env.cfg["history_versions"] = 10
    env.pair = env.store.pair(env.pair["id"])
    wfile(env.local / "essay.txt", "draft one")
    env.sync()
    fid = env.drive._find_by_rel("essay.txt")
    env.drive._fs_path(fid).write_bytes(b"remote rewrite")
    env.drive._touch(fid)
    env.sync()                                # pull overwrites the local file
    assert (env.local / "essay.txt").read_bytes() == b"remote rewrite"
    versions = recovery.list_versions(env.pair["id"], "essay.txt")
    assert len(versions) == 1                 # "draft one" was kept
    recovery.restore_version(env.store, env.pair["id"], "essay.txt",
                             versions[0]["stamp"])
    assert (env.local / "essay.txt").read_bytes() == b"draft one"
    # the restore is itself undoable: the overwritten bytes were re-kept
    versions = recovery.list_versions(env.pair["id"], "essay.txt")
    assert len(versions) == 2
    actions = [r["action"] for r in env.store.recent_recovery()]
    assert actions[0] == "version_restored"


def test_restore_version_rejects_missing_versions(env, tmp_path,
                                                  monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "apphome"))
    with pytest.raises(ValueError):
        recovery.restore_version(env.store, env.pair["id"], "nope.txt",
                                 "20990101-000000")


# ---- conflict resolution feeds the recovery history ---------------------

def test_conflict_resolution_is_recorded(env):
    from drivesync.conflicts import resolve_conflict
    wfile(env.local / "a.txt", "original")
    env.sync()
    env.set_policy("both")
    wfile(env.local / "a.txt", "local edit longer")
    fid = env.drive._find_by_rel("a.txt")
    env.drive._fs_path(fid).write_bytes(b"remote edit longer")
    env.drive._touch(fid)
    env.sync()                                # conflict copy created
    copy = next(c for c in env.store.base_entries(env.pair["id"])
                if "conflict-" in c["rel_path"])["rel_path"]
    resolve_conflict(env.store, env.pair, env.drive, copy, "keep_original")
    rows = env.store.recent_recovery(action="conflict_resolved")
    assert len(rows) == 1 and rows[0]["rel_path"] == copy


# ---- recovery history bookkeeping ---------------------------------------

def test_recent_recovery_filters_by_pair_and_action(env):
    env.store.recovery_log(env.pair["id"], "a.txt", "trash_restored",
                           "reason", "detail")
    env.store.recovery_log(None, "", "safety_stop", "r2", "d2")
    assert len(env.store.recent_recovery(action="safety_stop")) == 1
    assert len(env.store.recent_recovery(pair_id=env.pair["id"])) == 1
    assert len(env.store.recent_recovery()) == 2


def test_delete_pair_cleans_recovery_state(env):
    pid = env.pair["id"]
    env.store.kv_set(f"pending_deletes:{pid}", json.dumps({"count": 1}))
    env.store.recovery_log(pid, "x", "safety_stop", "r", "d")
    env.store.delete_pair(pid)
    assert env.store.recent_recovery() == []
    assert env.store.kv_get(f"pending_deletes:{pid}") is None


# ---- dashboard Recovery endpoints --------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    from drivesync.db import Store
    from drivesync.server import create_app

    store = Store(tmp_path / "state.db")
    app = create_app(store, worker=None)
    c = app.test_client()
    c.environ_base["HTTP_X_DRIVESYNC"] = app.config["API_TOKEN"]
    yield c, store
    store.close()


def test_recovery_summary_is_empty_but_ok(client):
    c, _ = client
    body = c.get("/api/recovery/summary").get_json()
    assert body["ok"] is True
    assert body["pending"] == [] and body["trash"] == []
    assert body["conflicts"] == [] and body["failures"] == []
    assert body["history"] == []


def test_recovery_summary_reports_a_hold(client):
    c, store = client
    pair = store.add_pair(1, str(client.local if False else "/tmp"),
                          "root-zz", "My Drive")
    store.kv_set(f"pending_deletes:{pair['id']}", json.dumps(
        {"count": 4, "limit": 3, "kinds": {"delete_local": 4},
         "paths": ["a.txt", "b.txt"], "more": 0}))
    store.recovery_log(pair["id"], "a.txt", "safety_stop", "limit", "held")
    body = c.get("/api/recovery/summary").get_json()
    assert body["pending"][0]["count"] == 4
    assert body["pending"][0]["pair_id"] == pair["id"]
    assert body["history"][0]["action"] == "safety_stop"


def test_allow_and_keep_endpoints_validate_the_hold(client):
    c, store = client
    pair = store.add_pair(1, "/tmp/nothing", "root-q", "My Drive")
    resp = c.post("/api/recovery/deletions/allow",
                  json={"pair_id": pair["id"]})
    assert resp.status_code == 400            # nothing held yet
    store.kv_set(f"pending_deletes:{pair['id']}", json.dumps(
        {"count": 2, "limit": 1, "kinds": {}, "paths": ["x"], "more": 0}))
    resp = c.post("/api/recovery/deletions/keep",
                  json={"pair_id": pair["id"]})
    assert resp.status_code == 200
    assert store.pair(pair["id"])["allow_deletes"] == 0
    assert recovery.pending_deletions(store, pair["id"]) is None
    resp = c.get("/api/pairs")
    assert resp.status_code == 200


def test_allow_endpoint_requires_an_existing_pair(client):
    c, _ = client
    resp = c.post("/api/recovery/deletions/allow", json={"pair_id": 99})
    assert resp.status_code == 404


def test_trash_restore_endpoint_rejects_unknown_items(client, tmp_path):
    c, _ = client
    resp = c.post("/api/recovery/trash/restore",
                  json={"pair_id": 1, "stamp": "20260101-000000",
                        "rel": "ghost.txt"})
    assert resp.status_code == 400


def test_drive_trash_endpoints_map_errors_to_502(client, monkeypatch):
    """A broken Drive token must be a clean 502, not a 500 page."""
    c, store = client
    store.add_account("me@example.com", {})
    store.add_pair(1, "/tmp/nowhere", "root-k", "My Drive")

    class BoomDrive:
        def __init__(self, provider, limiter=None):
            pass

        def list_trashed(self, limit=100):
            from drivesync.drive import DriveError
            raise DriveError("GET files -> 403: PERMISSION_DENIED")

        def untrash(self, file_id):
            from drivesync.drive import DriveError
            raise DriveError(f"PATCH files/{file_id} -> 403: PERMISSION_DENIED")

    monkeypatch.setattr("drivesync.server.Drive", BoomDrive)
    resp = c.get("/api/recovery/drive-trash?pair_id=1")
    assert resp.status_code == 502
    assert "PERMISSION_DENIED" in resp.get_json()["error"]
    resp = c.post("/api/recovery/drive-trash/restore",
                  json={"pair_id": 1, "file_id": "x"})
    assert resp.status_code == 502


def test_recovery_history_endpoint(client):
    c, store = client
    store.recovery_log(None, "", "deletions_allowed", "user", "3 applied")
    body = c.get("/api/recovery/history").get_json()
    assert body["ok"] and len(body["history"]) == 1
    assert body["history"][0]["action"] == "deletions_allowed"
