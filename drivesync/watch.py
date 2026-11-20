"""Instant local-change sync via inotify (watchdog), optional dependency.

`watchdog` watching each enabled pair's local tree; bursts of events are
debounced per pair and trigger worker.sync_now(pair_id) so edits push in
about a second instead of waiting for the scan interval. Missing watchdog
degrades to polling exactly like before (same pattern as the tray icon).

start(worker, store, cfg) -> (watcher | None, message), mirroring tray.start().
"""

import fnmatch
import os
import threading
import time

DEBOUNCE_SECS = 2.0
MAX_WAIT_SECS = 30.0      # a sustained burst still syncs while it runs
REFRESH_SECS = 15.0       # re-scan pair list so adds/removes apply live


def ignored(rel, patterns):
    """True when the path (or any component of it) matches an ignore glob.

    Whole-path globs like 'ignoreme/*' and per-component globs like
    '*.tmp' both work; the watcher uses this to skip junk events cheaply.
    """
    rel = rel.replace("\\", "/")
    parts = rel.split("/")
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat):
            return True
        if any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
    return False


def consider_event(debouncer, root, path, patterns, pair_id):
    """Route one filesystem event to the debouncer. Returns True when the
    event counted (i.e. was not the watched root itself or an ignored
    path) — the caller stops after the first hit."""
    try:
        rel = os.path.relpath(path, root)
    except ValueError:            # different drive/root on Windows-ish setups
        return False
    if rel == ".":
        return False
    if ignored(rel, patterns):
        return False
    debouncer.poke(pair_id)
    return True


class Debouncer:
    """Per-key trailing debounce with a max-wait override.

    poke(key) restarts a key's timer; fire(key) runs after `delay` seconds
    of quiet — or after `max_wait` of continuous poking, so copying a large
    tree still syncs while it runs instead of only when it finishes.
    """

    def __init__(self, delay=DEBOUNCE_SECS, max_wait=MAX_WAIT_SECS, fire=None):
        self.delay = delay
        self.max_wait = max_wait
        self.fire = fire
        self._lock = threading.Lock()
        self._timers = {}
        self._first = {}
        self._closed = False

    def poke(self, key):
        with self._lock:
            if self._closed:
                return
            now = time.monotonic()
            start = self._first.setdefault(key, now)
            old = self._timers.get(key)
            if now - start >= self.max_wait:
                self._timers.pop(key, None)
                self._first.pop(key, None)
                fire_now = True
            else:
                t = threading.Timer(self.delay, self._do_fire, (key,))
                t.daemon = True
                self._timers[key] = t
                fire_now = False
        if old:
            old.cancel()
        if fire_now:
            self._do_fire(key)
        else:
            t.start()

    def _do_fire(self, key):
        with self._lock:
            if self._closed:
                return
            self._timers.pop(key, None)
            self._first.pop(key, None)
        if self.fire:
            self.fire(key)

    def close(self):
        with self._lock:
            self._closed = True
            timers = list(self._timers.values())
            self._timers.clear()
            self._first.clear()
        for t in timers:
            t.cancel()


class Watcher:
    """One recursive observer per enabled pair, refreshed every REFRESH_SECS
    so pairs added/removed/disabled while serve runs apply without restart."""

    def __init__(self, worker, store, cfg):
        from watchdog import events
        from watchdog.observers import Observer

        self._worker = worker
        self._store = store
        self._patterns = list(cfg.get("ignore_patterns", []))
        self._debouncer = Debouncer(fire=worker.sync_now)
        self._observer = Observer(timeout=1.0)
        self._watches = {}          # pair_id -> (root, watch)
        self._stop = threading.Event()
        self._events = events

    def start(self):
        self._observer.start()
        self._refresh()
        threading.Thread(target=self._refresh_loop, daemon=True,
                         name="drivesync-watch-refresh").start()

    def _refresh(self):
        desired = {p["id"]: p["local_path"]
                   for p in self._store.pairs(enabled_only=True)}
        for pid, root in sorted(desired.items()):
            if pid in self._watches:
                continue
            try:
                w = self._observer.schedule(
                    self._make_handler(root, pid), root, recursive=True)
                self._watches[pid] = (root, w)
            except (OSError, FileNotFoundError):
                continue            # path not there yet; the engine logs it
        for pid in list(self._watches):
            if pid not in desired:
                _, w = self._watches.pop(pid)
                try:
                    self._observer.unschedule(w)
                except (KeyError, OSError):
                    pass

    def _make_handler(self, root, pair_id):
        events = self._events
        deb, patterns = self._debouncer, self._patterns

        class _Handler(events.FileSystemEventHandler):
            def on_any_event(self, event):
                for p in (getattr(event, "src_path", "") or "",
                          getattr(event, "dest_path", "") or ""):
                    if p and consider_event(deb, root, p, patterns, pair_id):
                        break

        return _Handler()

    def _refresh_loop(self):
        while not self._stop.wait(REFRESH_SECS):
            try:
                # Adopt ignore-pattern edits without a restart. Handlers keep
                # a reference to this list, so mutate it in place.
                patterns = self._worker.config.get("ignore_patterns", [])
                self._patterns[:] = list(patterns or [])
                self._refresh()
            except Exception:
                pass                # never let the watcher kill serve

    def stop(self):
        self._stop.set()
        self._debouncer.close()
        try:
            self._observer.stop()
            self._observer.join(timeout=3)
        except Exception:
            pass


def start(worker, store, cfg):
    """(watcher | None, message) — best-effort, mirrors tray.start()."""
    if not cfg.get("watch_enabled", True):
        return None, "instant local-change sync disabled (watch_enabled=false)"
    try:
        import watchdog  # noqa: F401
    except Exception as exc:
        return None, (f"instant sync unavailable ({exc.__class__.__name__}) — "
                      "install 'watchdog' (Fedora: python3-watchdog, Debian: "
                      "python3-watchdog) to push local changes immediately; "
                      "the periodic scan still runs")
    try:
        w = Watcher(worker, store, cfg)
        w.start()
        return w, "instant local-change sync active (inotify)"
    except Exception as exc:
        return None, f"instant sync failed to start: {exc}"
