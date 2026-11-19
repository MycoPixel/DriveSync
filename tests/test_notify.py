"""Desktop notification logic (no display needed)."""

import sys

import drivesync.notify as notify


def _row(level="info", ts="2026-11-18T10:00:00+00:00", action="upload",
         rel="a.txt"):
    return {"level": level, "ts": ts, "action": action, "rel_path": rel}


def test_problems_filters_levels():
    rows = [_row(), _row("conflict"), _row("error"), _row("info")]
    assert [r["level"] for r in notify.problems(rows)] == ["conflict", "error"]


def test_gate_notifies_hidden_only_once_per_cooldown():
    now = [100.0]
    g = notify.Gate(visible=lambda: False, cooldown=300.0,
                    clock=lambda: now[0])
    rows = [_row("conflict", "2026-11-18T10:00:05+00:00")]
    body = g.consider(rows, rows[-1]["ts"])
    assert body and "upload" in body          # body names the first problem
    # a newer problem within the cooldown stays silent
    rows2 = [_row("error", "2026-11-18T10:01:00+00:00")]
    assert g.consider(rows2, rows2[-1]["ts"]) is None
    # after the cooldown it notifies again
    now[0] += 301.0
    assert g.consider(rows2, rows2[-1]["ts"])


def test_gate_marks_seen_when_window_visible():
    now = [100.0]
    vis = [True]
    g = notify.Gate(visible=lambda: vis[0], cooldown=300.0,
                    clock=lambda: now[0])
    rows = [_row("error", "2026-11-18T10:00:05+00:00")]
    assert g.consider(rows, rows[-1]["ts"]) is None      # visible: silent
    vis[0] = False
    assert g.consider(rows, rows[-1]["ts"]) is None      # already seen


def test_gate_quiet_without_problems():
    g = notify.Gate(visible=lambda: False, clock=lambda: 1.0)
    assert g.consider([], None) is None


def test_send_headless_is_false(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)
    assert notify.send("t", "b") is False


def test_send_initialises_notify_once(monkeypatch):
    import types

    calls = {"init": 0, "uninit": 0, "show": 0}
    fake_gi = types.ModuleType("gi")
    fake_gi.require_version = lambda *a: None
    repo = types.ModuleType("gi.repository")

    class Notify:
        @staticmethod
        def init(app):
            calls["init"] += 1
            return True

        @staticmethod
        def uninit():
            calls["uninit"] += 1

        class Notification:
            def __init__(self, *a):
                pass

            @staticmethod
            def new(*a):
                return Notify.Notification()

            def show(self):
                calls["show"] += 1
                return True

    repo.Notify = Notify
    fake_gi.repository = repo
    monkeypatch.setitem(sys.modules, "gi", fake_gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repo)
    monkeypatch.setattr(notify, "_initialized", False)

    assert notify.send("t", "b") is True
    assert notify.send("t2", "b2") is True
    assert calls == {"init": 1, "uninit": 0, "show": 2}
