#!/usr/bin/env bash
# Build the DriveSync .deb package.
#
# Needs: dpkg-deb (present on any Debian/Ubuntu machine and WSL), python3.
# The package installs system-wide program files (python code into
# /usr/lib/python3/dist-packages) and declares its python dependencies as
# proper Debian packages — no bundled venv, so updates come from apt.
#
#   ./packaging/build-deb.sh            -> dist/drivesync_<ver>-1_all.deb
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

VERSION="$(python3 - "$ROOT" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from drivesync import __version__
print(__version__)
PY
)"
echo "==> Building drivesync ${VERSION}"

# ---- staging tree ------------------------------------------------------
PKG="$STAGE/drivesync"
D="$PKG/DEBIAN"
mkdir -p "$D" \
         "$PKG/usr/bin" \
         "$PKG/usr/lib/python3/dist-packages" \
         "$PKG/usr/share/applications" \
         "$PKG/usr/share/metainfo" \
         "$PKG/usr/share/icons/hicolor/scalable/apps" \
         "$PKG/usr/share/icons/hicolor/256x256/apps" \
         "$PKG/usr/share/kio/servicemenus" \
         "$PKG/usr/lib/systemd/user" \
         "$PKG/usr/share/doc/drivesync"

# application code (the python package itself)
cp -r "$ROOT/drivesync" "$PKG/usr/lib/python3/dist-packages/"
find "$PKG/usr/lib/python3/dist-packages" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
chmod -R go-w "$PKG/usr/lib/python3/dist-packages"
find "$PKG/usr/lib/python3/dist-packages" -type f -exec chmod 644 {} +

# console launcher (what the pip console-script entry point would generate)
cat > "$PKG/usr/bin/drivesync" <<EOF
#!/usr/bin/python3
import sys
from drivesync.cli import main

if __name__ == "__main__":
    sys.exit(main())
EOF
chmod 755 "$PKG/usr/bin/drivesync"

# menu launcher + icon
sed "s|__BIN__|/usr/bin|g; s|__ICON__|drivesync|g" "$ROOT/drivesync.desktop" \
    > "$PKG/usr/share/applications/drivesync.desktop"
cp "$ROOT/drivesync/static/mark.svg" \
   "$PKG/usr/share/icons/hicolor/scalable/apps/drivesync.svg"
cp "$ROOT/drivesync/static/mark.png" \
   "$PKG/usr/share/icons/hicolor/256x256/apps/drivesync.png"

# AppStream metadata: what Discover / GNOME Software show about the app
cp "$ROOT/drivesync.metainfo.xml" \
   "$PKG/usr/share/metainfo/drivesync.metainfo.xml"

# Dolphin/KDE right-click menu (sync a folder / add it as a pair)
cp "$ROOT/packaging/kde/drivesync-servicemenu.desktop" \
   "$PKG/usr/share/kio/servicemenus/drivesync-servicemenu.desktop"
chmod 644 "$PKG/usr/share/kio/servicemenus/drivesync-servicemenu.desktop"

# systemd *user* unit (users enable it themselves: systemctl --user enable)
cat > "$PKG/usr/lib/systemd/user/drivesync.service" <<'EOF'
[Unit]
Description=DriveSync — Google Drive sync client (background worker + dashboard)
After=network-online.target

[Service]
ExecStart=/usr/bin/drivesync serve
Restart=on-failure
RestartSec=10
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=off
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictSUIDSGID=yes
LockPersonality=yes
PrivateDevices=yes
ProtectClock=yes
ProtectHostname=yes
ProtectKernelLogs=yes
RestrictNamespaces=yes
RestrictRealtime=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
UMask=0077
TimeoutStopSec=30

[Install]
WantedBy=default.target
EOF

# docs
cp "$ROOT/README.md" "$ROOT/LICENSE" "$ROOT/CHANGELOG.md" \
   "$PKG/usr/share/doc/drivesync/"
chmod 644 "$PKG/usr/share/doc/drivesync"/*

# ---- package metadata --------------------------------------------------
SIZE_KB="$(du -sk --exclude=DEBIAN "$PKG" | cut -f1)"
cat > "$D/control" <<EOF
Package: drivesync
Version: ${VERSION}-1
Section: utils
Priority: optional
Architecture: all
Installed-Size: ${SIZE_KB}
Depends: python3 (>= 3.10), python3-flask (>= 2.2), python3-requests (>= 2.28), python3-gi, gir1.2-gtk-3.0
Recommends: python3-pystray, gir1.2-ayatanaappindicator3-0.1, python3-pil, python3-watchdog, gir1.2-notify-0.7, python3-fusepy
Suggests: systemd
Maintainer: DriveSync contributors <drivesync@localhost>
Description: Google Drive sync client for Linux desktops
 Two-way sync between local folders and Google Drive (upload-only and
 download-only modes included), with a local web dashboard, a background
 worker and an optional system tray icon.
 .
 Google Docs/Sheets/Slides/Drawings are exported as docx/xlsx/pptx/png
 files that desktop apps can open; edits made on either side sync back.
 Tokens stay on the machine and talk to Google directly.
EOF

cat > "$D/postinst" <<'EOF'
#!/bin/sh
set -e
echo
echo "DriveSync installed."
echo "  1. Create a Google OAuth 'Desktop app' client (see /usr/share/doc/drivesync/README.md)"
echo "  2. Run 'drivesync auth' to connect your Google account"
echo "  3. Optional autostart: systemctl --user enable --now drivesync"
echo "  4. Open the dashboard at http://127.0.0.1:5678"
# refresh the caches Discover/GNOME Software read so the menu entry, icon
# and app metadata appear right after install (best-effort)
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -fqt /usr/share/icons/hicolor >/dev/null 2>&1 || true
exit 0
EOF
chmod 755 "$D/postinst"

# ---- build -------------------------------------------------------------
mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/drivesync_${VERSION}-1_all.deb"
dpkg-deb --root-owner-group --build "$PKG" "$OUT"
echo "==> $OUT"
dpkg-deb --info "$OUT" | head -20
