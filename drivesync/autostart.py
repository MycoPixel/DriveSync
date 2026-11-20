"""Start-at-login via the XDG autostart standard.

Toggling the Options-tab switch writes or removes
``~/.config/autostart/drivesync.desktop`` (XDG_CONFIG_HOME respected).
The file carries a marker key so disable() only ever removes an entry
this module wrote — a hand-maintained autostart file for another DriveSync
copy is left alone and reported instead of silently deleted.
"""

import os
import sys
from pathlib import Path

MARKER = "X-DriveSync-Managed"

_TEMPLATE = """[Desktop Entry]
Type=Application
Name=DriveSync
Comment=DriveSync — Google Drive sync client
Exec={exec_line}
Terminal=false
Categories=Network;FileTransfer;
StartupNotify=false
X-GNOME-Autostart-enabled=true
{marker}=true
"""


def autostart_dir():
    """XDG autostart dir for the user (~/.config/autostart by default)."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(
        Path.home() / ".config")
    return Path(base) / "autostart"


def autostart_path():
    return autostart_dir() / "drivesync.desktop"


def _exec_line():
    """Command the autostart entry runs (the interpreter we are under).

    Inside an AppImage, sys.executable points into the temporary mount
    (/tmp/.mount_…), which is gone at the next login. The runtime exports
    APPIMAGE with the image's real path, so launch that instead.
    """
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return f'"{appimage}" gui'
    return f"{sys.executable} -m drivesync gui"


def is_enabled():
    """True when a managed (or at least DriveSync-branded) entry exists."""
    path = autostart_path()
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return MARKER in text or "drivesync" in text.lower()


def enable():
    """Write/refresh the autostart entry. Returns the file path."""
    path = autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_TEMPLATE.format(exec_line=_exec_line(),
                                     marker=MARKER), encoding="utf-8")
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    return path


def disable():
    """Remove the autostart entry we wrote.

    Returns True when a file was removed. A foreign file (no marker, no
    'drivesync' mention) is left untouched and False is returned so the
    toggle can warn instead of silently deleting someone else's launcher.
    """
    path = autostart_path()
    if not path.exists():
        return True
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if MARKER not in text and "drivesync" not in text.lower():
        return False           # not ours — do not delete it
    try:
        path.unlink()
    except OSError:
        return False
    return True
