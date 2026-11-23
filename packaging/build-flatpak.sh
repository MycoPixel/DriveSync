#!/usr/bin/env bash
# Build a DriveSync Flatpak bundle.
#
# Needs: flatpak, flatpak-builder and the org.gnome.{Platform,Sdk} 46
# runtime (installed automatically from Flathub on first run).
#
#   ./packaging/build-flatpak.sh            -> dist/DriveSync-<ver>-x86_64.flatpak
#   ./packaging/build-flatpak.sh --install  -> also install it for this user
#
# Flatpak is the sandboxed option: the app keeps its data under
# ~/.var/app/io.github.drivesync.DriveSync instead of ~/.local/share, and
# the systemd user unit from the deb/rpm is not used (enable autostart from
# the dashboard instead).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
MANIFEST="$HERE/flatpak/io.github.drivesync.DriveSync.yml"
BUILD="$ROOT/build-flatpak"
REPO="$ROOT/dist/flatpak-repo"

VERSION="$(python3 - "$ROOT" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from drivesync import __version__
print(__version__)
PY
)"
ARCH="$(flatpak --default-arch)"

echo "==> Building DriveSync ${VERSION} Flatpak (${ARCH})"

if ! flatpak remote-list --user | grep -q '^flathub'; then
    flatpak remote-add --user --if-not-exists flathub \
        https://flathub.org/repo/flathub.flatpakrepo
fi
flatpak install --user -y flathub \
    "org.gnome.Platform//46" "org.gnome.Sdk//46"

mkdir -p "$ROOT/dist"
# The manifest's build-options share the network with the build sandbox so
# the modules' pip installs can reach PyPI (the runtime download above runs
# on the host).
flatpak-builder --user --force-clean --repo="$REPO" "$BUILD" "$MANIFEST"

OUT="$ROOT/dist/DriveSync-${VERSION}-${ARCH}.flatpak"
flatpak build-bundle "$REPO" "$OUT" io.github.drivesync.DriveSync
echo "==> $OUT"

if [[ "${1:-}" == "--install" ]]; then
    flatpak install --user -y "$OUT"
    echo "==> Installed. Run: flatpak run io.github.drivesync.DriveSync"
fi
