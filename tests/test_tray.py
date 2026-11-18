"""Tray icon start must tolerate any pystray version.

Old pystray (< 0.17) has no Icon.run_detached; tray.start must fall back
to running the blocking run() in a daemon thread and still report success.
"""

import sys
import time
import types

import drivesync.tray as tray


class _FakeIcon:
    """Simulates old pystray: only a blocking run()."""

    def __init__(self):
        self.ran = False
        self.ran_detached = False
        self.detached_ok = True

    def run(self):
        self.ran = True


class _FakeIconDetached(_FakeIcon):
    """Simulates pystray >= 0.17 with run_detached support."""

    def run_detached(self):
        self.ran_detached = True
        return self.detached_ok


class _BoomIcon(_FakeIconDetached):
    """Simulates a backend that refuses to start at all."""

    def run_detached(self):
        raise RuntimeError("no backend")

    def run(self):
        raise RuntimeError("no backend")


def _fake_pystray(monkeypatch, icon, version="0.16.0",
                  backend="pystray._appindicator"):
    mod = types.ModuleType("pystray")
    mod.__version__ = version
    mod.__file__ = "/fake/site-packages/pystray/__init__.py"
    mod.Icon = lambda *a, **k: icon
    # tray detects the backend from type(icon).__module__ (e.g. the real
    # pystray reports pystray._appindicator / pystray._gtk)
    monkeypatch.setattr(type(icon), "__module__", backend)

    class Menu:
        SEPARATOR = object()

        def __init__(self, *items):
            pass

    class MenuItem:
        def __init__(self, *a, **k):
            pass

    mod.Menu, mod.MenuItem = Menu, MenuItem
    monkeypatch.setitem(sys.modules, "pystray", mod)

    pil = types.ModuleType("PIL")
    draw_mod = types.ModuleType("PIL.ImageDraw")
    image_mod = types.ModuleType("PIL.Image")

    class _Drawer:
        def rounded_rectangle(self, *a, **k):
            pass

        def polygon(self, *a, **k):
            pass

        def rectangle(self, *a, **k):
            pass

    # tray does `from PIL import Image, ImageDraw`, so the attributes must
    # live directly on the fake submodules
    image_mod.new = lambda *a, **k: object()
    draw_mod.Draw = lambda img: _Drawer()
    pil.Image, pil.ImageDraw = image_mod, draw_mod
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", image_mod)
    monkeypatch.setitem(sys.modules, "PIL.ImageDraw", draw_mod)


def _wait_ran(icon, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if icon.ran or icon.ran_detached:
            return True
        time.sleep(0.02)
    return False


def test_old_pystray_without_run_detached_starts(monkeypatch):
    icon = _FakeIcon()
    _fake_pystray(monkeypatch, icon, version="0.16.0")
    started, msg = tray.start(None, "http://127.0.0.1:5678")
    assert started, msg
    assert _wait_ran(icon) and icon.ran and not icon.ran_detached


def test_detached_unsupported_falls_back_to_thread(monkeypatch):
    icon = _FakeIconDetached()
    icon.detached_ok = False  # backend refuses run_detached
    _fake_pystray(monkeypatch, icon, version="0.19.2")
    started, msg = tray.start(None, "http://127.0.0.1:5678")
    assert started, msg
    assert _wait_ran(icon) and icon.ran


def test_new_pystray_uses_run_detached(monkeypatch):
    icon = _FakeIconDetached()
    _fake_pystray(monkeypatch, icon, version="0.19.2")
    started, msg = tray.start(None, "http://127.0.0.1:5678")
    assert started, msg
    assert _wait_ran(icon) and icon.ran_detached and not icon.ran


def test_broken_backend_reports_version_and_path(monkeypatch):
    icon = _BoomIcon()
    _fake_pystray(monkeypatch, icon, version="9.9.9-fake")
    started, msg = tray.start(None, "http://127.0.0.1:5678")
    assert not started
    assert "9.9.9-fake" in msg and "/fake/site-packages/pystray" in msg


def test_gtk_backend_message_carries_kde_hint(monkeypatch):
    icon = _FakeIconDetached()
    _fake_pystray(monkeypatch, icon, version="0.19.2", backend="pystray._gtk")
    started, msg = tray.start(None, "http://127.0.0.1:5678")
    assert started, msg
    assert "libayatana-appindicator-gtk3" in msg


def test_appindicator_message_has_no_hint(monkeypatch):
    icon = _FakeIconDetached()
    _fake_pystray(monkeypatch, icon, version="0.19.2",
                  backend="pystray._appindicator")
    started, msg = tray.start(None, "http://127.0.0.1:5678")
    assert started, msg
    assert msg == "tray icon running"
