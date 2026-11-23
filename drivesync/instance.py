"""Single-instance activation channel for the desktop app.

When "Stay in the system tray" is on, closing the window only hides it. On
desktops that launch a second process when the user clicks the pinned
launcher (KDE Plasma does this once the window is hidden), the second
process must ask the resident instance to show its window instead of just
reporting "already running" and exiting.

A per-user Unix socket carries those requests. The data directory is
already private to the user (0700), and the socket itself is created 0600,
so no other account can activate the window. The same channel also carries
the Dolphin action's ``add-pair:<path>`` request, which opens the add-pair
dialog pre-filled in the running instance.
"""

import os
import socket
import threading

from .config import app_dir

SOCKET_NAME = "gui.sock"


def socket_path():
    return app_dir() / SOCKET_NAME


def _send(payload, timeout):
    """Deliver one command to the resident GUI; True when acknowledged."""
    path = str(socket_path())
    if not os.path.exists(path):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall(payload)
            try:
                return sock.recv(16).startswith(b"ok")
            except OSError:
                # the request was delivered; the acknowledgement is best
                # effort (the process may be quitting)
                return True
    except OSError:
        return False


def request_show(timeout=0.75):
    """Ask the resident GUI to present its window.

    Returns True when the request was delivered and acknowledged. False
    means no GUI instance is listening (headless serve, stale socket, or
    the app is still starting up).
    """
    return _send(b"show\n", timeout)


def request_add_pair(local_path, timeout=0.75):
    """Ask the resident GUI to open the add-pair dialog for `local_path`.

    Used by the Dolphin context-menu action. Returns False when no GUI is
    listening, so the caller can start one instead.
    """
    text = str(local_path or "").replace("\n", " ").strip()
    if not text:
        return False
    return _send(b"add-pair:" + text.encode("utf-8", "replace") + b"\n",
                 timeout)


class ShowListener:
    """Accept 'show' requests and run ``on_show`` for each one.

    ``on_show`` runs on the listener thread. GTK callers should pass a
    function that marshals onto the main loop (``GLib.idle_add``).
    """

    def __init__(self, on_show, on_add_pair=None):
        self._on_show = on_show
        self._on_add_pair = on_add_pair
        self._server = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        """Bind and listen. Returns False when no socket can be created."""
        path = socket_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # We only get here after winning gui.lock, so any existing
            # socket is stale (a previous crash left it behind).
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(str(path))
                os.chmod(path, 0o600)
                server.listen(4)
                server.settimeout(0.5)
            except OSError:
                server.close()
                raise
        except OSError:
            return False
        self._server = server
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="drivesync-activate")
        self._thread.start()
        return True

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(0.5)
                    raw = conn.recv(4096).strip()
                    verb, _, argument = raw.partition(b":")
                    verb = verb.strip().lower()
                    if verb in (b"show", b"activate"):
                        self._on_show()
                    elif verb == b"add-pair" and self._on_add_pair:
                        self._on_add_pair(
                            argument.decode("utf-8", "replace"))
                    conn.sendall(b"ok\n")
                except OSError:
                    pass

    def stop(self):
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        try:
            socket_path().unlink()
        except OSError:
            pass
