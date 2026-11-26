"""SQLite state store: accounts, pairs, base snapshots and the activity log."""

import json
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .security import is_data_dir

_DATA_DIR_MSG = ("that folder is DriveSync's own data directory (it holds "
                 "your OAuth tokens and client secret) and cannot be synced")

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
    allow_deletes   INTEGER NOT NULL DEFAULT 1,
    include_patterns TEXT NOT NULL DEFAULT ''
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
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    pair_id   INTEGER,
    rel_path  TEXT DEFAULT '',
    action    TEXT NOT NULL,     -- safety_stop | deletions_allowed | deletions_kept
                                 -- pending_cleared | trash_restored | drive_trash_restored
                                 -- version_restored | conflict_resolved
    reason    TEXT DEFAULT '',   -- what triggered it (limit tripped, user action...)
    detail    TEXT DEFAULT ''    -- human-readable summary (counts, destinations)
);
CREATE INDEX IF NOT EXISTS idx_entries_pair ON entries(pair_id);
CREATE INDEX IF NOT EXISTS idx_events_pair  ON events(pair_id);
CREATE INDEX IF NOT EXISTS idx_events_ts    ON events(ts);
CREATE INDEX IF NOT EXISTS idx_recovery_ts  ON recovery(ts);
"""

_DIRECTIONS = ("bidirectional", "upload", "download")
_POLICIES = ("newer", "local", "remote", "both")
_SYMLINK_POLICIES = ("skip", "follow")


def normalize_include_patterns(value) -> str:
    """Canonical storage form for a pair's selective-sync scope.

    Accepts None / '' (everything), a list of subfolder paths
    ("docs", "photos/2024"), or a comma-separated string, and returns the
    canonical JSON list string ('[]' means 'no scope chosen' -> everything
    syncs). Raises ValueError when the value cannot be understood so callers
    get an error instead of silently syncing nothing.
    """
    if value is None:
        return "[]"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "[]"
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = [part.strip() for part in text.split(",")]
        if not isinstance(decoded, list):
            raise ValueError("include_patterns must be a list of subfolder "
                             "paths or a comma-separated string")
        items = decoded
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError("include_patterns must be a list of subfolder paths "
                         "or a comma-separated string")
    if not all(isinstance(x, str) for x in items):
        raise ValueError("include_patterns entries must be strings")
    cleaned = []
    for item in items:
        parts = "/".join(p for p in item.strip().split("/") if p and p != ".")
        if parts and parts not in cleaned and parts not in (".", ".."):
            cleaned.append(parts)
    return json.dumps(sorted(cleaned))


def parse_include_patterns(value) -> list:
    """Stored include_patterns column -> list of subfolder paths.

    Broken or legacy values degrade to [] (= everything syncs) — a scope
    must never silently hide the whole pair because of a bad string.
    """
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(decoded, list) and all(isinstance(x, str) for x in decoded):
        return decoded
    return []


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def roots_overlap(a, b) -> bool:
    """True when two local sync roots are the same or nested.

    Two pairs writing the same files would corrupt both base snapshots, so
    add_pair refuses overlapping roots.
    """
    a = os.path.realpath(str(a))
    b = os.path.realpath(str(b))
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


def _valid_hhmm(v):
    """'HH:MM' with 0<=HH<=23, 0<=MM<=59 (same rule as config.py)."""
    try:
        h, m = str(v).split(":")
        return len(str(v)) == 5 and 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except (ValueError, AttributeError):
        return False


def _validate_pair_qos(bandwidth_limit_kb, peak_from, peak_to,
                       bandwidth_limit_kb_peak):
    """Normalize a per-pair QoS quartet or raise ValueError.

    None means 'inherit the global setting'. The peak window must be
    set as a pair; caps are integers >= 0 (0 keeps the fall-back
    semantics of Phase 4.1: use the general cap inside the window).
    """
    out = []
    for v in (bandwidth_limit_kb, bandwidth_limit_kb_peak):
        if v is None or v == "":
            out.append(None)
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            raise ValueError("bandwidth limits must be integers (KB/s)")
        if iv < 0:
            raise ValueError("bandwidth limits must be 0 (inherit/fall "
                             "back) or higher")
        out.append(iv)
    win = []
    for v in (peak_from, peak_to):
        v = str(v).strip() if v is not None else ""
        if v and not _valid_hhmm(v):
            raise ValueError("peak_from/peak_to must be empty or HH:MM "
                             "(e.g. 08:00)")
        win.append(v or None)
    if (win[0] is not None) != (win[1] is not None):
        raise ValueError("peak_from and peak_to must be set together "
                         "(or both empty to inherit)")
    return out[0], win[0], win[1], out[1]


def to_local(iso: str) -> str:
    """Render a stored UTC timestamp as local wall-clock time.

    Timestamps are stored in UTC (portable across machines); every place
    that shows them to a human converts to the viewer's timezone.
    """
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:            # old rows predating the offset suffix
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """Thin thread-safe wrapper around the SQLite database."""

    def __init__(self, path):
        self.path = str(path)
        if path:
            parent = Path(self.path).parent
            parent.mkdir(parents=True, exist_ok=True)
            # the DB holds OAuth refresh tokens: private to this user
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        self._lock = threading.RLock()
        self._batch_depth = 0
        self.recovered = False
        self.keyring_enabled = False
        self._keyring_ok = None
        self._conn = self._connect_recovering()
        with self._lock:
            # WAL lets the dashboard read while a sync pass writes; NORMAL
            # fsyncs the WAL rather than every commit (safe against process
            # crashes, only a power cut can lose the last pass).
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError:  # e.g. read-only media
                pass
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()
        self._harden_files()

    def _harden_files(self):
        """Keep the DB and its sidecars private.

        SQLite creates ``-wal``/``-shm`` files with the process umask, which
        can be 0644 even when the database itself was chmodded 0600 — and
        the WAL can contain freshly written refresh tokens. Re-apply the
        mode after opening (and after a checkpoint recreates them).
        """
        for suffix in ("", "-wal", "-shm", ".bak"):
            path = self.path + suffix
            try:
                if os.path.exists(path):
                    os.chmod(path, 0o600)
            except OSError:
                pass

    # ---- state.db backup + recovery --------------------------------
    def _connect_recovering(self):
        """Open the database, falling back to state.db.bak when it is corrupt.

        The whole engine leans on this one SQLite file; a truncated or
        half-written state.db (power cut, full disk) used to be a hard stop.
        A clean pass keeps a fresh backup next to it, so corruption becomes
        a hiccup: restore the backup and carry on.
        """
        bak = self.path + ".bak"
        if not os.path.exists(self.path) and os.path.exists(bak):
            shutil.copy2(bak, self.path)
            self.recovered = True
        conn = self._new_conn()
        if self._healthy(conn):
            return conn
        conn.close()
        if not os.path.exists(bak):
            raise sqlite3.DatabaseError(
                f"state database {self.path} is corrupt and no backup exists")
        for side in ("-wal", "-shm"):
            stale = self.path + side
            if os.path.exists(stale):
                try:
                    os.remove(stale)
                except OSError:
                    pass
        shutil.copy2(bak, self.path)
        conn = self._new_conn()
        if not self._healthy(conn):
            conn.close()
            raise sqlite3.DatabaseError(
                f"state database {self.path} is corrupt and its backup did "
                "not survive either")
        self.recovered = True
        return conn

    def _new_conn(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _healthy(conn):
        """False when SQLite cannot actually read the file (corruption)."""
        try:
            conn.execute("PRAGMA quick_check").fetchone()
            return True
        except sqlite3.DatabaseError:
            return False

    def backup(self):
        """Snapshot the live database to state.db.bak (atomic replace).

        Uses SQLite's online backup API, so the copy is consistent even
        while sync passes write. Returns the backup path ('' for in-memory
        databases, which have nothing to back up).
        """
        if not self.path or self.path == ":memory:":
            return ""
        tmp = self.path + ".bak.tmp"
        dst = sqlite3.connect(tmp)
        try:
            with self._lock:
                self._conn.backup(dst)
        finally:
            dst.close()
        os.replace(tmp, self.path + ".bak")
        try:
            os.chmod(self.path + ".bak", 0o600)
        except OSError:
            pass
        return self.path + ".bak"

    # Columns added after the first release; ALTER is idempotent per launch.
    _MIGRATIONS = {
        "pairs": ["allow_deletes INTEGER NOT NULL DEFAULT 1",
                  "include_patterns TEXT NOT NULL DEFAULT ''",
                  "symlink_policy TEXT NOT NULL DEFAULT 'skip'",
                  "case_insensitive INTEGER NOT NULL DEFAULT 0",
                  "paused_until TEXT",
                  # per-pair QoS (Phase 4.2): NULL = inherit the global
                  # settings; the window pair follows the same rules as
                  # config peak_from/peak_to
                  "bandwidth_limit_kb INTEGER",
                  "peak_from TEXT",
                  "peak_to TEXT",
                  "bandwidth_limit_kb_peak INTEGER"],
        "entries": ["deleted_local INTEGER NOT NULL DEFAULT 0",
                    "deleted_remote INTEGER NOT NULL DEFAULT 0",
                    "remote_size REAL",
                    "remote_mtime REAL",
                    "remote_mime TEXT"],
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
            # Fold the WAL back into the main file so a stopped service
            # leaves a compact state.db (and no large -wal sibling).
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:
                pass
            self._conn.close()
            self._harden_files()

    def optimize(self):
        """Refresh SQLite planner statistics (cheap; safe to call often)."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA optimize")
            except sqlite3.DatabaseError:
                pass

    @contextmanager
    def batch(self):
        """Group writes into a single commit (a sync op writes several rows).

        Nested use is safe: only the outermost context commits. Writes stay
        visible to this connection immediately; other processes see them
        when the batch ends.
        """
        with self._lock:
            self._batch_depth += 1
        try:
            yield self
        finally:
            with self._lock:
                self._batch_depth -= 1
                if self._batch_depth == 0:
                    self._conn.commit()

    def _exec(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            if not self._batch_depth:
                self._conn.commit()
            return cur

    # ---- optional OS keyring for OAuth tokens ----------------------
    _KEYRING_SERVICE = "drivesync"

    def _keyring(self):
        """The keyring module, or None when it is unavailable/not installed."""
        if self._keyring_ok is False:
            return None
        try:
            import keyring
            # A backend may exist but fail at runtime (no Secret Service
            # daemon, locked KWallet); probe cheaply once.
            keyring.get_keyring()
        except Exception:
            self._keyring_ok = False
            return None
        self._keyring_ok = True
        return keyring

    def _keyring_set(self, email, token_json):
        kr = self._keyring()
        if kr is None:
            return False
        try:
            kr.set_password(self._KEYRING_SERVICE, email,
                            json.dumps(token_json))
            return True
        except Exception:
            return False

    def _keyring_get(self, email):
        kr = self._keyring()
        if kr is None:
            return None
        try:
            raw = kr.get_password(self._KEYRING_SERVICE, email)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def _keyring_delete(self, email):
        kr = self._keyring()
        if kr is None:
            return
        try:
            kr.delete_password(self._KEYRING_SERVICE, email)
        except Exception:
            pass

    def _hydrate(self, row):
        """Replace a keyring marker with the real token dict."""
        if row is None:
            return None
        try:
            data = json.loads(row["token_json"]) if isinstance(
                row["token_json"], str) else row["token_json"]
        except (TypeError, ValueError):
            return row
        if isinstance(data, dict) and data.get("keyring"):
            token = self._keyring_get(row["email"])
            out = dict(row)
            out["token_json"] = json.dumps(token or {"keyring_missing": True})
            return out
        return row

    def configure_keyring(self, enabled):
        """Turn keyring storage on/off; returns whether it is active.

        Enabling migrates existing plaintext tokens into the keyring and
        leaves only a marker in SQLite. Disabling writes future tokens back
        to the database but does not destroy the keyring copy (a later
        re-enable finds it again).
        """
        self.keyring_enabled = bool(enabled)
        if not self.keyring_enabled:
            return False
        if self._keyring() is None:
            self.keyring_enabled = False
            return False
        try:
            rows = self._exec("SELECT * FROM accounts").fetchall()
        except Exception:
            return True
        for row in rows:
            try:
                data = json.loads(row["token_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("keyring"):
                continue
            if self._keyring_set(row["email"], data):
                self._exec("UPDATE accounts SET token_json=? WHERE id=?",
                           (json.dumps({"keyring": True}), row["id"]))
        return True

    # ---- accounts -------------------------------------------------
    def add_account(self, email, token_json):
        # Upsert: on re-auth the INSERT takes the DO UPDATE path, where
        # lastrowid is NOT set (stays 0/None) — look up by email instead.
        stored = token_json
        if self.keyring_enabled and self._keyring_set(email, token_json):
            stored = {"keyring": True}
        self._exec(
            "INSERT INTO accounts(email, token_json, created_at) VALUES (?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET token_json=excluded.token_json",
            (email, json.dumps(stored), utcnow()))
        return self._hydrate(
            self._exec("SELECT * FROM accounts WHERE email=?", (email,)).fetchone())

    def accounts(self):
        return [self._hydrate(row) for row in
                self._exec("SELECT * FROM accounts ORDER BY id").fetchall()]

    def account(self, account_id):
        return self._hydrate(self._exec(
            "SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone())

    def update_account_tokens(self, account_id, token_json):
        row = self._exec("SELECT email FROM accounts WHERE id=?",
                         (account_id,)).fetchone()
        stored = token_json
        if row and self.keyring_enabled and \
                self._keyring_set(row["email"], token_json):
            stored = {"keyring": True}
        self._exec("UPDATE accounts SET token_json=? WHERE id=?",
                   (json.dumps(stored), account_id))

    def delete_account(self, account_id):
        row = self._exec("SELECT email FROM accounts WHERE id=?",
                         (account_id,)).fetchone()
        if row:
            self._keyring_delete(row["email"])
        self._exec("DELETE FROM entries WHERE pair_id IN "
                   "(SELECT id FROM pairs WHERE account_id=?)", (account_id,))
        self._exec("DELETE FROM pairs WHERE account_id=?", (account_id,))
        self._exec("DELETE FROM accounts WHERE id=?", (account_id,))

    # ---- pairs ----------------------------------------------------
    def add_pair(self, account_id, local_path, remote_id, remote_path="",
                 direction="bidirectional", conflict_policy="newer", enabled=1,
                 allow_deletes=1, include_patterns=None, symlink_policy="skip",
                 case_insensitive=0,
                 bandwidth_limit_kb=None, peak_from=None, peak_to=None,
                 bandwidth_limit_kb_peak=None):
        bw, pf, pt, bwp = _validate_pair_qos(
            bandwidth_limit_kb, peak_from, peak_to,
            bandwidth_limit_kb_peak)
        if direction not in _DIRECTIONS:
            raise ValueError(f"direction must be one of {_DIRECTIONS}")
        if conflict_policy not in _POLICIES:
            raise ValueError(f"conflict_policy must be one of {_POLICIES}")
        if symlink_policy not in _SYMLINK_POLICIES:
            raise ValueError(
                f"symlink_policy must be one of {_SYMLINK_POLICIES}")
        if is_data_dir(local_path):
            raise ValueError(_DATA_DIR_MSG)
        for row in self.pairs():
            if roots_overlap(local_path, row["local_path"]):
                raise ValueError(
                    f"local path overlaps pair #{row['id']} "
                    f"({row['local_path']}); two pairs must not write the "
                    "same files")
        scope = normalize_include_patterns(include_patterns)
        cur = self._exec(
            "INSERT INTO pairs(account_id, local_path, remote_id, remote_path, "
            "direction, conflict_policy, enabled, allow_deletes, "
            "include_patterns, symlink_policy, case_insensitive, "
            "bandwidth_limit_kb, peak_from, peak_to, "
            "bandwidth_limit_kb_peak) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, str(local_path), remote_id, remote_path,
             direction, conflict_policy, int(bool(enabled)),
             int(bool(allow_deletes)), scope, symlink_policy,
             int(bool(case_insensitive)), bw, pf, pt, bwp))
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
                   "conflict_policy", "enabled", "allow_deletes",
                   "include_patterns", "symlink_policy", "case_insensitive",
                   "paused_until", "bandwidth_limit_kb", "peak_from",
                   "peak_to", "bandwidth_limit_kb_peak"}
        keys = [k for k in fields if k in allowed]
        if not keys:
            return self.pair(pair_id)
        if "direction" in fields and fields["direction"] not in _DIRECTIONS:
            raise ValueError(f"direction must be one of {_DIRECTIONS}")
        if "conflict_policy" in fields and fields["conflict_policy"] not in _POLICIES:
            raise ValueError(f"conflict_policy must be one of {_POLICIES}")
        if "symlink_policy" in fields and \
                fields["symlink_policy"] not in _SYMLINK_POLICIES:
            raise ValueError(
                f"symlink_policy must be one of {_SYMLINK_POLICIES}")
        if "local_path" in fields and is_data_dir(fields["local_path"]):
            raise ValueError(_DATA_DIR_MSG)
        if "enabled" in fields:
            fields["enabled"] = int(bool(fields["enabled"]))
        if "allow_deletes" in fields:
            fields["allow_deletes"] = int(bool(fields["allow_deletes"]))
        if "case_insensitive" in fields:
            fields["case_insensitive"] = int(bool(fields["case_insensitive"]))
        if "include_patterns" in fields:
            fields["include_patterns"] = \
                normalize_include_patterns(fields["include_patterns"])
        if "paused_until" in fields and fields["paused_until"] is not None:
            # ISO timestamp (UTC preferred, naive treated as UTC by readers);
            # a junk value would silently pause a pair forever
            try:
                datetime.fromisoformat(str(fields["paused_until"]))
            except ValueError:
                raise ValueError("paused_until must be an ISO timestamp "
                                 "(e.g. 2026-11-21T09:00:00) or null")
        current = self.pair(pair_id)
        if any(k in fields for k in ("bandwidth_limit_kb", "peak_from",
                                     "peak_to",
                                     "bandwidth_limit_kb_peak")):
            # validate the RESOLVED quartet, not just the fields sent: a
            # partial edit must not leave a half window behind
            bw, pf, pt, bwp = _validate_pair_qos(
                fields.get("bandwidth_limit_kb", current["bandwidth_limit_kb"]),
                fields.get("peak_from", current["peak_from"]),
                fields.get("peak_to", current["peak_to"]),
                fields.get("bandwidth_limit_kb_peak",
                           current["bandwidth_limit_kb_peak"]))
            fields["bandwidth_limit_kb"] = bw
            fields["peak_from"] = pf
            fields["peak_to"] = pt
            fields["bandwidth_limit_kb_peak"] = bwp
        # repointing a pair at a different tree invalidates its snapshot:
        # the old base rows would look like mass deletions on the next pass
        repointed = current and any(
            k in fields and fields[k] != current[k]
            for k in ("local_path", "remote_id"))
        sets = ", ".join(f"{k}=?" for k in keys)
        self._exec(f"UPDATE pairs SET {sets} WHERE id=?",
                   tuple(fields[k] for k in keys) + (pair_id,))
        if repointed:
            self._exec("DELETE FROM entries WHERE pair_id=?", (pair_id,))
            self.kv_delete(f"pair_synced:{pair_id}")
            self.kv_delete(f"stale:{pair_id}")
        return self.pair(pair_id)

    def delete_pair(self, pair_id):
        self._exec("DELETE FROM entries WHERE pair_id=?", (pair_id,))
        self._exec("DELETE FROM pairs WHERE id=?", (pair_id,))
        self._exec("DELETE FROM recovery WHERE pair_id=?", (pair_id,))
        self.kv_delete(f"pair_synced:{pair_id}")
        self.kv_delete(f"stale:{pair_id}")
        self.kv_delete(f"pending_deletes:{pair_id}")
        self.kv_delete(f"deletes_approved:{pair_id}")

    def snapshot_pair(self, pair_id):
        """Everything needed to undo a pair removal (pair + base + history)."""
        pair = self.pair(pair_id)
        if pair is None:
            return None
        return {
            "pair": dict(pair),
            "entries": [dict(r) for r in self.base_entries(pair_id)],
            "recovery": [dict(r) for r in self._exec(
                "SELECT * FROM recovery WHERE pair_id=?", (pair_id,)).fetchall()],
        }

    def restore_pair(self, snapshot):
        """Restore a snapshot from snapshot_pair; None if the id is taken."""
        pair = dict(snapshot["pair"])
        if self.pair(pair["id"]) is not None:
            return None
        columns = ", ".join(pair)
        marks = ",".join("?" for _ in pair)
        with self.batch():
            self._exec(f"INSERT INTO pairs({columns}) VALUES ({marks})",
                       tuple(pair.values()))
            for row in snapshot.get("entries", []):
                cols = ", ".join(row)
                marks = ",".join("?" for _ in row)
                self._exec(f"INSERT INTO entries({cols}) VALUES ({marks})",
                           tuple(row.values()))
            for row in snapshot.get("recovery", []):
                cols = ", ".join(row)
                marks = ",".join("?" for _ in row)
                self._exec(f"INSERT INTO recovery({cols}) VALUES ({marks})",
                           tuple(row.values()))
        return self.pair(pair["id"])

    # ---- base snapshots -------------------------------------------
    def base_entries(self, pair_id):
        return self._exec("SELECT * FROM entries WHERE pair_id=?", (pair_id,)).fetchall()

    def base_get(self, pair_id, rel):
        return self._exec(
            "SELECT * FROM entries WHERE pair_id=? AND rel_path=?",
            (pair_id, rel)).fetchone()

    def base_put(self, pair_id, rel, kind, remote_id=None, local_md5=None,
                 local_size=None, local_mtime=None, remote_md5=None,
                 remote_version=None, remote_size=None, remote_mtime=None,
                 remote_mime=None):
        self._exec(
            "INSERT INTO entries(pair_id, rel_path, kind, remote_id, local_md5, "
            "local_size, local_mtime, remote_md5, remote_version, "
            "remote_size, remote_mtime, remote_mime) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(pair_id, rel_path) DO UPDATE SET "
            "kind=excluded.kind, remote_id=excluded.remote_id, "
            "local_md5=excluded.local_md5, local_size=excluded.local_size, "
            "local_mtime=excluded.local_mtime, remote_md5=excluded.remote_md5, "
            "remote_version=excluded.remote_version, "
            "remote_size=excluded.remote_size, remote_mtime=excluded.remote_mtime, "
            "remote_mime=excluded.remote_mime",
            (pair_id, rel, kind, remote_id, local_md5, local_size,
             local_mtime, remote_md5, remote_version, remote_size,
             remote_mtime, remote_mime))

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
            sets = ("deleted_remote=1, remote_md5=NULL, remote_version=NULL, "
                    "remote_size=NULL, remote_mtime=NULL, remote_mime=NULL")
        self._exec(f"UPDATE entries SET {sets} WHERE pair_id=? AND rel_path=?",
                   (pair_id, rel))

    def base_rename(self, pair_id, old_rel, new_rel):
        self._exec("UPDATE entries SET rel_path=? WHERE pair_id=? AND rel_path=?",
                   (new_rel, pair_id, old_rel))

    def base_clear(self, pair_id):
        self._exec("DELETE FROM entries WHERE pair_id=?", (pair_id,))

    # ---- key/value (changes-feed tokens, misc state) ---------------
    def kv_get(self, key):
        row = self._exec("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key, value):
        self._exec("INSERT INTO kv(key, value) VALUES (?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, str(value)))

    def kv_delete(self, key):
        self._exec("DELETE FROM kv WHERE key=?", (key,))

    # ---- events ---------------------------------------------------
    def log(self, pair_id, action, rel_path="", message="", level="info"):
        # the events table feeds str-only GUI TreeViews — never store NULL
        self._exec(
            "INSERT INTO events(ts, pair_id, level, action, rel_path, message) "
            "VALUES (?,?,?,?,?,?)",
            (utcnow(), pair_id, level or "info", action,
             rel_path or "", message or ""))

    def digest_summary(self, since_id=0):
        """One-row activity rollup for the notifications digest (4.4).

        Counts FILE-level actions after event id `since_id` (push/pull/
        delete), plus conflicts and errors by level, and how many
        distinct pairs saw file activity. The window is keyed on the
        AUTOINCREMENT id, not the timestamp: ts has second precision, so
        same-second rows would leak into (or out of) a time window.
        Summary rows ('pass', 'metered-pause', the digest row itself)
        are excluded, so re-running never compounds. SQL conditional
        aggregation keeps it a single scan.
        """
        row = self._exec(
            "SELECT"
            "  SUM(action='push') AS pushed,"
            "  SUM(action='pull') AS pulled,"
            "  SUM(action='delete') AS deleted,"
            "  SUM(level='conflict') AS conflicts,"
            "  SUM(level='error') AS errors,"
            "  COUNT(DISTINCT CASE WHEN action IN"
            "    ('push','pull','delete','conflict') THEN pair_id END)"
            "    AS pairs"
            " FROM events WHERE id > ? AND action NOT IN"
            " ('pass','metered-pause','metered-resume','digest')",
            (int(since_id or 0),)).fetchone()
        g = lambda k: int(row[k] or 0) if row is not None else 0
        return {"pushed": g("pushed"), "pulled": g("pulled"),
                "deleted": g("deleted"), "conflicts": g("conflicts"),
                "errors": g("errors"), "pairs": g("pairs"),
                "files": g("pushed") + g("pulled") + g("deleted")}

    def last_event_id(self):
        """Highest event id — the digest bookmark for the next window."""
        row = self._exec("SELECT MAX(id) AS m FROM events").fetchone()
        return int(row["m"] or 0) if row is not None else 0

    def recent_events(self, limit=200, level=None, pair_id=None, action=None,
                      since=None):
        """Newest-first activity rows, optionally filtered.

        since is a UTC datetime (or ISO string): ts is stored as ISO text
        in one uniform format, so string comparison keeps chrono order.
        """
        sql = "SELECT * FROM events"
        conds, params = [], []
        if level:
            conds.append("level=?"); params.append(level)
        if pair_id:
            conds.append("pair_id=?"); params.append(pair_id)
        if action:
            conds.append("action=?"); params.append(action)
        if since:
            if hasattr(since, "isoformat"):
                since = since.isoformat(timespec="seconds")
            conds.append("ts>=?"); params.append(since)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return self._exec(sql, tuple(params)).fetchall()

    def event_actions(self):
        """Distinct actions in the log, for the activity filter dropdown."""
        return [r[0] for r in
                self._exec("SELECT DISTINCT action FROM events "
                           "ORDER BY action").fetchall()]

    # ---- recovery history (Phase 2.6) ------------------------------
    def recovery_log(self, pair_id, rel_path, action, reason="", detail=""):
        """One row in the recovery history.

        Everything a user might later need to understand what happened to
        their files: the original path, why (reason) and what was done
        (action + detail). The events table stays a chronological activity
        feed; this table is the queryable "what protection fired and what
        did the user decide" record.
        """
        self._exec(
            "INSERT INTO recovery(ts, pair_id, rel_path, action, reason, "
            "detail) VALUES (?,?,?,?,?,?)",
            (utcnow(), pair_id, rel_path or "", action, reason or "",
             detail or ""))

    def recent_recovery(self, limit=100, pair_id=None, action=None):
        """Newest-first recovery rows, optionally filtered."""
        sql = "SELECT * FROM recovery"
        conds, params = [], []
        if pair_id:
            conds.append("pair_id=?"); params.append(pair_id)
        if action:
            conds.append("action=?"); params.append(action)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return self._exec(sql, tuple(params)).fetchall()

    def clear_recovery(self):
        """Wipe the recovery history; returns the number of rows removed."""
        return self._exec("DELETE FROM recovery").rowcount

    def count_events(self):
        return self._exec("SELECT COUNT(*) FROM events").fetchone()[0]

    def clear_events(self):
        """Wipe the whole activity log; returns the number of rows removed."""
        return self._exec("DELETE FROM events").rowcount

    def prune_events(self, days=30, keep=5000):
        """Bound the activity log: drop rows older than `days`, then cap the
        table at the `keep` newest rows. The worker calls this on start so a
        service logging a summary row per pass (~3k/day at the default
        interval) cannot bloat state.db. Returns rows removed."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) \
            .isoformat(timespec="seconds")
        with self._lock:
            removed = self._conn.execute(
                "DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
            if self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] > keep:
                removed += self._conn.execute(
                    "DELETE FROM events WHERE id NOT IN "
                    "(SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                    (keep,)).rowcount
            self._conn.commit()
            try:
                self._conn.execute("PRAGMA optimize")
            except sqlite3.DatabaseError:
                pass
            return removed
