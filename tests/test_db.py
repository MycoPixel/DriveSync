"""Store (SQLite) tests."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from drivesync.db import Store, to_local


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


def test_account_upsert_by_email(store):
    a1 = store.add_account("me@example.com", {"refresh_token": "r1"})
    a2 = store.add_account("me@example.com", {"refresh_token": "r2"})
    rows = store.accounts()
    assert len(rows) == 1
    assert rows[0]["id"] == a1["id"] == a2["id"]
    import json
    assert json.loads(rows[0]["token_json"])["refresh_token"] == "r2"


def test_reauth_on_fresh_connection(tmp_path):
    """Re-auth crash: the first DML on a fresh connection is an upsert that
    takes the DO UPDATE path, where cursor.lastrowid is not set (0/None)."""
    import json
    db = tmp_path / "state.db"
    s1 = Store(db)
    first = s1.add_account("me@example.com", {"refresh_token": "r1"})
    s1.close()

    s2 = Store(db)  # fresh connection: no INSERT has ever run on it
    row = s2.add_account("me@example.com", {"refresh_token": "r2"})
    assert row is not None
    assert row["id"] == first["id"]
    assert json.loads(row["token_json"])["refresh_token"] == "r2"
    assert len(s2.accounts()) == 1
    s2.close()


def test_pair_crud_and_validation(store):
    store.add_account("a@x.com", {})
    pair = store.add_pair(1, "/tmp/x", "root123", remote_path="My Drive/x")
    assert pair["direction"] == "bidirectional" and pair["enabled"] == 1
    with pytest.raises(ValueError):
        store.add_pair(1, "/tmp/x", "root123", direction="sideways")
    with pytest.raises(ValueError):
        store.update_pair(pair["id"], conflict_policy="bogus")
    updated = store.update_pair(pair["id"], direction="upload", enabled=0)
    assert updated["direction"] == "upload" and updated["enabled"] == 0
    store.delete_pair(pair["id"])
    assert store.pair(pair["id"]) is None


def test_state_db_permissions_are_private(store):
    """The DB stores OAuth refresh tokens."""
    assert store.path
    import os
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    assert os.stat(os.path.dirname(store.path)).st_mode & 0o777 == 0o700


class _CountingConn:
    """Proxy that records commits (sqlite3.Connection is read-only)."""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "commits", 0)

    def commit(self):
        object.__setattr__(self, "commits", self.commits + 1)
        self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_wal_mode_and_busy_timeout(store):
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_batch_groups_writes_into_one_commit(store):
    proxy = _CountingConn(store._conn)
    store._conn = proxy
    with store.batch():
        store.kv_set("k1", "1")
        store.kv_set("k2", "2")
        assert proxy.commits == 0          # nothing committed yet
    assert proxy.commits == 1
    assert store.kv_get("k1") == "1"       # note: reads also commit today

    # nested batches commit only at the outermost exit
    base = proxy.commits
    with store.batch():
        store.kv_set("k3", "3")
        with store.batch():
            store.kv_set("k4", "4")
        assert proxy.commits == base
    assert proxy.commits == base + 1
    assert store.kv_get("k3") == "3" and store.kv_get("k4") == "4"


def test_add_pair_rejects_overlapping_local_roots(store):
    store.add_account("a@x.com", {})
    store.add_pair(1, "/tmp/tree", "root")
    with pytest.raises(ValueError) as exc:
        store.add_pair(1, "/tmp/tree/sub", "root2")
    assert "overlaps pair" in str(exc.value)
    with pytest.raises(ValueError):
        store.add_pair(1, "/tmp/tree", "root3")
    # a sibling tree is fine
    store.add_pair(1, "/tmp/tree2", "root4")
    assert len(store.pairs()) == 2


def test_repointing_pair_drops_stale_base_and_markers(store):
    store.add_account("a@x.com", {})
    pair = store.add_pair(1, "/tmp/x", "root123")
    store.base_put(pair["id"], "a.txt", "file", remote_id="f1")
    store.kv_set(f"pair_synced:{pair['id']}", "1")
    store.kv_set(f"stale:{pair['id']}", "1")

    store.update_pair(pair["id"], local_path="/tmp/y")   # different tree
    assert store.base_entries(pair["id"]) == []
    assert store.kv_get(f"pair_synced:{pair['id']}") is None
    assert store.kv_get(f"stale:{pair['id']}") is None

    store.base_put(pair["id"], "b.txt", "file", remote_id="f2")
    store.update_pair(pair["id"], enabled=0)             # unrelated edit
    assert len(store.base_entries(pair["id"])) == 1      # base preserved


def test_entries_roundtrip(store):
    store.add_account("a@x.com", {})
    pair = store.add_pair(1, "/tmp/x", "root123")
    store.base_put(pair["id"], "a.txt", "file", remote_id="f1",
                   local_md5="m", local_size=3, local_mtime=1.0,
                   remote_md5="rm", remote_version="1")
    row = store.base_get(pair["id"], "a.txt")
    assert row["remote_id"] == "f1" and row["local_md5"] == "m"
    store.base_put(pair["id"], "a.txt", "file", remote_id="f1",
                   local_md5="m2", local_size=4, local_mtime=2.0,
                   remote_md5="rm2", remote_version="2")
    assert store.base_get(pair["id"], "a.txt")["local_md5"] == "m2"
    store.base_rename(pair["id"], "a.txt", "b.txt")
    assert store.base_get(pair["id"], "b.txt")["remote_id"] == "f1"
    assert store.base_get(pair["id"], "a.txt") is None
    store.base_delete(pair["id"], "b.txt")
    assert store.base_get(pair["id"], "b.txt") is None


def test_deleting_account_cascades(store):
    store.add_account("a@x.com", {})
    pair = store.add_pair(1, "/tmp/x", "root123")
    store.base_put(pair["id"], "f.txt", "file", remote_id="r1")
    store.delete_account(1)
    assert store.pairs() == []
    assert store.base_entries(pair["id"]) == []


def test_events_log_and_filter(store):
    store.add_account("a@x.com", {})
    pair = store.add_pair(1, "/tmp/x", "root123")
    store.log(pair["id"], "upload", "a.txt")
    store.log(pair["id"], "conflict", "b.txt", "both changed", level="conflict")
    all_rows = store.recent_events(limit=10)
    assert len(all_rows) == 2
    conflicts = store.recent_events(limit=10, level="conflict")
    assert len(conflicts) == 1 and conflicts[0]["rel_path"] == "b.txt"
    by_pair = store.recent_events(limit=10, pair_id=pair["id"])
    assert len(by_pair) == 2


def test_events_action_time_and_clear(store):
    store.add_account("a@x.com", {})
    pair = store.add_pair(1, "/tmp/x", "root123")
    store.log(pair["id"], "upload", "a.txt")
    store.log(pair["id"], "download", "b.txt")
    store.log(pair["id"], "upload", "c.txt", level="error")
    # newest first within a filtered set
    uploads = store.recent_events(limit=10, action="upload")
    assert [r["rel_path"] for r in uploads] == ["c.txt", "a.txt"]
    errs = store.recent_events(limit=10, action="upload", level="error")
    assert [r["rel_path"] for r in errs] == ["c.txt"]
    # time frame: 'since' as UTC datetime — recent rows pass, future cut-off
    recent = store.recent_events(
        limit=10, since=datetime.now(timezone.utc) - timedelta(hours=1))
    assert len(recent) == 3
    future = store.recent_events(
        limit=10, since=datetime.now(timezone.utc) + timedelta(hours=1))
    assert future == []
    # filter dropdown data + wipe
    assert store.event_actions() == ["download", "upload"]
    assert store.count_events() == 3
    assert store.clear_events() == 3
    assert store.count_events() == 0
    assert store.event_actions() == []


def test_activity_rows_are_strings(store):
    """The GUI TreeView model declares str-only columns; a NULL or numeric
    value from the events table would make the ListStore append raise
    TypeError and 'drivesync gui' would crash at startup."""
    store.add_account("a@x.com", {})
    pair = store.add_pair(1, "/tmp/x", "root123")
    store.log(pair["id"], "upload", "a.txt", "copied")
    store.log(pair["id"], "conflict", "b.txt", "both changed",
              level="conflict")
    store.log(pair["id"], "test", None, None)      # NULLs must not leak
    for e in store.recent_events(limit=10):
        for k in ("ts", "level", "action", "rel_path", "message"):
            assert isinstance(e[k], str), (k, e[k])


def test_optimize_and_close_keep_database_usable(tmp_path):
    s = Store(tmp_path / "state.db")
    s.log(None, "test", "", "row")
    s.optimize()
    assert s.count_events() >= 1
    s.close()
    # reopening after a checkpointed close must still work
    s2 = Store(tmp_path / "state.db")
    assert s2.count_events() >= 1
    s2.close()


def test_prune_events_drops_old_then_caps(store):
    """Old rows go by age first; the table is then capped at `keep` newest."""
    old = (datetime.now(timezone.utc) - timedelta(days=40)) \
        .isoformat(timespec="seconds")
    for _ in range(3):
        store._exec("INSERT INTO events(ts, level, action) VALUES (?,?,?)",
                    (old, "info", "old"))
    for _ in range(5):
        store.log(1, "fresh", "f", "m")
    assert store.prune_events(days=30, keep=5000) == 3
    assert store.count_events() == 5

    for _ in range(12):
        store.log(1, "bulk", "", "")
    assert store.prune_events(days=30, keep=4) == 13
    assert store.count_events() == 4


def test_pair_allow_deletes_and_tombstones(tmp_path):
    s = Store(tmp_path / "s.db")
    p = s.add_pair(1, "/tmp/x", "root")           # default: deletes allowed
    assert p["allow_deletes"] == 1
    s.update_pair(p["id"], allow_deletes=False)
    assert s.pair(p["id"])["allow_deletes"] == 0
    s.base_put(p["id"], "a.txt", "file", remote_id="r1",
               local_md5="m", local_size=5, local_mtime=1.0,
               remote_md5="rm", remote_version="1")
    s.base_mark_deleted(p["id"], "a.txt", "remote")
    row = {r["rel_path"]: dict(r) for r in s.base_entries(p["id"])}["a.txt"]
    assert row["deleted_remote"] == 1 and row["remote_md5"] is None
    assert row["local_md5"] == "m"                # local fields untouched
    s.base_mark_deleted(p["id"], "a.txt", "local")
    row = {r["rel_path"]: dict(r) for r in s.base_entries(p["id"])}["a.txt"]
    assert row["deleted_local"] == 1 and row["local_md5"] is None
    s.close()


def test_migration_adds_columns_to_existing_db(tmp_path):
    """A database from an older build must gain the new columns on open."""
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE pairs (id INTEGER PRIMARY KEY, account_id INTEGER,
            local_path TEXT, remote_id TEXT, remote_path TEXT DEFAULT '',
            direction TEXT DEFAULT 'bidirectional',
            conflict_policy TEXT DEFAULT 'newer',
            enabled INTEGER DEFAULT 1);
        CREATE TABLE entries (pair_id INTEGER, rel_path TEXT, kind TEXT,
            remote_id TEXT, local_md5 TEXT, local_size INTEGER,
            local_mtime REAL, remote_md5 TEXT, remote_version TEXT,
            PRIMARY KEY (pair_id, rel_path));
    """)
    conn.commit()
    conn.close()
    s = Store(path)
    assert s.pair(1) is None                      # opens fine, no crash
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(pairs)")}
    assert "allow_deletes" in cols
    ecols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(entries)")}
    assert {"deleted_local", "deleted_remote"} <= ecols
    s.close()


def test_to_local_renders_local_timezone():
    """Displayed event timestamps are local wall-clock, not UTC."""
    utc_dt = datetime(2026, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
    out = to_local(utc_dt.isoformat())
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", out)
    # parsing back as local wall time must give the same instant
    assert datetime.fromisoformat(out).astimezone().timestamp() == utc_dt.timestamp()
