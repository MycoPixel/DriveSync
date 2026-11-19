"""Desktop notifications for sync problems (libnotify via GObject Introspection).

Pure logic (problem filtering + the notification gate) lives here so it is
testable without a display; only send()/available() touch libnotify, and
they are best-effort — no session bus or missing GIR simply disables
notifications, like the tray icon.
"""

import time

COOLDOWN_SECS = 300.0      # at most one notification every 5 minutes
_initialized = False       # Notify.init() once per process


def available():
    try:
        import gi
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify  # noqa: F401
        return True
    except (ImportError, ValueError):
        return False


def send(title, body):
    """Show a desktop notification. Returns True when actually shown."""
    global _initialized
    try:
        import gi
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify
        # Notify.uninit() after every message invalidated the bus connection;
        # initialise once and let the process keep it for its lifetime
        if not _initialized:
            if not Notify.init("DriveSync"):
                return False
            _initialized = True
        n = Notify.Notification.new(title, body, "drivesync")
        return bool(n.show())
    except Exception:
        return False


def problems(rows):
    """The error/conflict subset of newest-first activity rows."""
    return [r for r in rows if r["level"] in ("error", "conflict")]


class Gate:
    """Decides when new problems deserve a desktop notification.

    Rules:
    - problems seen while the main window is open are never announced
      later (the user was looking at the Activity tab);
    - while the window is hidden, new problems notify at most once per
      cooldown, and only rows newer than the last handled one;
    - a hidden window with no new problems stays silent.
    """

    def __init__(self, visible, cooldown=COOLDOWN_SECS, clock=time.monotonic):
        self._visible = visible          # callable -> bool
        self._cooldown = cooldown
        self._clock = clock
        self._last_ts = None             # newest ts handled so far
        self._last_sent = None           # monotonic time of last notification

    def consider(self, problem_rows, newest_ts):
        """Return a notification body, or None to stay quiet."""
        if not problem_rows:
            self._last_ts = newest_ts or self._last_ts
            return None
        newest = newest_ts or problem_rows[0]["ts"]
        if self._visible():
            self._last_ts = newest       # user is right there; mark seen
            return None
        if self._last_ts and newest <= self._last_ts:
            return None                  # nothing genuinely new
        now = self._clock()
        if self._last_sent is not None \
                and now - self._last_sent < self._cooldown:
            return None
        self._last_ts = newest
        self._last_sent = now
        first = problem_rows[0]
        body = f"{first['action']} {first['rel_path']}".strip() or "sync problem"
        if len(problem_rows) > 1:
            body += f" (+{len(problem_rows) - 1} more)"
        return body
