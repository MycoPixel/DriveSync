# RPM spec for DriveSync.
#
# Build on any RPM-based machine (Fedora, RHEL, openSUSE):
#   ./packaging/build-rpm.sh          -> ~/rpmbuild/RPMS/noarch/drivesync-<ver>-1.noarch.rpm
#
# The package mirrors the .deb layout: python code under the site-packages
# dir, launcher in /usr/bin, menu launcher, icon and a systemd user unit.
Name:           drivesync
Version:        0.6.0
# No distro release suffix: this is a noarch, pure-python package and a
# host-specific tag made the artifact name differ per build machine for no
# benefit. Keep release/name stable everywhere.
Release:        1
Summary:        Google Drive sync client for Linux desktops

License:        MIT
URL:            https://github.com/MycoPixel/DriveSync
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3
Requires:       python3 >= 3.9
Requires:       python3-flask >= 2.2
Requires:       python3-requests >= 2.28
# native desktop UI ('drivesync gui', also the menu launcher target)
Requires:       python3-gobject
# Gtk 3 typelib needed by PyGObject (python3-gobject does not pull it on
# every rpm distro, and pip cannot provide it)
Requires:       gtk3
# optional tray icon support (pystray + pillow drive the icon;
# ayatana-appindicator is the backend it talks to on GNOME/KDE)
Recommends:     python3-pystray
Recommends:     libayatana-appindicator-gtk3
Recommends:     python3-pillow
# instant local-change sync (watch.py degrades to polling without it)
Recommends:     python3-watchdog
# optional read-only FUSE mount ('drivesync mount')
Recommends:     python3-fusepy
# desktop notifications for conflicts/errors while the window is hidden
Recommends:     libnotify

# python site-packages dir, computed on the build machine at spec-parse time
# — avoids a hard dependency on python-rpm-macros (which ships
# %{python3_sitelib} but is not installed on every rpm distro).
# /usr/bin/python3 pins the *system* interpreter even if a venv is active.
# Explicit base/platbase keep the path under /usr/lib even when the build
# host's python was configured with a /usr/local prefix (otherwise the
# package would install to /usr/local/lib and the launcher could not see it).
%global py_purelib %(/usr/bin/python3 -c "import sysconfig; print(sysconfig.get_path('purelib', vars={'base': '/usr', 'platbase': '/usr'}))")
%global appdir %{py_purelib}/drivesync
# some minimal build environments lack systemd-rpm-macros too
%{!?_userunitdir:%global _userunitdir /usr/lib/systemd/user}

# changelog date generated at build time so the weekday always matches
# (rpmbuild rejects changelog entries whose weekday != actual date).
# NOTE the doubled percent escapes below: rpm macro-expands the shell
# command before running it, so a bare percent sequence there is read as
# an rpm macro — and undefined ones simply vanish. That is exactly what
# once produced "Wed Nov  2026" and the 'bad date in changelog' error.
# Doubled percent reaches the shell as one literal percent, so date(1)
# gets its own format string back.
%global chlogdate %(date "+%%a %%b %%d %%Y")

%description
Two-way sync between local folders and Google Drive (upload-only and
download-only modes included), with a local web dashboard, a background
worker and an optional system tray icon.

Google Docs/Sheets/Slides/Drawings are exported as docx/xlsx/pptx/png files
that desktop apps can open; edits made on either side sync back. Tokens stay
on the machine and talk to Google directly.

%prep
%setup -q

%build
# pure-python app: nothing to compile

%install
mkdir -p %{buildroot}%{appdir} \
         %{buildroot}%{_bindir} \
         %{buildroot}%{_datadir}/applications \
         %{buildroot}%{_datadir}/metainfo \
         %{buildroot}%{_datadir}/icons/hicolor/scalable/apps \
         %{buildroot}%{_datadir}/kio/servicemenus \
         %{buildroot}%{_userunitdir} \
         %{buildroot}%{_docdir}/%{name}
