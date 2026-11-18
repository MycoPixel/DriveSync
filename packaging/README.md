# Packaging DriveSync

Three ways to install the app without touching the terminal, and the
scripts/specs that build them:

| Format | File | Best for | Build with |
| --- | --- | --- | --- |
| **deb** | `drivesync_<ver>-1_all.deb` | Debian, Ubuntu, Mint, Pop!\_OS | `./build-deb.sh` (needs `dpkg-deb`) |
| **rpm** | `drivesync-<ver>-1.noarch.rpm` | Fedora, RHEL, openSUSE | `./build-rpm.sh` (needs `rpmbuild`) |
| AppImage | `DriveSync-<ver>-x86_64.AppImage` | any other distro, zero install | `./build-appimage.sh` (needs `python3`, `curl`) |

**Recommendation: publish deb + rpm as the primary artifacts** and attach
them to GitHub Releases (the included `.github/workflows/release.yml` builds
all three on every tag). Packages integrate with the software center, put
`drivesync` on PATH, install the menu launcher and the systemd user unit —
the AppImage is the fallback for distros without either package manager.

All three install modes keep user data (config, tokens, state DB) in
`~/.local/share/drivesync/`, exactly like the source install.

## What the packages do

- Python code → `/usr/lib/python3/dist-packages/drivesync` (deb) or
  `%{python3_sitelib}/drivesync` (rpm); flask/requests come from distro
  packages via `Depends`/`Requires`, so security updates flow through apt/dnf
- `/usr/bin/drivesync` launcher (same CLI as the source install)
- Menu launcher + hicolor SVG icon
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

## One-time release checklist

1. `git tag v0.2.0 && git push --tags` — Actions builds the artifacts.
2. Attach `dist/*` to the GitHub Release.
3. Users install with `sudo apt install ./drivesync_…deb` or
   `sudo dnf install ./drivesync-…rpm` — or double-click in most desktop
   software centers.
