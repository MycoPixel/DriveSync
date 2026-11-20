"""Tests for the 'drivesync gui' subcommand (environment-independent)."""

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
