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
