"""Background worker: syncs every enabled pair on an interval, on demand."""

import os
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone

try:
    import fcntl  # Unix only: cross-process sync lock
except ImportError:  # pragma: no cover
    fcntl = None

from . import engine, netstate
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
        # per-pair QoS (Phase 4.2): pair id -> (resolved kb, TokenBucket);
        # must exist before the init-time _update_limiter() below
        self._pair_buckets = {}
        self._limit_kb = int(config.get("bandwidth_limit_kb", 0) or 0)
        self.limiter = TokenBucket(self._limit_kb * 1024) \
            if self._limit_kb > 0 else None
        # peak/off-peak QoS (Phase 4.1): a different cap applies inside the
        # optional peak window; the swap happens in _update_limiter, which
        # every loop tick re-evaluates so the boundary needs no restart
        self.status["peak_active"] = False
        self.status["bandwidth_now_kb"] = self._limit_kb
        # metered-network awareness (Phase 4.3): unknown detection fails
        # open — sync runs; the UI mirrors the last detection result
        self.status["network"] = {"metered": None, "source": "",
                                  "checked_at": 0.0}
        # notifications digest (Phase 4.4): last sent message + schedule
        self.status["digest"] = {"last_sent_at": None, "last_message": None,
                                 "skipped": None, "error": None}
        self._metered_pause_logged = False
        self._update_limiter()
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
        # the swap decision compares the OLD rate with the NEW effective
        # rate, so do not pre-assign _limit_kb here — _update_limiter does
        self._update_limiter()

    def _base_effective_kb(self, pair=None):
        """KB/s the 4.1/4.2 ladder resolves right now (globally or per pair).

        Global behaviour (pair=None) is unchanged from Phase 4.1. With a
        pair, its overrides nest over the global rate: a pair bandwidth
        cap always wins; a pair peak window switches to the pair peak cap
        when set (0 = fall back to the pair general cap, then global);
        NULL/empty everywhere inherits the global rate. Resolution is
        re-computed per pass, so edited pairs need no restart.
        """
        base = int(self.config.get("bandwidth_limit_kb", 0) or 0)
        gpf = str(self.config.get("peak_from") or "").strip()
        gpt = str(self.config.get("peak_to") or "").strip()
        now_t = datetime.now().time()
        if pair is None:
            if gpf and gpt and in_window(now_t, gpf, gpt):
                peak = int(self.config.get("bandwidth_limit_kb_peak", 0)
                           or 0)
                return peak or base
            return base

        # the global rate as it stands right now (peak-window aware) is
        # the fallback at every level of the per-pair ladder
        global_now = self._base_effective_kb()
        p_bw = pair["bandwidth_limit_kb"]
        p_pf = str(pair["peak_from"] or "").strip()
        p_pt = str(pair["peak_to"] or "").strip()
        p_peak = pair["bandwidth_limit_kb_peak"]
        if p_pf and p_pt and in_window(now_t, p_pf, p_pt) \
                and p_peak is not None:
            return p_peak or (global_now if p_bw is None else p_bw)
        if p_bw is not None:
            return p_bw
        return global_now

    def _effective_limit_kb(self, pair=None):
        """KB/s the limiter should run at right now, metered-aware.

        The 4.1/4.2 ladder (global peak window, per-pair overrides) is
        the base; while the connection is metered and metered_action is
        'throttle', the metered cap is a hard ceiling on every rate —
        including unlimited (0 becomes the cap) and pair overrides (a
        hotspot must never lift a cap the user set for it).
        """
        kb = self._base_effective_kb(pair)
        cap = self._metered_throttle_kb()
        if cap is None:
            return kb
        return cap if kb == 0 else min(kb, cap)

    def _metered_throttle_kb(self):
        """The configured metered cap while throttling applies, or None."""
        if not self.config.get("respect_metered", True):
            return None
        if self.config.get("metered_action", "pause") != "throttle":
            return None
        metered, _src = self._metered_state()
        if not metered:
            return None
        cap = int(self.config.get("metered_bandwidth_kb", 0) or 0)
        return cap or None          # 0 = no cap configured

    def _metered_state(self):
        """(metered, source) with the status mirror for both UIs.

        Detection failures never raise into the pass loop: a broken
        NetworkManager must not stop syncing.
        """
        try:
            metered, source = netstate.is_metered()
        except Exception:
            metered, source = None, "error"
        self.status["network"] = {
            "metered": metered,
            "source": source,
            "checked_at": time.time(),
        }
        return metered, source

    def _metered_blocks_auto(self):
        """True when AUTOMATIC passes should defer (metered + pause).

        Explicit passes — dashboard 'Sync now', the GUI button, CLI
        'drivesync sync' — bypass this gate: they are deliberate user
        intent, and a manual sync on a hotspot is the user's call. A
        single event row is logged on each state transition, not per
        deferred pass.
        """
        if not self.config.get("respect_metered", True):
            return False
        metered, source = self._metered_state()
        if not metered:
            if self._metered_pause_logged:
                self._metered_pause_logged = False
                self.store.log(None, "metered-resume", "",
                               "network no longer metered — automatic "
                               "sync resumes")
            return False
        if self.config.get("metered_action", "pause") != "pause":
            return False
        if not self._metered_pause_logged:
            self._metered_pause_logged = True
            self.store.log(None, "metered-pause", "",
                           f"metered connection detected ({source}) — "
                           "automatic sync is paused; manual syncs still "
                           "run", level="info")
        return True

    # ---- notifications digest (Phase 4.4) -----------------------------
    def _maybe_digest(self):
        """Send the daily 'what synced' digest when due.

        Called every loop tick. Scheduling: at/after digest_time, on the
        first day not yet handled — so a laptop that boots at 19:00 with
        an 18:00 digest catches up on the first tick instead of missing
        the day. digest_time empty means fire on the first tick of each
        new day. The send itself is failure-tolerant: a broken notifier
        records status['digest']['error'] instead of killing the loop.
        """
        if not self.config.get("digest_enabled", False):
            self.status["digest"]["next_at"] = None
            return
        now = datetime.now()
        at = str(self.config.get("digest_time") or "").strip() or "00:00"
        try:
            hh, mm = (int(x) for x in at.split(":"))
        except ValueError:
            hh, mm = 0, 0
        fire = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        today = now.date().isoformat()
        handled = str(self.store.kv_get("last_digest_day") or "") == today
        if handled:
            self.status["digest"]["next_at"] = None   # done for today
            return
        if now < fire:
            self.status["digest"]["next_at"] = fire.timestamp()
            return
        self.status["digest"]["next_at"] = None
        try:
            self._send_digest(int(self.store.kv_get("last_digest_id")
                                  or 0))
        except Exception as exc:            # store/notifier hiccup
            self.status["digest"]["error"] = str(exc)
        finally:
            # the day counts as handled even if the send failed — retrying
            # every tick would spam on a persistent store error. The
            # window bookmark is the current max event id: ids are
            # strictly monotonic, unlike the second-precision timestamps.
            self.store.kv_set("last_digest_day", today)
            self.store.kv_set("last_digest_id",
                              str(self.store.last_event_id()))

    def _send_digest(self, since_id):
        """Aggregate the window, notify, and log one summary row."""
        summary = self.store.digest_summary(since_id)
        empty = not (summary["files"] or summary["conflicts"]
                     or summary["errors"])
        if empty and self.config.get("digest_skip_empty", True):
            self.status["digest"]["skipped"] = True
            return
        self.status["digest"]["skipped"] = False
        self.status["digest"]["last_summary"] = summary
        msg = self._digest_message(summary)
        self.status["digest"]["last_message"] = msg
        self.status["digest"]["last_sent_at"] = time.time()
        # the activity-log row is the durable record — write it BEFORE the
        # best-effort notification so a notifier blow-up cannot lose the
        # digest (row also feeds the next window via the id bookmark)
        self.store.log(None, "digest", "", msg)
        self._notify("DriveSync", msg)

    @staticmethod
    def _digest_message(s):
        """Human one-liner: '12 pushed · 3 pulled · 1 deleted ·
        2 conflicts · 0 errors (across 2 folders)' — zeros shown for
        conflicts/errors (reassuring), omitted buckets stay out."""
        parts = []
        if s["pushed"]:
            parts.append(f'{s["pushed"]} pushed')
        if s["pulled"]:
            parts.append(f'{s["pulled"]} pulled')
        if s["deleted"]:
            parts.append(f'{s["deleted"]} deleted')
        tail = (f'{s["conflicts"]} '
                f'conflict{"s" if s["conflicts"] != 1 else ""} · '
                f'{s["errors"]} '
                f'error{"s" if s["errors"] != 1 else ""}')
        body = " · ".join(parts) + " · " + tail if parts else \
            "no file changes · " + tail
        return body + (f' (across {s["pairs"]} '
                       f'folder{"s" if s["pairs"] != 1 else ""})')

    @staticmethod
    def _notify(title, body):
        """Best-effort desktop notification (notify-send).

        Silently absent on headless hosts or desktops without it — the
        digest still lands in the activity log and the dashboard.
        """
        try:
            subprocess.run(["notify-send", title, body],
                           capture_output=True, timeout=5)
        except Exception:
            pass

    def _update_limiter(self):
        """Swap the token bucket when the effective rate changed.

        Cheap enough to run every loop tick: a rate change builds one new
        bucket (old references in drive.py reads are replaced on the next
        chunk — see worker.limiter attribute access), and the dashboard
        shows the outcome via status.bandwidth_now_kb / peak_active.
        """
        kb = self._effective_limit_kb()
        pf = str(self.config.get("peak_from") or "").strip()
        pt = str(self.config.get("peak_to") or "").strip()
        self.status["peak_active"] = bool(
            pf and pt and in_window(datetime.now().time(), pf, pt))
        self.status["bandwidth_now_kb"] = kb
        if kb != self._limit_kb:
            self._limit_kb = kb
            self.limiter = TokenBucket(kb * 1024) if kb > 0 else None
            self._pair_buckets.clear()   # inherited rates must re-resolve
            return True
        return False

    def _bucket_for_pair(self, pair):
        """(TokenBucket|None, kb) for this pair, cached by resolved rate.

        The pass loop assigns drive.limiter per pair; Drive reads the
        attribute at transfer time, so swapping between pairs of one
        account is safe. A cached bucket survives unchanged rates across
        passes (its token state carries over — intentional: it smooths
        bursts instead of resetting them each pass).
        """
        kb = self._effective_limit_kb(pair)
        cached = self._pair_buckets.get(pair["id"])
        if cached and cached[0] == kb:
            return cached[1], kb
        bucket = TokenBucket(kb * 1024) if kb > 0 else None
        self._pair_buckets[pair["id"]] = (kb, bucket)
        return bucket, kb

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
                    bucket, pair_bw = self._bucket_for_pair(pair)
                    drive.limiter = bucket
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
                        self._note_pair(pair["id"], False, None, str(exc),
                                        bw_kb=pair_bw)
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
                                    None if ok else self._pair_error(pair["id"]),
                                    bw_kb=pair_bw)
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

    def _note_pair(self, pair_id, ok, stats, error, bw_kb=None):
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
            "bandwidth_kb": bw_kb,
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
            self._maybe_digest()   # daily summary: a report, not a pass
            if self._paused.is_set():
                self.status["next_run"] = None
                continue
            ids = list(self._pending)
            self._pending.clear()
            self._update_limiter()      # peak window may start/end here
            if not in_window(datetime.now().time(),
                             self.config.get("active_from", ""),
                             self.config.get("active_to", "")):
                self.status["waiting_until"] = \
                    self.config.get("active_to") \
                    or self.config.get("active_from") or None
                self.status["next_run"] = None
                continue
            self.status["waiting_until"] = None
            if not ids and self._metered_blocks_auto():
                # metered (pause): defer this automatic pass; the next
                # interval tick re-checks, and explicit sync_now requests
                # (ids) are never blocked — deliberate user intent
                self.status["next_run"] = time.time() + self.interval
                continue
            self.status["next_run"] = None      # a pass is starting now
            self.run_pass(ids or None)
