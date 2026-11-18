# Changelog

All notable changes to DriveSync, newest first. Dates are release dates.
Older entries below 0.2.1 are summarized from the git history.

## [0.3.0] - 2026-11-18

### Added
- `drivesync serve --open` opens the dashboard in a browser once it is up
  (best-effort: silently skipped on headless machines)
- Activity log auto-prune: the worker drops rows older than 30 days and
  caps the table at 5,000 entries when it starts, so a long-running
  service can no longer bloat `state.db`

### Changed
- Bare `drivesync` now prints a getting-started banner (data dir + next
  steps) instead of an argparse error
- `config set` validates the key (typo → list of valid settings) and value
  (out-of-range → the actual constraint) instead of crashing or silently
  "saving" an unknown key
- `pairs set` validates field names and pair IDs; `pairs add` checks the
  account exists before creating a pair bound to a missing account
- `sync` explains what to do when there are no enabled pairs, and names
  the pair when `--pair N` does not exist
- `status` suggests the next command while nothing is configured
- `serve` diagnoses a busy dashboard port (usually the systemd service
  already running) instead of a traceback

## [0.2.4] - 2026-11-18

### Fixed
- GUI launch crash — `TypeError: Must be number, not str` on start (the
  activity list's hidden column was declared `float` while the code
  appended the raw timestamp string); the store now also refuses to save
  NULLs that would crash the same list

### Added
- AppStream metainfo (`/usr/share/metainfo/drivesync.metainfo.xml`) in the
  .deb and RPM: KDE Discover and GNOME Software now show the developer,
  description, release notes and the DriveSync icon
- Both packages refresh the desktop-database and hicolor icon cache on
  install/uninstall so the menu entry works immediately

### Changed
- Real project URL (github.com/MycoPixel/DriveSync) in the spec and
  metainfo

## [0.2.3] - 2026-11-18

### Added
- GUI Activity tab: newest-first list that keeps its scroll position and
  selection across refreshes, filters (action, level, time frame), a sort
  toggle and a clear-log button

## [0.2.2] - 2026-11-18

### Added
- GUI "Browse Drive…" folder picker in the Add-folder dialog (My Drive,
  Shared with me, shortcut resolution — the same flow as the dashboard)

## [0.2.1] - 2026-11-18

### Fixed
- GUI launch crash on GTK3 CSS parsing; timestamps shown in local time

### Added
- Google Docs/Sheets/Slides/Drawings export to docx/xlsx/pptx/png
- `uninstall.sh` mirrors `install.sh`; handles pip/deb/rpm installs
- Package-manager-aware tray dependency hints

## [0.1.0 – 0.2.0]

Initial development releases: three-way sync engine (local ↔ Drive ↔
base snapshot), OAuth device-less flow with the user's own Google client,
local web dashboard, GTK desktop app, background worker with a
cross-process lock, systemd user unit, .deb/.rpm/AppImage packaging.
Details in the git history.