cp -r drivesync/* %{buildroot}%{appdir}/
find %{buildroot}%{appdir} -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

cat > %{buildroot}%{_bindir}/drivesync <<'EOF'
#!/usr/bin/python3
import sys
from drivesync.cli import main

if __name__ == "__main__":
    sys.exit(main())
EOF
chmod 755 %{buildroot}%{_bindir}/drivesync

sed "s|__BIN__|/usr/bin|g; s|__ICON__|drivesync|g" drivesync.desktop \
    > %{buildroot}%{_datadir}/applications/drivesync.desktop
cp drivesync/static/mark.svg \
   %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/drivesync.svg

# AppStream metadata: what Discover / GNOME Software show about the app
cp drivesync.metainfo.xml \
   %{buildroot}%{_datadir}/metainfo/drivesync.metainfo.xml

# Dolphin/KDE right-click menu (sync a folder / add it as a pair)
install -m 644 packaging/kde/drivesync-servicemenu.desktop \
   %{buildroot}%{_datadir}/kio/servicemenus/drivesync-servicemenu.desktop

cat > %{buildroot}%{_userunitdir}/drivesync.service <<'EOF'
[Unit]
Description=DriveSync — Google Drive sync client (background worker + dashboard)
After=network-online.target

[Service]
ExecStart=/usr/bin/drivesync serve
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

# README/LICENSE are packaged via the %doc lines below (rpmbuild copies
# them from the source tree into %_docdir itself)

# refresh the caches Discover/GNOME Software read so the menu entry, icon
# and app metadata appear right after install (best-effort)
%post
/usr/bin/update-desktop-database >/dev/null 2>&1 || :
/usr/bin/gtk-update-icon-cache -fqt %{_datadir}/icons/hicolor >/dev/null 2>&1 || :

%postun
/usr/bin/update-desktop-database >/dev/null 2>&1 || :
/usr/bin/gtk-update-icon-cache -fqt %{_datadir}/icons/hicolor >/dev/null 2>&1 || :

%files
%doc README.md LICENSE CHANGELOG.md
%{_bindir}/drivesync
%{appdir}
%{_datadir}/applications/drivesync.desktop
%{_datadir}/metainfo/drivesync.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/drivesync.svg
%{_datadir}/kio/servicemenus/drivesync-servicemenu.desktop
%{_userunitdir}/drivesync.service

%changelog
* %{chlogdate} MycoPixel (DriveSync contributors) <drivesync@localhost> - 0.6.0-1
- Shared Drives: browse organizational drives as folder roots in the
  dashboard, GTK app and pair picker (list_drives + supportsAllDrives on
  every Files call, including mutations)
- Portable settings: 'drivesync config export/import' moves preferences
  and pair definitions between machines, never OAuth tokens or secrets
- Security: 'drivesync audit [--fix]', traversal-safe path joins for
  recovery/conflicts, private SQLite WAL/SHM modes, SECURITY.md
- Release pipeline builds and publishes deb, rpm, AppImage and Flatpak
  with SHA-256 checksums
- Optional read-only FUSE mount: 'drivesync mount <dir>'
- Dolphin/KDE context menu: sync a folder now or add it as a pair

* %{chlogdate} MycoPixel (DriveSync contributors) <drivesync@localhost> - 0.5.1-1
- Drive-tab downloads show live progress: spinner + per-file strip in the
  tab, headerbar mirror, and a persistent result summary; folder downloads
  stream per-file counts as the tree downloads (engine progress callback)
- A second download now runs concurrently instead of being silently
  swallowed until the app was restarted
- Selected rows are highlighted on every system theme (Material-3 tonal
  fill for treeview.view:selected) plus a Select all button
- Fixed the RPM build itself: a stale hardcoded Version tag pointed %prep
  at the old tarball and an unescaped date macro produced 'bad date in
  %changelog'

* %{chlogdate} MycoPixel (DriveSync contributors) <drivesync@localhost> - 0.5.0-1
- On-demand downloads: the GUI Drive tab lists your whole Drive and
  downloads any file or folder to a picked destination — no sync pair,
  no mount (Google Docs exported with the configured format)
- Selective sync per pair: Subfolders… dialog / 'pairs add --include' /
  'pairs set include_patterns=' restrict a pair to chosen subfolders;
  narrowing a scope deletes nothing, widening downloads on the next pass
- Dashboard API: GET /api/drive/<account>/children (files AND folders)
- Fixed: desktop app always opened on the Setup tab (GTK >= 3.24.49
  ignores set_current_page pre-show); startup page now applied post-show
- Fixed: launch failures print, log to gui-crash.log and show a dialog
  instead of dying silently; hand-edited config values cannot crash the
  window; the Drive tab loads lazily with inline listing errors

* %{chlogdate} DriveSync contributors <drivesync@localhost> - 0.4.0-1
- Instant local-change sync: inotify watcher (watchdog, optional) pokes
  the worker so local edits push in ~1s instead of the scan interval
- Desktop notifications for new conflicts/errors while the GUI is hidden
- 'drivesync sync --full' re-lists Drive and rehashes local files
- Bandwidth cap (bandwidth_limit_kb) and daily sync window (active_from/to)
- doctor reports watchdog/libnotify availability

* %{chlogdate} DriveSync contributors <drivesync@localhost> - 0.3.0-1
- Friendlier CLI: bare 'drivesync' shows next steps; config/pairs validate
  input with actionable errors; 'sync' guides first-time users
- serve: port-busy hint (detects the running systemd instance) and --open
- Activity log auto-prune (30 days / 5k rows) when the worker starts
- CHANGELOG.md shipped in the packages

* %{chlogdate} DriveSync contributors <drivesync@localhost> - 0.2.4-1
- Fixed 'drivesync gui' startup crash (activity hidden column type)
- AppStream metainfo + icon/desktop-db cache refresh on (un)install:
  Discover/GNOME Software now show developer, release notes and the icon

* %{chlogdate} DriveSync contributors <drivesync@localhost> - 0.2.3-1
- GUI Activity tab: newest-first list that keeps scroll/selection across
  refreshes, filter by action/level/time frame, sort toggle, clear-log

* %{chlogdate} DriveSync contributors <drivesync@localhost> - 0.2.2-1
- GUI: Browse Drive… folder picker in the Add-folder dialog (My Drive,
  Shared with me, shortcut resolution — same flow as the dashboard)

* %{chlogdate} DriveSync contributors <drivesync@localhost> - 0.2.1-1
- GUI launch crash on GTK3 CSS parsing fixed; timestamps shown in local time
- uninstall.sh mirrors install.sh; pip/deb/rpm installs handled
- Google Docs/Sheets/Slides/Drawings export to docx/xlsx/pptx/png
- Package-manager-aware tray dependency hints in install.sh
