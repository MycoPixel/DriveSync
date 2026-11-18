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
