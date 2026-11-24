#!/usr/bin/env bash
# Build a DriveSync AppImage.
#
# Needs: python3 (only to compute the version + create the payload when the
# host-python fallback is chosen), curl, and appimagetool (fetched
# automatically on first run into ~/.local/bin).
#
# By default the AppImage bundles its OWN CPython from
# python-build-standalone, so the result runs on any distro with a
# compatible glibc — no "same python 3.x minor family" requirement and no
# host venv quirks. Override pieces with env vars:
#
#   PY_STANDALONE_VERSION  bundled CPython version      (default 3.12.7)
#   PY_STANDALONE_RELEASE  upstream release tag         (default 20241016)
#   PY_STANDALONE_SHA256   sha256 of the tarball        (empty = no check)
#   PY_STANDALONE_URL      full tarball URL (overrides the two above)
#   DS_USE_HOST_PYTHON=1   old behavior: venv from the build machine's
#                          python3 with --system-site-packages (smaller
#                          download, but ties the image to the build
#                          host's python minor and distro packages)
#
#   ./packaging/build-appimage.sh   -> dist/DriveSync-<ver>-x86_64.AppImage
#
# NOTE: for most users the .deb/.rpm packages are the better choice — they
# are smaller, integrate with the software center and autostart cleanly.
# The AppImage bundles its own CPython for the CLI/dashboard/tray; its
# `gui` command uses the host's python3 when GTK 3 + PyGObject are present
# (a bundled CPython cannot import the distro's compiled gi extension), and
# prints the install command when they are missing. Set DRIVESYNC_PYTHON to
# force a specific interpreter for the window.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
VERSION="$(python3 -c "import sys; sys.path.insert(0, '$ROOT'); from drivesync import __version__; print(__version__)")"
STAGE="$(mktemp -d)"
APPDIR="$STAGE/DriveSync.AppDir"
trap 'rm -rf "$STAGE"' EXIT
ARCH="${ARCH:-x86_64}"

echo "==> Building DriveSync ${VERSION} AppImage (${ARCH})"

# ---- app payload -------------------------------------------------------
mkdir -p "$APPDIR/usr" "$APPDIR/usr/bin" "$APPDIR/usr/app"
# The package must be importable as `drivesync`, so it needs its own
# directory under usr/app (PYTHONPATH points at usr/app). Copying the
# package *as* usr/app put cli.py at the top level and every command died
# with "No module named drivesync".
cp -r "$ROOT/drivesync" "$APPDIR/usr/app/drivesync"
find "$APPDIR/usr/app" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

if [ "${DS_USE_HOST_PYTHON:-0}" = "1" ]; then
    # legacy mode: reuse the build machine's interpreter. --system-site-
    # packages lets the venv see the distro's PyGObject bindings (GTK app).
    echo "==> bundling the host python (DS_USE_HOST_PYTHON=1)"
    python3 -m venv --system-site-packages "$APPDIR/usr/venv"
    "$APPDIR/usr/venv/bin/pip" install --quiet flask requests \
        pillow pystray watchdog
    PYBIN="$APPDIR/usr/venv/bin/python"
else
    # bundle a standalone CPython: the AppImage no longer cares which
    # python minor the build host (or the run host) ships.
    PYVER="${PY_STANDALONE_VERSION:-3.12.7}"
    PYREL="${PY_STANDALONE_RELEASE:-20241016}"
    TRIPLE="${PY_STANDALONE_TRIPLE:-${ARCH}-unknown-linux-gnu}"
    TARBALL="cpython-${PYVER}+${PYREL}-${TRIPLE}-install_only.tar.gz"
    URL="${PY_STANDALONE_URL:-https://github.com/astral-sh/python-build-standalone/releases/download/${PYREL}/${TARBALL}}"
    SHA="${PY_STANDALONE_SHA256:-}"
    TARBALL_PATH="$STAGE/$TARBALL"

    echo "==> downloading bundled CPython ${PYVER} (${PYREL})"
    curl -fL --retry 3 -o "$TARBALL_PATH" "$URL" \
        || { echo "ERROR: could not download $URL" >&2
             echo "       override with PY_STANDALONE_URL=<full tarball url>" \
                 >&2
             echo "       or build with DS_USE_HOST_PYTHON=1" >&2
             exit 1; }
    if [ -n "$SHA" ]; then
        echo "$SHA  $TARBALL_PATH" | sha256sum -c - >/dev/null \
            || { echo "ERROR: sha256 mismatch for $TARBALL" >&2; exit 1; }
    fi

    mkdir -p "$APPDIR/usr/python"
    tar -xzf "$TARBALL_PATH" -C "$APPDIR/usr/python" --strip-components=1
    PYBIN="$APPDIR/usr/python/bin/python3"
    # a bundled interpreter has no venv machinery worth shipping: install
    # the dependencies straight into it (keeps the image smaller too)
    "$PYBIN" -m pip install --quiet --no-warn-script-location \
        flask requests pillow pystray watchdog
