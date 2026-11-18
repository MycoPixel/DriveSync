"""SQLite state store: accounts, pairs, base snapshots and the activity log."""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY,
    email       TEXT UNIQUE NOT NULL,
    token_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pairs (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    local_path      TEXT NOT NULL,
    remote_id       TEXT NOT NULL,
    remote_path     TEXT NOT NULL DEFAULT '',
    direction       TEXT NOT NULL DEFAULT 'bidirectional',
    conflict_policy TEXT NOT NULL DEFAULT 'newer',
    enabled         INTEGER NOT NULL DEFAULT 1,
    allow_deletes   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS entries (
    pair_id         INTEGER NOT NULL REFERENCES pairs(id),
    rel_path        TEXT NOT NULL,
    kind            TEXT NOT NULL,             -- file | dir | gdoc
    remote_id       TEXT,
    local_md5       TEXT,
    local_size      INTEGER,
    local_mtime     REAL,
    remote_md5      TEXT,
    remote_version  TEXT,
    deleted_local   INTEGER NOT NULL DEFAULT 0,  -- deletion acknowledged, remote kept
    deleted_remote  INTEGER NOT NULL DEFAULT 0,  -- deletion acknowledged, local kept
    PRIMARY KEY (pair_id, rel_path)
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    pair_id  INTEGER,
    level    TEXT NOT NULL DEFAULT 'info',     -- info | conflict | error
    action   TEXT NOT NULL,
    rel_path TEXT DEFAULT '',
    message  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_entries_pair ON entries(pair_id);
CREATE INDEX IF NOT EXISTS idx_events_pair  ON events(pair_id);
"""

_DIRECTIONS = ("bidirectional", "upload", "download")
_POLICIES = ("newer", "local", "remote", "both")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Thin thread-safe wrapper around the SQLite database."""

    def __init__(self, path):
        self.path = str(path)
        if path:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    # Columns added after the first release; ALTER is idempotent per launch.
    _MIGRATIONS = {
        "pairs": ["allow_deletes INTEGER NOT NULL DEFAULT 1"],
        "entries": ["deleted_local INTEGER NOT NULL DEFAULT 0",
                    "deleted_remote INTEGER NOT NULL DEFAULT 0"],
    }

    def _migrate(self):
        for table, columns in self._MIGRATIONS.items():
            have = {row[1] for row in
                    self._conn.execute(f"PRAGMA table_info({table})")}
            for col in columns:
                if col.split()[0] not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")

    def close(self):
        with self._lock:
            self._conn.close()

    def _exec(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    # ---- accounts -------------------------------------------------
    def add_account(self, email, token_json):
        # Upsert: on re-auth the INSERT takes the DO UPDATE path, where
        # lastrowid is NOT set (stays 0/None) — look up by email instead.
        self._exec(
            "INSERT INTO accounts(email, token_json, created_at) VALUES (?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET token_json=excluded.token_json",
            (email, json.dumps(token_json), utcnow()))
        return self._exec("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()

    def accounts(self):
        return self._exec("SELECT * FROM accounts ORDER BY id").fetchall()

    def account(self, account_id):
        return self._exec("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()

    def update_account_tokens(self, account_id, token_json):
        self._exec("UPDATE accounts SET token_json=? WHERE id=?",
                   (json.dumps(token_json), account_id))

    def delete_account(self, account_id):
        self._exec("DELETE FROM entries WHERE pair_id IN "
                   "(SELECT id FROM pairs WHERE account_id=?)", (account_id,))
        self._exec("DELETE FROM pairs WHERE account_id=?", (account_id,))
        self._exec("DELETE FROM accounts WHERE id=?", (account_id,))

    # ---- pairs ----------------------------------------------------
    def add_pair(self, account_id, local_path, remote_id, remote_path="",
                 direction="bidirectional", conflict_policy="newer", enabled=1,
                 allow_deletes=1):
        if direction not in _DIRECTIONS:
            raise ValueError(f"direction must be one of {_DIRECTIONS}")
        if conflict_policy not in _POLICIES:
            raise ValueError(f"conflict_policy must be one of {_POLICIES}")
        cur = self._exec(
            "INSERT INTO pairs(account_id, local_path, remote_id, remote_path, "
            "direction, conflict_policy, enabled, allow_deletes) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (account_id, str(local_path), remote_id, remote_path,
             direction, conflict_policy, int(bool(enabled)),
             int(bool(allow_deletes))))
        return self.pair(cur.lastrowid)

    def pairs(self, enabled_only=False):
        sql = "SELECT * FROM pairs"
        if enabled_only:
            sql += " WHERE enabled=1"
        return self._exec(sql + " ORDER BY id").fetchall()

    def pair(self, pair_id):
        return self._exec("SELECT * FROM pairs WHERE id=?", (pair_id,)).fetchone()

    def update_pair(self, pair_id, **fields):
        allowed = {"local_path", "remote_id", "remote_path", "direction",
                   "conflict_policy", "enabled", "allow_deletes"}
        keys = [k for k in fields if k in allowed]
        if not keys:
            return self.pair(pair_id)
        if "direction" in fields and fields["direction"] not in _DIRECTIONS:
            raise ValueError(f"direction must be one of {_DIRECTIONS}")
        if "conflict_policy" in fields and fields["conflict_policy"] not in _POLICIES:
            raise ValueError(f"conflict_policy must be one of {_POLICIES}")
        if "enabled" in fields:
            fields["enabled"] = int(bool(fields["enabled"]))
        if "allow_deletes" in fields:
            fields["allow_deletes"] = int(bool(fields["allow_deletes"]))
        sets = ", ".join(f"{k}=?" for k in keys)
        self._exec(f"UPDATE pairs SET {sets} WHERE id=?",
                   tuple(fields[k] for k in keys) + (pair_id,))
        return self.pair(pair_id)

    def delete_pair(self, pair_id):
        self._exec("DELETE FROM entries WHERE pair_id=?", (pair_id,))
        self._exec("DELETE FROM pairs WHERE id=?", (pair_id,))

    # ---- base snapshots -------------------------------------------
    def base_entries(self, pair_id):
        return self._exec("SELECT * FROM entries WHERE pair_id=?", (pair_id,)).fetchall()

    def base_get(self, pair_id, rel):
        return self._exec(
            "SELECT * FROM entries WHERE pair_id=? AND rel_path=?",
            (pair_id, rel)).fetchone()

    def base_put(self, pair_id, rel, kind, remote_id=None, local_md5=None,
                 local_size=None, local_mtime=None, remote_md5=None,
                 remote_version=None):
        self._exec(
            "INSERT INTO entries(pair_id, rel_path, kind, remote_id, local_md5, "
            "local_size, local_mtime, remote_md5, remote_version) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(pair_id, rel_path) DO UPDATE SET "
            "kind=excluded.kind, remote_id=excluded.remote_id, "
            "local_md5=excluded.local_md5, local_size=excluded.local_size, "
            "local_mtime=excluded.local_mtime, remote_md5=excluded.remote_md5, "
            "remote_version=excluded.remote_version",
            (pair_id, rel, kind, remote_id, local_md5, local_size,
             local_mtime, remote_md5, remote_version))

    def base_delete(self, pair_id, rel):
        self._exec("DELETE FROM entries WHERE pair_id=? AND rel_path=?", (pair_id, rel))

    def base_mark_deleted(self, pair_id, rel, side):
        """Tombstone a one-sided deletion without touching either copy.

        side='local': the local file was deleted; keep the remote copy and
        record the absence so later passes stay stable (deletes-off mode).
        side='remote': mirror image — keep the local copy.
        """
        if side == "local":
            sets = ("deleted_local=1, local_md5=NULL, local_size=NULL, "
                    "local_mtime=NULL")
        else:
            sets = "deleted_remote=1, remote_md5=NULL, remote_version=NULL"
        self._exec(f"UPDATE entries SET {sets} WHERE pair_id=? AND rel_path=?",
                   (pair_id, rel))

    def base_rename(self, pair_id, old_rel, new_rel):
        self._exec("UPDATE entries SET rel_path=? WHERE pair_id=? AND rel_path=?",
                   (new_rel, pair_id, old_rel))

    def base_clear(self, pair_id):
        self._exec("DELETE FROM entries WHERE pair_id=?", (pair_id,))

    # ---- events ---------------------------------------------------
    def log(self, pair_id, action, rel_path="", message="", level="info"):
        self._exec(
            "INSERT INTO events(ts, pair_id, level, action, rel_path, message) "
            "VALUES (?,?,?,?,?,?)",
            (utcnow(), pair_id, level, action, rel_path, message))

    def recent_events(self, limit=200, level=None, pair_id=None):
        sql = "SELECT * FROM events"
        conds, params = [], []
        if level:
            conds.append("level=?"); params.append(level)
        if pair_id:
            conds.append("pair_id=?"); params.append(pair_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return self._exec(sql, tuple(params)).fetchall()
