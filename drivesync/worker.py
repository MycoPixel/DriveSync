"""Background worker: syncs every enabled pair on an interval, on demand."""

import threading
import time

try:
    import fcntl  # Unix only: cross-process sync lock
except ImportError:  # pragma: no cover
    fcntl = None

from . import engine
from .auth import access_token, token_from_json
from .config import app_dir
from .drive import Drive


class Worker:
    """Runs sync passes in a daemon thread; wake_event triggers an immediate run."""

    def __init__(self, store, config, interval=None):
        self.store = store
        self.config = config
        self.interval = interval or config.get("sync_interval", 30)
        self.wake = threading.Event()
        self.stop_flag = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="drivesync-worker")
        self.status = {"running": False, "paused": False, "last_run": None,
                       "last_error": None, "last_stats": None}
        self._lock = threading.Lock()
        self._lockfh = None

    def start(self):
        self._thread.start()

    def stop(self):
        self.stop_flag.set()
        self.wake.set()
        self._thread.join(timeout=10)

    def pause(self):
        """Stop the background loop (dashboard stays up); resume() restarts it."""
        if self._thread.is_alive():
            self.stop()
            self.status["paused"] = True

    def resume(self):
        """Restart the background loop after a pause."""
        if self._thread.is_alive():
            return
        self.stop_flag.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="drivesync-worker")
        self._thread.start()
        self.status["paused"] = False

    def sync_now(self, pair_id=None):
        """Request an immediate sync pass (optionally one pair only)."""
        self.wake.set()
        self._pending_pair = pair_id

    def make_drive(self, account_row):
        token = token_from_json(account_row["token_json"])

        def provider():
            tok = access_token(token)
            # persist refreshed tokens so restarts do not re-auth needlessly
            self.store.update_account_tokens(account_row["id"], token)
            return tok

        return Drive(provider)

    def run_pass(self, pair_id=None):
        """One sync sweep across all enabled pairs (or a single pair)."""
        with self._lock:
            if self.status["running"]:
                return
            self.status["running"] = True
        if not self._acquire_sync_lock():
            with self._lock:
                self.status["running"] = False
            return
        try:
            pairs = self.store.pairs(enabled_only=True)
            if pair_id is not None:
                pairs = [p for p in pairs if p["id"] == pair_id]
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
                    continue
                drive = self.make_drive(account)
                feed, new_token = None, None
                if incremental:
                    feed, new_token = self._fetch_changes(drive, acc_id)
                all_ok = True
                for pair in groups[acc_id]:
                    try:
                        stats, ok = engine.sync_pair(
                            self.store, self.config, pair, drive, changes=feed)
                    except Exception as exc:  # keep the worker alive no matter what
                        self.status["last_error"] = str(exc)
                        self.store.log(pair["id"], "worker-error", "",
                                       str(exc), level="error")
                        all_ok = False
                        continue
                    if ok:
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
                        all_ok = False
                    self.status["last_run"] = time.time()
                # The feed token advances only when the whole account was
                # swept clean and unfiltered: a filtered or failing run keeps
                # re-reading the same feed (decide() no-ops unchanged paths).
                if incremental and new_token and all_ok and pair_id is None:
                    self.store.kv_set(f"changes_token:{acc_id}", new_token)
        except Exception as exc:  # keep the worker alive no matter what
            self.status["last_error"] = str(exc)
            self.store.log(None, "worker-error", "", str(exc), level="error")
        finally:
            self._release_sync_lock()
            with self._lock:
                self.status["running"] = False

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
            pair_id = getattr(self, "_pending_pair", None)
            self._pending_pair = None
            self.run_pass(pair_id)
