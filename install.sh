#!/usr/bin/env bash
# DriveSync installer — user-local install, no root required.
set -euo pipefail

PREFIX="${HOME}/.local"
BIN_DIR="${PREFIX}/bin"
APP_DIR="${PREFIX}/share/drivesync"
DATA_DIR="${HOME}/.local/share/drivesync"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

say "Installing DriveSync (user-local)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required but was not found." >&2
    exit 1
fi

# 1. copy the application
mkdir -p "${APP_DIR}" "${BIN_DIR}" "${DATA_DIR}"
cp -r drivesync "${APP_DIR}/"
cp requirements.txt README.md LICENSE "${APP_DIR}/" 2>/dev/null || true

# 2. dependencies into a private venv
# --system-site-packages lets the venv use the distro GObject bindings
# (python3-gobject), which pystray needs for the AppIndicator tray icon
say "Creating virtualenv at ${APP_DIR}/.venv"
python3 -m venv --system-site-packages "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/.venv/bin/pip" install --quiet -r requirements.txt

# optional tray-icon deps (best effort — the dashboard works without them)
"${APP_DIR}/.venv/bin/pip" install --quiet pystray pillow \
    || say "optional tray deps skipped; dashboard still works"
if ! python3 -c "import gi; gi.require_version('AyatanaAppIndicator3','0.1')" 2>/dev/null; then
    # every distro names these GObject/AppIndicator packages differently —
    # detect the package manager and give the exact command that works
    if command -v apt-get >/dev/null 2>&1; then
        say "tray icon needs GI bindings: sudo apt install python3-gi python3-gi-cairo gir1.2-ayatanaappindicator3-0.1 gir1.2-gtk-3.0"
    elif command -v dnf >/dev/null 2>&1; then
        say "tray icon needs GI bindings: sudo dnf install python3-gobject libayatana-appindicator-gtk3"
    elif command -v pacman >/dev/null 2>&1; then
        say "tray icon needs GI bindings: sudo pacman -S python-gobject libayatana-appindicator"
    elif command -v zypper >/dev/null 2>&1; then
        say "tray icon needs GI bindings: sudo zypper install python3-gobject-gtk libayatana-appindicator3-1 typelib-1_0-AyatanaAppindicator3-0_1"
    elif command -v apk >/dev/null 2>&1; then
        say "tray icon needs GI bindings: apk add py3-gobject3 libayatana-appindicator"
    elif command -v emerge >/dev/null 2>&1; then
        say "tray icon needs GI bindings: emerge dev-python/pygobject dev-libs/libayatana-appindicator"
    else
        say "tray icon needs GI bindings: install python3-gobject and libayatana-appindicator with your distro's package manager"
    fi
fi

# sanity check: transfers need a valid TLS CA bundle (certifi)
if ! "${APP_DIR}/.venv/bin/python" -c "import os, certifi, sys; sys.exit(0 if os.path.exists(certifi.where()) else 1)" 2>/dev/null; then
    say "WARNING: TLS CA bundle check failed — recreate the venv and rerun this installer"
fi

# 3. launcher script
# PYTHONPATH is required: the venv only holds the dependencies; `python -m`
# finds the app package via sys.path, which otherwise starts at the CWD
cat > "${BIN_DIR}/drivesync" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="${APP_DIR}\${PYTHONPATH:+:\${PYTHONPATH}}"
exec "${APP_DIR}/.venv/bin/python" -m drivesync "\$@"
EOF
chmod +x "${BIN_DIR}/drivesync"

# 4. desktop launcher (shows up in app menus)
if [ -d "${HOME}/.local/share/applications" ] || mkdir -p "${HOME}/.local/share/applications"; then
    sed "s|__BIN__|${BIN_DIR}|g; s|__ICON__|${APP_DIR}/drivesync/static/mark.svg|g" \
        drivesync.desktop > "${HOME}/.local/share/applications/drivesync.desktop" 2>/dev/null || true
fi

# 5. systemd user unit (optional, autostart on login)
if command -v systemctl >/dev/null 2>&1; then
    UNIT_DIR="${HOME}/.config/systemd/user"
    mkdir -p "${UNIT_DIR}"
    sed "s|__VENV__|${APP_DIR}/.venv|g" drivesync.service > "${UNIT_DIR}/drivesync.service"
    systemctl --user daemon-reload || true
    say "Optional: run 'systemctl --user enable --now drivesync' to autostart"
fi

cat <<DONE

DriveSync installed.

  Next steps:
    1. Create a Google OAuth 'Desktop app' client (see README, 2 minutes)
    2. drivesync auth                  # connect your Google account
    3. drivesync serve                 # dashboard: http://127.0.0.1:5678
    4. drivesync pairs add ~/Drive --remote-id <folder-id>

  Make sure ${BIN_DIR} is on your PATH (usually is on major distros).
DONE
