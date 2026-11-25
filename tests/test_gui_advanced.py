"""Options > Sync > Advanced sync (desktop) writes the same settings the web
page does. Real window under Xvfb, in a subprocess (see
test_desktop_window_builds_under_a_display for why)."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = textwrap.dedent("""
    import json
    from drivesync import gui, i18n
    from drivesync.config import load_config, update_config

    # the window rewrites its own state (last_tab, geometry) while building;
    # that is not a setting
    STATE = {"last_tab", "window_width", "window_height", "window_maximized"}

    def settings():
        return {k: v for k, v in load_config().items() if k not in STATE}

    # a config as an older version / a hand edit would leave it
    update_config(transfer_concurrency=7, history_versions=7,
                  delete_safety_limit=42, conflict_policy="both",
                  watch_enabled=False, incremental_scan=False,
                  convert_google_docs=False, ignore_patterns=["a*", "b/"],
                  resumable_threshold=1024)
    before = settings()

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
    errors = []
    app._error = lambda msg: errors.append(msg)

    # 1. the widgets show what is stored, and building them wrote nothing
    assert app.sp_conc.get_value() == 7
    assert app.sp_hist.get_value() == 7
    assert app.sp_dellimit.get_value() == 42
    assert gui.POLICIES[app.cmb_policy.get_active()] == "both"
    assert not app.chk_watch.get_active()
    assert not app.chk_incremental.get_active()
    assert not app.chk_convert.get_active()
    buf = app.txt_ignores.get_buffer()
    assert buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False) \\
        == "a*\\nb/"
    assert settings() == before, "building the UI changed a setting"

    # 2. every control writes its setting
    app.sp_conc.set_value(9)
    assert load_config()["transfer_concurrency"] == 9
    app.sp_hist.set_value(3)
    assert load_config()["history_versions"] == 3
    app.sp_dellimit.set_value(0)
    assert load_config()["delete_safety_limit"] == 0
    app.cmb_policy.set_active(2)
    assert load_config()["conflict_policy"] == "remote"
    app.chk_watch.set_active(True)
    app.chk_incremental.set_active(True)
    app.chk_convert.set_active(True)
    cfg = load_config()
    assert cfg["watch_enabled"] and cfg["incremental_scan"] \\
        and cfg["convert_google_docs"]

    # 3. ignore patterns save on focus-out; blanks and padding are dropped
    from gi.repository import Gdk
    buf.set_text("*.log\\n\\n   build/  \\n.cache")
    assert load_config()["ignore_patterns"] == ["a*", "b/"]   # not per key
    app.txt_ignores.emit("focus-out-event",
                         Gdk.Event.new(Gdk.EventType.FOCUS_CHANGE))
    assert load_config()["ignore_patterns"] == ["*.log", "build/", ".cache"]

    # 4. switching language relabels the combo but keeps and re-saves nothing
    snapshot = settings()
    i18n.set_language("es")
    app._retranslate()
    assert app.cmb_policy.get_active() == 2
    assert app.cmb_policy.get_active_text() == "Conservar la copia en Drive"
    assert load_config()["conflict_policy"] == "remote"
    assert settings() == snapshot, "language switch changed a setting"
    i18n.set_language("en")
    app._retranslate()
    assert app.cmb_policy.get_active_text() == "Keep remote copy"

    # 5. Transfers section: now persists, and an unrelated edit does not
    #    round a hand-edited sub-MiB threshold to 0
    app.sp_retries.set_value(5)
    cfg = load_config()
    assert cfg["transfer_retries"] == 5
    assert cfg["resumable_threshold"] == 1024, cfg["resumable_threshold"]
    app.sp_threshold.set_value(4)
    assert load_config()["resumable_threshold"] == 4 << 20
    app.sp_backoff.set_value(7.5)
    assert load_config()["transfer_retry_base"] == 7.5

    assert not errors, errors
    app.win.destroy()
    print("advanced ok")
""")


def test_advanced_sync_options_in_the_desktop_window(tmp_path):
    pytest.importorskip("gi")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("no display (run under xvfb-run)")
    env = dict(os.environ, DRIVE_SYNC_HOME=str(tmp_path),
               PYTHONPATH=str(ROOT))
    proc = subprocess.run([sys.executable, "-c", SCRIPT], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "advanced ok" in proc.stdout
