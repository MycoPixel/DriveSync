"""Single-instance activation channel (KDE pinned-icon restore).

Closing the window to the tray hides it; launching the app again must ask
the resident process to show the window instead of printing "already
running" and exiting.
"""

import threading
import time

from drivesync import instance


def test_show_request_reaches_a_listening_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path))
    called = threading.Event()
    listener = instance.ShowListener(called.set)
    assert listener.start()
    try:
        assert instance.request_show(timeout=2.0)
        assert called.wait(timeout=2.0)
    finally:
        listener.stop()
    assert not instance.socket_path().exists()


def test_request_show_is_false_without_a_listener(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path))
    assert instance.request_show(timeout=0.2) is False
    assert instance.request_add_pair("/tmp/x", timeout=0.2) is False


def test_add_pair_request_carries_the_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path))
    got = []
    listener = instance.ShowListener(lambda: None, got.append)
    assert listener.start()
    try:
        assert instance.request_add_pair("/home/me/My Docs", timeout=2.0)
        deadline = time.monotonic() + 2.0
        while not got and time.monotonic() < deadline:
            time.sleep(0.01)
        assert got == ["/home/me/My Docs"]
    finally:
        listener.stop()


def test_listener_replaces_a_stale_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path))
    path = instance.socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stale")          # not a socket at all

    called = threading.Event()
    listener = instance.ShowListener(called.set)
    assert listener.start()
    try:
        assert instance.request_show(timeout=2.0)
        assert called.wait(timeout=2.0)
    finally:
        listener.stop()
