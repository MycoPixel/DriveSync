"""Background worker: syncs every enabled pair on an interval, on demand."""

import threading
import time
from datetime import datetime

try:
    import fcntl  # Unix only: cross-process sync lock
except ImportError:  # pragma: no cover
    fcntl = None

from . import engine
from .auth import access_token_persisting, token_from_json
from .config import app_dir
from .drive import Drive
from .limits import TokenBucket


def _hhmm(s):
    h, _, m = s.strip().partition(":")
    return int(h) * 60 + int(m)


def in_window(now_time, start_s, end_s):
    """True when now_time is inside the optional HH:MM sync window.

    Empty bounds are unbounded; start > end wraps past midnight; invalid
    values degrade to 'always sync' rather than silently pausing forever.
    """
    try:
        lo = _hhmm(start_s) if start_s else None
        hi = _hhmm(end_s) if end_s else None
    except ValueError:
        return True
    cur = now_time.hour * 60 + now_time.minute
    if lo is None and hi is None:
        return True
    if lo is None:
        return cur < hi
    if hi is None:
        return cur >= lo
    if lo <= hi:
        return lo <= cur < hi
    return cur >= lo or cur < hi      # window spans midnight


class Worker:
    """Runs sync passes in a daemon thread; wake_event triggers an immediate run."""

    def __init__(self, store, config, interval=None, config_source=None):
        self.store = store
        self.config = config
        # Optional callable returning the current config (load_config). When
        # given, settings changed outside this process (dashboard, CLI) are
        # picked up on the next pass instead of requiring a restart.
        self._config_source = config_source
        self._interval_override = interval
        self.interval = interval or config.get("sync_interval", 30)
        self.wake = threading.Event()
        self.stop_flag = threading.Event()
        self._paused = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="drivesync-worker")
        self.status = {"running": False, "paused": False, "last_run": None,
                       "last_error": None, "last_stats": None,
                       "waiting_until": None}
        self._lock = threading.Lock()
        self._lockfh = None
        self._limit_kb = int(config.get("bandwidth_limit_kb", 0) or 0)
        self.limiter = TokenBucket(self._limit_kb * 1024) \
            if self._limit_kb > 0 else None
        self._pending = set()           # pair ids requested via sync_now()
        # keep the activity log bounded: every pass logs a summary row, so a
        # service at the default interval writes ~3k rows/day. Best-effort —
        # a broken store must not stop the worker from starting.
        try:
            self.store.prune_events()
        except Exception:
            pass

    def apply_config(self, cfg):
        """Adopt a freshly loaded config without restarting the worker.

        Ignore patterns, concurrency, the bandwidth limiter, the sync window
        and incremental scanning are all read from self.config — but the
        worker used to cache the dict from construction, so dashboard/CLI
        changes were ignored until a restart.
        """
        self.config = dict(cfg)
        if self._interval_override is None:
            try:
                self.interval = int(cfg.get("sync_interval", self.interval))
            except (TypeError, ValueError):
                pass
        try:
            limit = int(cfg.get("bandwidth_limit_kb", 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit != self._limit_kb:
            self._limit_kb = limit
            self.limiter = TokenBucket(limit * 1024) if limit > 0 else None

    def _reload_config(self):
        if not self._config_source:
            return
        try:
            self.apply_config(self._config_source())
        except Exception:
            pass                    # a broken config file must not stop syncing

    def start(self):
        self._thread.start()

    def stop(self):
        self.stop_flag.set()
        self.wake.set()
        self._thread.join(timeout=10)

    def pause(self):
        """Skip sync passes while keeping the worker thread (dashboard up)."""
        self._paused.set()
        self.wake.set()                 # wake a sleeping loop promptly
        self.status["paused"] = True

    def resume(self):
        """Allow sync passes again after a pause."""
        self._paused.clear()
        self.status["paused"] = False
        self.wake.set()

    def sync_now(self, pair_id=None):
        """Request an immediate sync pass (optionally one pair only)."""
        if pair_id is not None:
            self._pending.add(pair_id)
        self.wake.set()

    def make_drive(self, account_row):
        token = token_from_json(account_row["token_json"])
        return Drive(
            lambda: access_token_persisting(self.store, account_row["id"],
                                            token),
            limiter=self.limiter)

    def run_pass(self, pair_id=None, force_full=False):
        """One sync sweep across all enabled pairs (or the given pair/ids).

        pair_id: None (all pairs), one id, or a list of ids (inotify bursts
        coalesced by the watcher). force_full re-lists Drive and rehashes
        local files even when size+mtime look unchanged — the recovery for
        same-mtime saves ('drivesync sync --full').

        Returns (SyncStats totals, ok). ok is False when any pair, account or
        the pass itself failed — 'drivesync sync' turns that into exit 2.
        """
        self._reload_config()
        total = engine.SyncStats()
        # a fresh pass supersedes the previous failure; leave stale errors
        # in the activity log (where they belong) instead of status
        self.status["last_error"] = None
        with self._lock:
            if self.status["running"]:
                self.store.log(None, "sync-skipped", "",
                               "previous pass is still running")
                return total, False
            self.status["running"] = True
        if not self._acquire_sync_lock():
            with self._lock:
                self.status["running"] = False
            self.store.log(None, "sync-skipped", "",
                           "another DriveSync process is syncing")
            return total, False
        overall_ok = True
        try:
            pairs = self.store.pairs(enabled_only=True)
            if pair_id:
                wanted = {pair_id} if isinstance(pair_id, int) else set(pair_id)
                pairs = [p for p in pairs if p["id"] in wanted]
            groups = {}
            for p in pairs:
                groups.setdefault(p["account_id"], []).append(p)
            incremental = bool(self.config.get("incremental_scan", True))
            for acc_id in sorted(groups):
                account = self.store.account(acc_id)
                if not account:
                    for pair in groups[acc_id]:
                        self.store.log(pair["id"], "error",
                                       message="account missing", level="error")
                        total.errors += 1
                    overall_ok = False
                    continue
                drive = self.make_drive(account)
                feed, new_token = None, None
                if incremental:
                    feed, new_token = self._fetch_changes(drive, acc_id)
                for pair in groups[acc_id]:
                    # A pair that failed last pass does a full scan now: its
                    # error may have hidden remote changes the delta does not
                    # replay, and a full scan is the only way to re-see them.
                    stale = bool(self.store.kv_get(f"stale:{pair['id']}"))
                    try:
                        stats, ok = engine.sync_pair(
                            self.store, self.config, pair, drive,
                            force_full=force_full,
                            changes=None if stale else feed)
                    except Exception as exc:  # keep the worker alive no matter what
                        self.status["last_error"] = str(exc)
                        self.store.log(pair["id"], "worker-error", "",
                                       str(exc), level="error")
                        self.store.kv_set(f"stale:{pair['id']}", "1")
                        total.errors += 1
                        overall_ok = False
                        continue
                    total.merge(stats)
                    if ok:
                        if stale:
                            self.store.kv_delete(f"stale:{pair['id']}")
                        self.store.log(pair["id"], "summary", "",
                                       f"pushed {stats.pushed}, pulled {stats.pulled}, "
                                       f"deleted {stats.deleted}, renamed {stats.renamed}, "
                                       f"conflicts {stats.conflicts}")
                        self.status["last_stats"] = {
                            "pair_id": pair["id"], "pushed": int(stats.pushed),
                            "pulled": int(stats.pulled),
                            "deleted": int(stats.deleted),
                            "renamed": int(stats.renamed),
                            "conflicts": int(stats.conflicts),
                        }
                    else:
                        self.store.kv_set(f"stale:{pair['id']}", "1")
                        overall_ok = False
                    self.status["last_run"] = time.time()
                # The feed token advances on unfiltered passes even when a
                # pair is stale: that pair full-scans next time (see above),
                # while advancing here keeps a repeatedly failing file from
                # making the changes feed grow without bound. Filtered passes
                # never advance it — the other pairs were not swept.
                if incremental and new_token and pair_id is None:
                    self.store.kv_set(f"changes_token:{acc_id}", new_token)
        except Exception as exc:  # keep the worker alive no matter what
            self.status["last_error"] = str(exc)
            self.store.log(None, "worker-error", "", str(exc), level="error")
            total.errors += 1
            overall_ok = False
        finally:
            self._release_sync_lock()
            with self._lock:
                self.status["running"] = False
        return total, overall_ok

    def _fetch_changes(self, drive, account_id):
        """(feed_items | None, new_token | None) for one account.

        None feed = full scan this pass. First run: no token yet — capture
        the 'now' token BEFORE the full scan so the next delta pass cannot
        miss anything that happened between the scan and the token. A broken
        stored token self-heals: it is dropped and re-baselined next pass.
        """
        key = f"changes_token:{account_id}"
        stored = self.store.kv_get(key)
        try:
            if stored:
                items, new_token = drive.changes_list(stored)
                return items, new_token
            return None, drive.changes_start_token()
        except Exception:
            if stored:
                self.store.kv_delete(key)
            return None, None

    # ---- cross-process guard ------------------------------------------
    def _acquire_sync_lock(self):
        """Advisory flock so 'serve' and 'gui' never double-sync.

        Kernel-managed: a crashed process releases automatically. Returns
        False when another DriveSync process is mid-pass — the caller skips
        this round instead of queueing a duplicate.
        """
        if fcntl is None:
            return True
        try:
            app_dir().mkdir(parents=True, exist_ok=True)
            fh = open(app_dir() / "sync.lock", "w")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return False
            self._lockfh = fh
            return True
        except OSError:
            return True  # no writable data dir: let the pass proceed

    def _release_sync_lock(self):
        if getattr(self, "_lockfh", None):
            try:
                fcntl.flock(self._lockfh, fcntl.LOCK_UN)
            finally:
                self._lockfh.close()
                self._lockfh = None

    # ---- internals ------------------------------------------------
    def _loop(self):
        while not self.stop_flag.is_set():
            self.wake.wait(timeout=self.interval)
            self.wake.clear()
            if self.stop_flag.is_set():
                break
            if self._paused.is_set():
                continue
            ids = list(self._pending)
            self._pending.clear()
            if not in_window(datetime.now().time(),
                             self.config.get("active_from", ""),
                             self.config.get("active_to", "")):
                self.status["waiting_until"] = \
                    self.config.get("active_to") \
                    or self.config.get("active_from") or None
                continue
            self.status["waiting_until"] = None
            self.run_pass(ids or None)
