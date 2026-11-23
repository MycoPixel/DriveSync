"""Tests for the 'drivesync gui' subcommand (environment-independent)."""

import os
import sys

import pytest

from drivesync.cli import build_parser, main


def test_gui_subcommand_exists():
    args = build_parser().parse_args(["gui"])
    assert args.cmd == "gui"


def test_gui_missing_gtk_exits_with_hint(monkeypatch, capsys):
    # sys.modules[name] = None makes `from .gui import run` raise ImportError
    # even on machines that do have PyGObject installed.
    monkeypatch.setitem(sys.modules, "drivesync.gui", None)
    with pytest.raises(SystemExit) as exc:
        main(["gui"])
    assert "detail:" in str(exc.value.code)
    out = capsys.readouterr().out
    assert "python3-gobject" in out


def test_activity_rows_fit_liststore_columns(tmp_path):
    """Regression for 'TypeError: Must be number, not str' at 'drivesync
    gui' startup: the hidden activity column was declared float while the
    code appended the raw ts STRING. Rebuild the exact ListStore and append
    real rows through _activity_row — append raises on any type mismatch.
    Needs PyGObject (skipped on headless machines without it)."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    from drivesync import gui as gui_mod
    from drivesync.db import Store

    store = Store(tmp_path / "state.db")
    store.add_account("a@x.com", {})
    pair = store.add_pair(1, "/tmp/x", "root123")
    store.log(pair["id"], "upload", "a.txt", "copied")
    store.log(pair["id"], "conflict", "b.txt", "both changed",
              level="conflict")
    store.log(pair["id"], "test", None, None)      # NULLs are coerced

    view = Gtk.ListStore(str, str, str, str, str, str)
    for e in store.recent_events(limit=200):
        row = gui_mod._activity_row(e)
        assert len(row) == view.get_n_columns()
        assert all(isinstance(v, str) for v in row)
        view.append(row)               # the exact call that used to crash
    assert len(view) == 3
    assert view[0][5]                  # hidden column holds the raw ts
    store.close()


def test_pair_last_sync_text_formats_status(tmp_path):
    """The Folders tab 'Last sync' cell: ok/error/never, relative time."""
    pytest.importorskip("gi")               # gui module imports gi at load

    from drivesync import gui as gui_mod
    import time as _time

    class StubWorker:
        status = {"pairs": {1: {"ts": _time.time() - 95, "ok": True,
                                "stats": {"pushed": 2, "pulled": 0,
                                          "deleted": 0, "renamed": 0,
                                          "conflicts": 1},
                                "error": None},
                            2: {"ts": _time.time() - 10, "ok": False,
                                "stats": None, "error": "disk full"},
                            3: {"ts": _time.time() - 7200, "ok": True,
                                "stats": {"pushed": 0, "pulled": 0,
                                          "deleted": 0, "renamed": 0,
                                          "conflicts": 0},
                                "error": None}}}

    app = type("Stub", (), {"worker": StubWorker()})()
    text_1 = gui_mod.DesktopApp._pair_last_sync_text(app, 1)
    assert text_1.startswith("ok ") and "95s ago" in text_1
    assert "conflict" in text_1
    text_2 = gui_mod.DesktopApp._pair_last_sync_text(app, 2)
    assert text_2.startswith("ERROR") and "10s ago" in text_2
    text_3 = gui_mod.DesktopApp._pair_last_sync_text(app, 3)
    assert text_3.startswith("ok ") and "up to date" in text_3
    assert gui_mod.DesktopApp._pair_last_sync_text(app, 99) == "never"
    assert gui_mod.DesktopApp._pair_last_sync_text(app, None) == "never"


def test_dashboard_html_defines_every_js_selector():
    """Every $('#id') referenced by app.js must exist in index.html.

    Regression guard for UI wiring typos (an earlier edit produced
    '$("$scopeList")', which silently broke the scope modal).
    """
    import os
    import re

    import drivesync

    static = os.path.join(os.path.dirname(drivesync.__file__), "static")
    html = open(os.path.join(static, "index.html"),
                encoding="utf-8").read()
    js = open(os.path.join(static, "app.js"), encoding="utf-8").read()
    ids = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', js))
    assert ids, "selector scan found nothing — regex or file moved?"
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert not missing, \
        f"app.js references ids missing from index.html: {missing}"

def test_startup_page_prioritizes_setup_then_accounts_then_overview():
    pytest.importorskip("gi")
    from drivesync.gui import startup_page

    assert startup_page({}, []) == 1
    assert startup_page({"client_id": "configured"}, []) == 2
    assert startup_page({"client_id": "configured"}, [{"email": "a@example.com"}]) == 0


def test_crash_log_is_bounded(tmp_path, monkeypatch):
    """A crash loop must not grow gui-crash.log without limit."""
    pytest.importorskip("gi")
    from drivesync import gui as gui_mod

    log = tmp_path / "gui-crash.log"
    log.write_bytes(b"x" * 4000)
    monkeypatch.setattr(gui_mod, "_CRASH_LOG_MAX", 1000)
    out = gui_mod._write_crash_log("latest traceback\n", log)
    assert out == log
    data = log.read_bytes()
    assert len(data) <= 1000 + 200          # kept tail + new block
    assert b"latest traceback" in data
    assert b"truncated" in data


def test_desktop_window_builds_under_a_display(tmp_path):
    """Smoke-test the real window under Xvfb (CI's gui-smoke job).

    Runs in a subprocess: an earlier test can leave a pystray GTK thread
    alive, and building a second GTK window on the main thread next to it
    crashes libgtk. Everything that talks to the outside world (worker,
    tray, single-instance socket) is stubbed; the point is that every page
    builds and Options exposes its four sub-tabs.
    """
    pytest.importorskip("gi")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("no display (run under xvfb-run)")

    import subprocess
    import textwrap
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent("""
        from drivesync import gui

        RealWorker = gui.Worker

        class FakeWorker(RealWorker):
            def start(self):
                pass

            def stop(self):
                pass

        class FakeListener:
            def __init__(self, callback, on_add_pair=None):
                pass

            def start(self):
                return False

            def stop(self):
                pass

        gui.Worker = FakeWorker
        gui.instance.ShowListener = FakeListener
        gui.DesktopApp._start_tray = lambda self: None
        gui.GLib.timeout_add_seconds = lambda *a, **k: 0

        app = gui.DesktopApp()
        app.show()
        assert app.nb.get_n_pages() == 8, app.nb.get_n_pages()

        from gi.repository import Gtk

        def find_notebook(widget):
            if isinstance(widget, Gtk.Notebook):
                return widget
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    found = find_notebook(child)
                    if found is not None:
                        return found
            return None

        options = app.nb.get_nth_page(gui.PAGE_OPTIONS)
        sub = find_notebook(options)
        assert sub is not None and sub.get_n_pages() == 4
        assert app._startup_page in range(app.nb.get_n_pages())
        app.win.destroy()
        print("window ok")
    """)
    env = dict(os.environ, DRIVE_SYNC_HOME=str(tmp_path),
               PYTHONPATH=str(root))
    proc = subprocess.run([sys.executable, "-c", script], cwd=root, env=env,
                          capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "window ok" in proc.stdout


def test_tall_pages_scroll_instead_of_growing_the_window():
    """Regression: the Options page used to report its full content height
    as the window minimum, opening taller than a laptop screen. Tall pages
    are wrapped in a vertical scroller so the window can always shrink."""
    pytest.importorskip("gi")
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    from drivesync.gui import _scrollable

    scroll = _scrollable(Gtk.Label(label="tall page"))
    assert isinstance(scroll, Gtk.ScrolledWindow)
    policy = scroll.get_policy()
    assert policy.vscrollbar_policy == Gtk.PolicyType.AUTOMATIC
