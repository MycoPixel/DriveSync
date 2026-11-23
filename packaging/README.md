# Packaging DriveSync

Four ways to install the app without touching the terminal, and the
scripts/specs that build them:

| Format | File | Best for | Build with |
| --- | --- | --- | --- |
| **deb** | `drivesync_<ver>-1_all.deb` | Debian, Ubuntu, Mint, Pop!\_OS | `./build-deb.sh` (needs `dpkg-deb`) |
| **rpm** | `drivesync-<ver>-1.noarch.rpm` | Fedora, RHEL, openSUSE | `./build-rpm.sh` (needs `rpmbuild`) |
| AppImage | `DriveSync-<ver>-x86_64.AppImage` | any other distro, zero install | `./build-appimage.sh` (needs `python3`, `curl`) |
| Flatpak | `DriveSync-<ver>-x86_64.flatpak` | sandboxed install, atomic updates | `./build-flatpak.sh` (needs `flatpak-builder`) |

**Recommendation: publish deb + rpm as the primary artifacts** and attach
them to GitHub Releases (the included `.github/workflows/release.yml` builds
deb, rpm, AppImage and Flatpak on every `v*` tag and publishes them with a
`SHA256SUMS` file). Packages integrate with the software center, put
`drivesync` on PATH, install the menu launcher and the systemd user unit —
the AppImage and Flatpak are the fallbacks for distros without either
package manager.

All install modes keep user data (config, tokens, state DB) in
`~/.local/share/drivesync/` (or `~/.var/app/…` under Flatpak), exactly like
the source install.

## What the packages do

- Python code → `/usr/lib/python3/dist-packages/drivesync` (deb) or
  `%{python3_sitelib}/drivesync` (rpm); flask/requests come from distro
  packages via `Depends`/`Requires`, so security updates flow through apt/dnf
- `/usr/bin/drivesync` launcher (same CLI as the source install)
- Menu launcher + hicolor SVG icon
- Dolphin/KDE service menu (`packaging/kde/drivesync-servicemenu.desktop`)
  for "Sync this folder now" and "Add this folder to DriveSync…"
- systemd **user** unit → enable autostart with
  `systemctl --user enable --now drivesync`
- The tray icon needs GObject bindings, declared as
  `Recommends`/`Suggests`; apt/dnf install them automatically unless run
  with `--no-install-recommends`

## Notes on the AppImage

The AppImage bundles a venv built with the **build machine's** python, so it
runs on machines with a compatible glibc and the same python 3.x minor
family. Build on an older, widely-shared distro (e.g. Ubuntu 22.04) for the
best reach. It does not register a systemd autostart unit — run it once and
enable autostart from the dashboard instead.

## Notes on the Flatpak

The manifest (`flatpak/io.github.drivesync.DriveSync.yml`) builds on
`org.gnome.Platform//46`, which already ships GTK 3, PyGObject and
libnotify; only Flask, requests and the app itself are added. It requests
home-directory access because sync folders live there, plus Wayland/X11,
notifications and the tray (StatusNotifier) interfaces.

Inside the sandbox the app keeps its data in
`~/.var/app/io.github.drivesync.DriveSync` instead of `~/.local/share`, and
the systemd user unit is not used — enable autostart from the dashboard.
The desktop file and AppStream metadata are installed under the Flatpak app
ID, and the window adopts that ID on Wayland so the launcher and running
window are associated correctly.

## One-time release checklist

1. Bump `drivesync/__init__.py`, `packaging/drivesync.spec` (Version and
   `%changelog`), `drivesync.metainfo.xml` and `CHANGELOG.md` together.
2. `git tag v0.6.0 && git push --tags` — Actions runs the test matrix,
   builds all four packages, attaches them to a **draft** release and
   publishes it with `SHA256SUMS`. If any build fails the release stays a
   draft: fix it, move the tag (`git tag -f …`), force-push it and re-run.
3. Users install with `sudo apt install ./drivesync_…deb` or
   `sudo dnf install ./drivesync-…rpm` — or double-click in most desktop
   software centers.
