"""Watcher helpers: debounce, ignore matching, event routing, start()."""

import sys
import time

from drivesync.watch import Debouncer, ignored, consider_event, start


def test_ignored_matches_any_component():
    pats = ["*.tmp", "ignoreme/*"]
    assert ignored("notes.tmp", pats)
    assert ignored("sub/notes.tmp", pats)
    assert ignored("ignoreme/x.bin", pats)
    assert not ignored("sub/notes.txt", pats)


def test_debouncer_coalesces_bursts():
    fired = []
    d = Debouncer(delay=0.05, max_wait=10.0, fire=fired.append)
    for _ in range(5):
        d.poke("k")
        time.sleep(0.01)
    time.sleep(0.2)
    assert fired == ["k"]
    d.close()


def test_debouncer_max_wait_fires_during_burst():
    fired = []
    d = Debouncer(delay=0.5, max_wait=0.2, fire=fired.append)
    for _ in range(12):                  # 12 x 30 ms = 360 ms of poking
        d.poke("k")
        time.sleep(0.03)
    assert fired == ["k"]                # fired mid-burst, not after it
    d.close()


def test_debouncer_close_prevents_fires():
    fired = []
    d = Debouncer(delay=0.02, max_wait=5.0, fire=fired.append)
    d.poke("k")
    d.close()
    time.sleep(0.1)
    assert fired == []


def test_consider_event_routes_and_filters(tmp_path):
    fired = []
    d = Debouncer(delay=0.05, max_wait=10.0, fire=fired.append)
    root = str(tmp_path)
    assert consider_event(d, root, str(tmp_path / "a.txt"), [], 7)
    time.sleep(0.15)
    assert fired == [7]
    (tmp_path / "x.tmp").write_text("")
    assert not consider_event(d, root, root, [], 7)        # the root itself
    assert not consider_event(d, root, str(tmp_path / "x.tmp"),
                              ["*.tmp"], 7)
    time.sleep(0.1)
    assert fired == [7]                                     # nothing extra
    d.close()


def test_start_disabled():
    h, msg = start(None, None, {"watch_enabled": False})
    assert h is None and "disabled" in msg


def test_start_without_watchdog_degrades(monkeypatch):
    monkeypatch.setitem(sys.modules, "watchdog", None)
    h, msg = start(None, None, {"watch_enabled": True})
    assert h is None and "watchdog" in msg


def test_builtin_ignores_skip_vcs_and_build_noise(tmp_path):
    fired = []
    d = Debouncer(delay=0.02, max_wait=10.0, fire=fired.append)
    root = str(tmp_path)
    for rel in (".git/config", "sub/.git/HEAD", "node_modules/x/index.js",
                "__pycache__/mod.pyc", "venv/.venv/pyvenv.cfg"):
        assert not consider_event(d, root, str(tmp_path / rel), [], 1), rel
    assert consider_event(d, root, str(tmp_path / "notes.txt"), [], 1)
    time.sleep(0.1)
    assert fired == [1]
    d.close()


def test_events_inside_the_data_directory_are_ignored(
        tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "app"))
    fired = []
    d = Debouncer(delay=0.02, max_wait=10.0, fire=fired.append)
    root = str(tmp_path)
    assert not consider_event(
        d, root, str(tmp_path / "app" / "state.db"), [], 1)
    assert not consider_event(
        d, root, str(tmp_path / "app" / "history" / "x"), [], 1)
    time.sleep(0.1)
    assert fired == []
    d.close()
