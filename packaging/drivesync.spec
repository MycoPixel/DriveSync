# RPM spec for DriveSync.
#
# Build on any RPM-based machine (Fedora, RHEL, openSUSE):
#   ./packaging/build-rpm.sh          -> ~/rpmbuild/RPMS/noarch/drivesync-<ver>-1.noarch.rpm
#
# The package mirrors the .deb layout: python code under the site-packages
# dir, launcher in /usr/bin, menu launcher, icon and a systemd user unit.
Name:           drivesync
Version:        0.2.0
Release:        1%{?dist}
Summary:        Google Drive sync client for Linux desktops

License:        MIT
URL:            https://github.com/YOUR_USERNAME/drivesync
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3
Requires:       python3 >= 3.9
Requires:       python3-flask >= 2.2
Requires:       python3-requests >= 2.28
# native desktop UI ('drivesync gui', also the menu launcher target)
Requires:       python3-gobject
# optional tray icon support (pystray + pillow drive the icon;
# ayatana-appindicator is the backend it talks to on GNOME/KDE)
Recommends:     python3-pystray
Recommends:     libayatana-appindicator-gtk3
Recommends:     python3-pillow

# python site-packages dir, computed on the build machine at spec-parse time
# — avoids a hard dependency on python-rpm-macros (which ships
# %{python3_sitelib} but is not installed on every rpm distro).
# /usr/bin/python3 pins the *system* interpreter even if a venv is active.
%global py_purelib %(/usr/bin/python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
%global appdir %{py_purelib}/drivesync
# some minimal build environments lack systemd-rpm-macros too
%{!?_userunitdir:%global _userunitdir /usr/lib/systemd/user}

# changelog date generated at build time so the weekday always matches
# (rpmbuild rejects changelog entries whose weekday != actual weekday)
%global chlogdate %(date "+%a %b %-d %Y")

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
         %{buildroot}%{_datadir}/icons/hicolor/scalable/apps \
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

%files
%doc README.md LICENSE
%{_bindir}/drivesync
%{appdir}
%{_datadir}/applications/drivesync.desktop
%{_datadir}/icons/hicolor/scalable/apps/drivesync.svg
%{_userunitdir}/drivesync.service

%changelog
* %{chlogdate} DriveSync contributors <drivesync@localhost> - 0.2.0-1
- Google Docs/Sheets/Slides/Drawings export to docx/xlsx/pptx/png
- Package-manager-aware tray dependency hints in install.sh
