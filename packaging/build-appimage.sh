#!/usr/bin/env bash
# Build a DriveSync AppImage.
#
# Needs: python3 + pip, curl, and appimagetool (fetched automatically on
# first run into ~/.local/bin). The AppDir bundles the app plus a venv
# created with the *build machine's* python — so the result runs on
# machines with a compatible glibc and the same python 3.x minor family
# (build on an older, common distro for best reach; e.g. Ubuntu 22.04).
#
#   ./packaging/build-appimage.sh   -> dist/DriveSync-<ver>-x86_64.AppImage
#
# NOTE: for most users the .deb/.rpm packages are the better choice — they
# are smaller, integrate with the software center and autostart cleanly.
# The AppImage is the zero-install fallback for "everything else" distros.
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
mkdir -p "$APPDIR/usr" "$APPDIR/usr/bin"
# --system-site-packages lets the venv see the distro's PyGObject bindings:
# pystray and the optional desktop notifications need `gi`, which pip cannot
# install usefully inside an AppImage. The app still runs without it.
python3 -m venv --system-site-packages "$APPDIR/usr/venv"
"$APPDIR/usr/venv/bin/pip" install --quiet flask requests \
    pillow pystray watchdog
cp -r "$ROOT/drivesync" "$APPDIR/usr/app"
find "$APPDIR/usr/app" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/bash
HERE="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$HERE/usr/app"
exec "$HERE/usr/venv/bin/python" -m drivesync "$@"
EOF
chmod 755 "$APPDIR/AppRun"

cat > "$APPDIR/DriveSync.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DriveSync
GenericName=Google Drive Sync Client
Comment=Sync Google Drive folders on your Linux desktop
Exec=DriveSync serve
Icon=drivesync
Terminal=false
Categories=Network;FileTransfer;
Keywords=drive;sync;google;backup;
StartupWMClass=DriveSync
EOF
cp "$ROOT/drivesync/static/mark.svg" "$APPDIR/drivesync.svg"

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
