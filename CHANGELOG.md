# Changelog

All notable changes to DriveSync, newest first. Dates are release dates.
Older entries below 0.2.1 are summarized from the git history.

## [0.5.1] - 2026-11-18

### Fixed
- **Drive-tab downloads now show what is happening** — the old build gave
  no visible feedback: only the small headerbar subtitle blinked, so a
  running download looked like nothing happened (or a frozen app). The tab
  now has a live progress strip: a spinner plus a per-file line
  ("downloading 1/2: notes.md"), the headerbar subtitle mirrors it, and the
  strip keeps the result summary (green, or red on failure) after the job
  finishes. Folder downloads stream per-file progress while the tree
  downloads (`download_tree` gained a `progress` callback and now reports
  as each file completes instead of only at the end)
- **A second download no longer silently does nothing** — while one
  download was running, every click on "Download selected…" /
  "Download this folder…" / a file row was swallowed with no message until
  the app was restarted. Downloads are now counted, not boolean-locked:
  starting another one runs it concurrently and the strip shows how many
  are in flight
- **Selected rows are highlighted on every system theme** — the Drive list
  had no explicit selection styling, so whether a click produced a visible
  highlight depended on the theme (Adwaita's `.view:selected` rule won the
  cascade; some themes showed no highlight at all). Selected rows now use
  a Material-3 tonal fill (`treeview.view:selected`) with a backdrop
  variant, verified pixel-wise under Xvfb
- Added a **Select all** button to the Drive tab for bulk downloads
- **The RPM spec builds again** — `Version:` was hardcoded (so `%prep`
  looked for the old `drivesync-0.5.0.tar.gz` and failed) and the
  changelog date macro lost its percent-escapes, which rpm read as
  macros and produced `Wed Nov  2026` → "bad date in %changelog". The
  spec now carries 0.5.1 with a correct date macro, and `build-rpm.sh`
  injects the real version into the spec on every build (plus a warning
  when the top changelog entry lags the package version)

## [0.5.0] - 2026-11-18

### Added
- **On-demand downloads (the Drive tab)** — the desktop app now lists your
  whole Drive (My Drive + Shared with me) with kind, size and date, and
  downloads any file — or a whole folder tree — to a destination you pick,
  without creating a sync pair. Google Docs are exported with the
  configured format; unconvertible types are skipped and reported, never
  silently dropped. This is the "Google Drive on Windows" experience
  without the filesystem mount: everything is visible, download happens
  when you say so
- **Selective sync per pair** — Folders → Subfolders… (or
  `drivesync pairs add --include docs,photos` / `pairs set
  include_patterns=…`) restricts a pair to chosen subfolders. Excluded
  subtrees are never downloaded, uploaded or tracked; narrowing a scope
  deletes nothing (both copies stay, the base rows are forgotten);
  widening one downloads the subfolder on the next pass. Loose files in
  the pair root always sync. Works with full and incremental scans, the
  dashboard API (`include_patterns`) and the CLI
- **Dashboard API**: `GET /api/drive/<account>/children` lists files AND
  folders of any Drive folder (the on-demand browser feed)
- The GUI now shows the pair scope in the Folders tab and logs scope
  changes, on-demand downloads, skips and failures to the activity log

### Fixed
- **Desktop app opened on the Setup tab on every launch** — GTK ≥ 3.24.49
  silently ignores `Gtk.Notebook.set_current_page()` before the window is
  shown; the startup tab is now applied after `show()` so a configured
  user lands where they left off (Setup → Accounts → Activity logic kept)
- A window that fails to build no longer dies invisibly: the traceback is
  printed, appended to `gui-crash.log` in the data dir and shown in a
  dialog, so menu launches are diagnosable
- Hand-edited `config.json` values (e.g. a quoted sync interval) can no
  longer crash the desktop app at launch — widget inputs are coerced and
  degrade to defaults
- The Drive tab lists Drive lazily (on first view) and shows listing
  errors inline in the tab instead of popping a dialog at startup

## [0.4.0] - 2026-11-18

### Added
- **Instant local-change sync**: the worker watches each pair's local tree
  with inotify (watchdog) and pushes edits in about a second instead of
  waiting for the scan interval. Optional dependency — without `watchdog`
  the periodic scan still applies exactly as before
  (`drivesync config set watch_enabled=false` disables it)
- **Desktop notifications** in the tray-resident desktop app: new
  conflicts/errors pop a libnotify notification while the window is hidden
  (at most one per 5 minutes; anything already seen in the Activity tab
  never repeats)
- **`drivesync sync --full`**: re-list Drive and rehash local files even
  when size+mtime look unchanged — the recovery for editors that save with
  an unchanged mtime
- **Bandwidth throttle**: `bandwidth_limit_kb` caps uploads + downloads
  combined (token bucket; applies to downloads, uploads and Google Docs
  exports); `0` = unlimited (default)
- **Sync schedule window**: `active_from`/`active_to` (HH:MM) restrict
  background passes to that window; overnight windows ("22:00"–"06:00")
  work, and explicit `drivesync sync` runs are never schedule-blocked
- `*.drivesync` temp files ignored by default (download scratch files no
  longer poke the watcher)

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
