#!/usr/bin/env bash
# DriveSync uninstaller — the inverse of ./install.sh.
#
# Removes the user-local install (app copy, virtualenv, launcher, menu
# entry, systemd user unit) plus any pip/pipx-installed copy. Your sync
# data (~/.local/share/drivesync: config.json, state.db, tokens) is KEPT
# by default so a reinstall picks up where you left off; run
# `./uninstall.sh --purge` to delete it too.
#
# .deb/.rpm installs are detected and handed to the package manager,
# which owns their files.
set -euo pipefail

PREFIX="${HOME}/.local"
BIN_DIR="${PREFIX}/bin"
APP_DIR="${PREFIX}/share/drivesync"            # app copy + venv (install.sh)
DATA_DIR="${HOME}/.local/share/drivesync"      # user data (same dir, by design)
DESKTOP="${HOME}/.local/share/applications/drivesync.desktop"
UNIT="${HOME}/.config/systemd/user/drivesync.service"

PURGE=0
case "${1:-}" in
    --purge) PURGE=1 ;;
    "") ;;
    *) echo "usage: $0 [--purge]" >&2; exit 2 ;;
esac

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }

as_root() {
    if [ "$(id -u)" = "0" ]; then "$@"
    elif command -v sudo >/dev/null 2>&1; then sudo "$@"
    else
        echo "need root to: $*" >&2
        return 1
    fi
}

say "Uninstalling DriveSync"

# 1. package-manager installs own their files — hand over to the manager
if command -v rpm >/dev/null 2>&1 && rpm -q drivesync >/dev/null 2>&1; then
    say "installed as an RPM — removing it with dnf"
    exec as_root dnf remove -y drivesync
fi
if command -v dpkg >/dev/null 2>&1 && dpkg -s drivesync >/dev/null 2>&1; then
    say "installed as a .deb — removing it with apt"
    exec as_root apt remove -y drivesync
fi

# 2. stop the background worker service and drop its unit
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now drivesync >/dev/null 2>&1 || true
    rm -f "${UNIT}"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

# 3. stop any running desktop app / dashboard / worker (matches only the
#    app processes — NOT this script, which may live in a …/drivesync/ path)
pkill -f '(drivesync (gui|serve)|-m drivesync)' >/dev/null 2>&1 || true

# 4. pip-installed copy (e.g. /usr/local/lib/python3.14/site-packages)
if python3 -m pip show drivesync >/dev/null 2>&1; then
    say "removing the pip-installed copy"
    if ! python3 -m pip uninstall -y drivesync >/dev/null 2>&1; then
        say "pip needs root to remove it — retrying with sudo"
        as_root python3 -m pip uninstall -y drivesync || true
    fi
fi
if command -v pipx >/dev/null 2>&1 && pipx list 2>/dev/null | grep -q 'drivesync'; then
    say "removing the pipx install"
    pipx uninstall drivesync || true
fi

# 5. install.sh launcher — only ours (pip installs its own console script)
if [ -f "${BIN_DIR}/drivesync" ] && \
   grep -q "share/drivesync/.venv" "${BIN_DIR}/drivesync" 2>/dev/null; then
    rm -f "${BIN_DIR}/drivesync"
fi

# 6. app copy + virtualenv. The user data lives in the SAME directory
#    (install.sh's APP_DIR == DATA_DIR), so only the app bits are removed
#    here; the data files survive unless --purge was given.
if [ -d "${APP_DIR}" ]; then
    say "removing app copy and virtualenv (${APP_DIR}/{drivesync,.venv})"
    rm -rf "${APP_DIR}/drivesync" "${APP_DIR}/.venv"
    rm -f "${APP_DIR}/requirements.txt" "${APP_DIR}/README.md" "${APP_DIR}/LICENSE"
fi

# 7. menu entry
rm -f "${DESKTOP}"
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "${HOME}/.local/share/applications" >/dev/null 2>&1 || true

# 8. data
if [ "${PURGE}" = "1" ]; then
    say "purging all sync data (${DATA_DIR})"
    rm -rf "${DATA_DIR}"
    say "DriveSync removed — config, tokens and sync state were deleted."
else
    if [ -d "${DATA_DIR}" ] && [ -n "$(ls -A "${DATA_DIR}" 2>/dev/null)" ]; then
        say "kept your sync data in ${DATA_DIR}"
        note "config.json, state.db (accounts, pairs, tokens), locks"
        note "a reinstall picks it up automatically"
        note "delete it later with: rm -rf ${DATA_DIR}  — or rerun this script with --purge"
    else
        rm -rf "${DATA_DIR}"
    fi
    say "DriveSync removed."
fi