fi

# AppRun: the bundled interpreter runs the CLI and the dashboard, but the
# GTK window needs PyGObject — a C extension built for one Python ABI, so
# the bundled CPython cannot import the distro's gi. For `gui` we therefore
# exec the host's python3 when it has GTK 3 + PyGObject + requests (every
# desktop distro ships them); otherwise print the exact install command.
# DRIVESYNC_PYTHON overrides the interpreter search.
PYBIN_REL="${PYBIN#"$APPDIR"}"   # e.g. /usr/python/bin/python3
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/bash
HERE="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$HERE/usr/app"

# Double-clicking the AppImage (or a launcher that passes no arguments)
# should open the window, not print CLI help.
[ "$#" -eq 0 ] && set -- gui

if [ "${1:-}" = "gui" ]; then
    for py in "${DRIVESYNC_PYTHON:-}" "__PYBIN__" python3 /usr/bin/python3; do
        [ -n "$py" ] || continue
        command -v "$py" >/dev/null 2>&1 || continue
        if "$py" - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk  # noqa: F401
import requests  # noqa: F401
PY
        then
            # Run the window on the host interpreter. LD_LIBRARY_PATH is
            # deliberately NOT set here: the bundled python's lib dir can
            # contain libs the host GTK stack must not load.
            exec "$py" -m drivesync "$@"
        fi
    done
    cat >&2 <<'MSG'
The DriveSync GTK window needs PyGObject, GTK 3 and requests for the
system Python. Install them, or use the deb/rpm package instead:
  Fedora/Ultramarine: sudo dnf install python3-gobject gtk3 python3-requests
  Debian/Ubuntu:      sudo apt install python3-gi gir1.2-gtk-3.0 python3-requests
The dashboard and CLI work without GTK:  DriveSync-*.AppImage serve --open
MSG
    exit 1
fi

# LD_LIBRARY_PATH keeps libpython resolvable on hosts whose own libs are
# older than the bundled build expects
export LD_LIBRARY_PATH="$HERE/usr/python/lib:${LD_LIBRARY_PATH:-}"
exec "__PYBIN__" -m drivesync "$@"
EOF
# PYBIN is an absolute path into the staging temp dir; rewrite it to the
# runtime location inside the (relocatable) AppImage.
sed -i "s|__PYBIN__|\$HERE${PYBIN_REL}|g" "$APPDIR/AppRun"
chmod 755 "$APPDIR/AppRun"

cat > "$APPDIR/DriveSync.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DriveSync
GenericName=Google Drive Sync Client
Comment=Sync Google Drive folders on your Linux desktop
Exec=DriveSync gui
Icon=drivesync
Terminal=false
Categories=Network;FileTransfer;
Keywords=drive;sync;google;backup;
StartupWMClass=DriveSync
StartupNotify=true
SingleMainWindow=true
X-GNOME-UsesNotifications=true
Actions=SyncNow;Dashboard;
EOF
cat >> "$APPDIR/DriveSync.desktop" <<'EOF'

[Desktop Action SyncNow]
Name=Sync all now
Exec=DriveSync sync

[Desktop Action Dashboard]
Name=Open dashboard
Exec=DriveSync serve --open
EOF
cp "$ROOT/drivesync/static/mark.svg" "$APPDIR/drivesync.svg"
# appimagetool builds .DirIcon from <Icon>.png (SVG alone is not accepted)
cp "$ROOT/drivesync/static/mark.png" "$APPDIR/drivesync.png"
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp "$ROOT/drivesync/static/mark.png" \
   "$APPDIR/usr/share/icons/hicolor/256x256/apps/drivesync.png"

# ---- appimagetool ------------------------------------------------------
TOOL_DIR="$HOME/.local/bin"
mkdir -p "$TOOL_DIR"
APPIMAGETOOL="$TOOL_DIR/appimagetool-$ARCH.AppImage"
if [ ! -x "$APPIMAGETOOL" ]; then
    echo "==> downloading appimagetool"
    curl -fsSL -o "$APPIMAGETOOL" \
        "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
    chmod +x "$APPIMAGETOOL"
fi

mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/DriveSync-${VERSION}-${ARCH}.AppImage"
# appimagetool reads ARCH from the environment when it was not built for a
# single architecture; export it so the generated AppImage matches the name
export ARCH
# --appimage-extract-and-run avoids needing FUSE on the build host
APPIMAGETOOL_APPIMAGE="$APPIMAGETOOL" \
    "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUT"
echo "==> $OUT"
