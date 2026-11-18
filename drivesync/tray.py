"""Optional system tray icon.

Uses pystray + Pillow when installed; degrades to a no-op with a printed
hint otherwise, so `drivesync serve` always works headless. The menu covers
the common actions so the dashboard does not need a browser tab open.
"""

import os
import threading
import webbrowser


def start(worker, url):
    """Best-effort tray icon. Returns (started: bool, message: str)."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception as exc:
        return False, (f"tray icon unavailable ({exc.__class__.__name__}) — "
                       "install pystray + pillow to enable it")
    version = getattr(pystray, "__version__", "unknown version")
    try:
        icon = pystray.Icon(
            "DriveSync", _icon_image(Image, ImageDraw),
            "DriveSync — local Drive sync", _menu(pystray, worker, url))
        backend = type(icon).__module__.rsplit(".", 1)[-1]
        # run_detached() needs pystray >= 0.17. Older copies (e.g. a system
        # RPM visible through a --system-site-packages venv) only offer the
        # blocking run(); a daemon thread is the fallback for those.
        if hasattr(icon, "run_detached") and icon.run_detached():
            return True, _tray_msg(backend)
        threading.Thread(target=_run_guarded, args=(icon,), daemon=True,
                         name="drivesync-tray").start()
        return True, _tray_msg(backend)
    except Exception as exc:
        return False, (f"tray icon failed to start: {exc} "
                       f"[pystray {version} from "
                       f"{getattr(pystray, '__file__', '?')}]")


def _run_guarded(icon):
    """Run the blocking pystray loop in a thread.

    Old pystray's GLib backends call signal.signal() in _run(), which only
    works in the main thread — neuter it just for this thread (SIGINT keeps
    Python's default handling, which is what serve wants anyway) and
    restore it afterwards.
    """
    import signal
    original = signal.signal
    signal.signal = lambda *a, **k: None
    try:
        icon.run()
    finally:
        signal.signal = original


def _tray_msg(backend):
    if backend == "_gtk":
        return ("tray icon running — GTK/XEmbed backend; on KDE Plasma "
                "install libayatana-appindicator-gtk3 (and upgrade pystray "
                "in the venv) for a reliable icon")
    return "tray icon running"


def _menu(pystray, worker, url):
    def open_dashboard():
        webbrowser.open(url)

    def sync_now():
        worker.sync_now()

    def pause():
        worker.pause()

    def resume():
        worker.resume()

    def quit_app():
        worker.stop()
        os._exit(0)  # flask dev server has no clean shutdown from a thread

    return pystray.Menu(
        pystray.MenuItem("Open dashboard", open_dashboard, default=True),
        pystray.MenuItem("Sync now", sync_now),
        pystray.MenuItem("Pause background sync", pause),
        pystray.MenuItem("Resume background sync", resume),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit DriveSync", quit_app),
    )


def _icon_image(Image, ImageDraw):
    """64x64 tile: blue rounded square with white up/down arrows.

    Pure shapes (no font glyphs) so it renders identically everywhere.
    """
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=14, fill=(30, 108, 240, 255))
    d.polygon([(18, 36), (32, 36), (25, 18)], fill="white")   # up arrow head
    d.rectangle([22, 36, 28, 47], fill="white")               # up arrow stem
    d.polygon([(32, 28), (46, 28), (39, 46)], fill="white")   # down arrow head
    d.rectangle([36, 17, 42, 28], fill="white")               # down arrow stem
    return img
