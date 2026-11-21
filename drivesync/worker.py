"""Background worker: syncs every enabled pair on an interval, on demand."""

import os
import socket
import threading
import time
from datetime import datetime, timezone

try:
    import fcntl  # Unix only: cross-process sync lock
except ImportError:  # pragma: no cover
    fcntl = None

from . import engine
from .auth import access_token_persisting, token_from_json
from .config import app_dir
from .drive import Drive
from .limits import TokenBucket


def sd_notify(state):
    """Best-effort systemd notification (no-op outside a service).

    Sends READY=1 / WATCHDOG=1 / STOPPING=1 to the socket systemd passes in
    NOTIFY_SOCKET. Implemented directly on an AF_UNIX datagram socket so the
    worker has no dependency on the systemd Python bindings.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):        # abstract namespace socket
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except OSError:
        pass


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
                       "waiting_until": None, "next_run": None,
                       "pairs": {}, "transfers": []}
        self._lock = threading.Lock()
        self._transfer_lock = threading.Lock()
        self._lockfh = None
        self._limit_kb = int(config.get("bandwidth_limit_kb", 0) or 0)
        self.limiter = TokenBucket(self._limit_kb * 1024) \
            if self._limit_kb > 0 else None
        self._pending = set()           # pair ids requested via sync_now()
        # systemd watchdog state: a liveness stamp the pass loop, the idle
        # wait and every transfer chunk keep fresh while things move
        self.heartbeat = time.monotonic()
        self._watchdog_sec = 120.0
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = None
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
        self._start_watchdog()

    def stop(self):
        self._watchdog_stop.set()
        self.stop_flag.set()
        self.wake.set()
        self._thread.join(timeout=10)
        if os.environ.get("NOTIFY_SOCKET"):
            sd_notify("STOPPING=1")

    def _touch_heartbeat(self):
        """Mark the worker as alive for the systemd watchdog."""
        self.heartbeat = time.monotonic()

    def _start_watchdog(self):
        """Systemd watchdog: READY=1 now, WATCHDOG=1 while the worker moves.

        Active only when NOTIFY_SOCKET is set (running under systemd with
        WatchdogSec= in the unit). The pinger deliberately stops pinging —
        which makes systemd restart the service — when the heartbeat goes
        stale for 3/4 of the watchdog window: neither the pass loop, the
        idle wait, nor any transfer chunk has shown liveness for that long,
        so a hung worker is replaced instead of stalling forever.
        """
        if not os.environ.get("NOTIFY_SOCKET"):
            return
        try:
            sec = float(os.environ.get("WATCHDOG_USEC", "0")) / 1_000_000
        except ValueError:
            sec = 0.0
        self._watchdog_sec = sec if sec > 0 else 120.0
        sd_notify("READY=1")
        self._touch_heartbeat()

        def ping():
            while not self._watchdog_stop.wait(
                    timeout=max(1.0, self._watchdog_sec / 4)):
                if time.monotonic() - self.heartbeat \
                        < self._watchdog_sec * 0.75:
                    sd_notify("WATCHDOG=1")

        self._watchdog_thread = threading.Thread(
            target=ping, daemon=True, name="drivesync-watchdog")
        self._watchdog_thread.start()

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
        drive = Drive(
            lambda: access_token_persisting(self.store, account_row["id"],
                                            token),
            limiter=self.limiter)
        # transfer chunk loops keep the watchdog fed while bytes move, so a
        # slow but healthy upload is never mistaken for a hung worker
        drive.heartbeat = self._touch_heartbeat
        return drive

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
        self._touch_heartbeat()
        try:
            pairs = self.store.pairs(enabled_only=True)
            if pair_id:
                wanted = {pair_id} if isinstance(pair_id, int) else set(pair_id)
                pairs = [p for p in pairs if p["id"] in wanted]
            pairs = self._drop_paused_pairs(pairs)
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
                    self._touch_heartbeat()
                    stale = bool(self.store.kv_get(f"stale:{pair['id']}"))
                    try:
                        stats, ok = engine.sync_pair(
                            self.store, self.config, pair, drive,
                            force_full=force_full,
                            changes=None if stale else feed,
                            transfer_callback=self._on_transfer)
                    except Exception as exc:  # keep the worker alive no matter what
                        self.status["last_error"] = str(exc)
                        self.store.log(pair["id"], "worker-error", "",
                                       str(exc), level="error")
                        self.store.kv_set(f"stale:{pair['id']}", "1")
                        self._note_pair(pair["id"], False, None, str(exc))
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
                    self._note_pair(pair["id"], bool(ok), stats,
                                    None if ok else self._pair_error(pair["id"]))
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
        if overall_ok:
            self._maybe_backup_db()
        return total, overall_ok

    def _drop_paused_pairs(self, pairs):
        """Apply the per-pair 'pause until' schedule.

        A pair paused into the future is skipped (silently — a log row per
        pass would just spam the activity feed). An expired pause is cleared
        and announced once, so the pair is back on every later pass.
        """
        active = []
        for p in pairs:
            until = p["paused_until"]
            if not until:
                active.append(p)
                continue
            try:
                expiry = datetime.fromisoformat(str(until))
            except ValueError:
                self.store.update_pair(p["id"], paused_until=None)
                active.append(p)
                continue
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= expiry:
                self.store.update_pair(p["id"], paused_until=None)
                self.store.log(p["id"], "auto-resume", "",
                               f"pause expired ({until}) — pair resumed "
                               "automatically")
                active.append(p)
            # else: still inside the pause window — leave it out
        return active

    def _maybe_backup_db(self):
        """Snapshot state.db after a clean pass (rate-limited to 10 min).

        Store falls back to state.db.bak when the live database turns out
        to be corrupt, so the fresher the backup, the smaller the loss.
        """
        try:
            last = self.store.kv_get("last_db_backup")
            if last and time.time() - float(last) < 600:
                return
            if self.store.backup():
                self.store.kv_set("last_db_backup", str(time.time()))
        except Exception:
            pass        # a failed backup must never fail the pass

    def _on_transfer(self, item):
        """Publish a bounded, thread-safe list of queued and recent transfers.

        The engine reports lifecycle changes from its transfer threads. Keep
        the newest 50 records so the GTK and web queues can render the same
        state without a second queue or database write path.
        """
        with self._transfer_lock:
            rows = list(self.status.get("transfers") or [])
            for index, current in enumerate(rows):
                if current.get("id") == item.get("id"):
                    rows[index] = {**current, **item}
                    break
            else:
                rows.append(dict(item))
            self.status["transfers"] = rows[-50:]

    def _note_pair(self, pair_id, ok, stats, error):
        """Record one pair's last-pass outcome for the UI status columns.

        Kept inside status["pairs"] so the GUI and the dashboard can show
        "last sync 2m ago / ERROR: …" per pair without parsing the event
        log. Assignments replace the whole per-pair dict, so readers never
        see a half-updated row.
        """
        self.status["pairs"][pair_id] = {
            "ts": time.time(),
            "ok": bool(ok),
            "stats": ({
                "pushed": int(stats.pushed), "pulled": int(stats.pulled),
                "deleted": int(stats.deleted), "renamed": int(stats.renamed),
                "conflicts": int(stats.conflicts),
            } if stats is not None else None),
            "error": error,
        }

    def _pair_error(self, pair_id):
        """Message of the pair's newest error event (sync_pair logs there)."""
        try:
            rows = self.store.recent_events(limit=1, level="error",
                                            pair_id=pair_id)
            return rows[0]["message"] if rows else "sync failed"
        except Exception:
            return "sync failed"

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
    def _wait_wake(self):
        """wake.wait() in watchdog-sized slices.

        An idle worker must keep telling systemd it is alive even when the
        sync interval is hours long, so the wait is split into slices no
        longer than a quarter of the watchdog window, refreshing the
        heartbeat between them. Behavior without systemd is unchanged.
        """
        deadline = time.time() + self.interval
        while not self.stop_flag.is_set():
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            slice_ = min(remaining, max(1.0, self._watchdog_sec / 4))
            if self.wake.wait(timeout=slice_):
                return
            self._touch_heartbeat()

    def _loop(self):
        while not self.stop_flag.is_set():
            if self._paused.is_set():
                self.status["next_run"] = None
            else:
                self.status["next_run"] = time.time() + self.interval
            self._wait_wake()
            self.wake.clear()
            if self.stop_flag.is_set():
                break
            if self._paused.is_set():
                self.status["next_run"] = None
                continue
            ids = list(self._pending)
            self._pending.clear()
            if not in_window(datetime.now().time(),
                             self.config.get("active_from", ""),
                             self.config.get("active_to", "")):
                self.status["waiting_until"] = \
                    self.config.get("active_to") \
                    or self.config.get("active_from") or None
                self.status["next_run"] = None
                continue
            self.status["waiting_until"] = None
            self.status["next_run"] = None      # a pass is starting now
            self.run_pass(ids or None)
