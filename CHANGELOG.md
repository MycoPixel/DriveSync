# Changelog

All notable changes to DriveSync, newest first. Dates are release dates.
Older entries below 0.2.1 are summarized from the git history.

## [Unreleased]

### Fixed
- **The AppImage launcher now opens the dashboard** — its `.desktop` entry
  ran `serve` without `--open`, so clicking the icon started the worker but
  showed nothing; it now runs `serve --open`. The AppImage also writes the
  `$APPIMAGE` path (not the temporary mount's interpreter) into the
  start-at-login entry, and the GTK window declares a stable WM class so
  the launcher and KDE's pinned icon associate with the running app
- **Ignore-pattern edits reach the inotify watcher without a restart** —
  the watcher re-reads `ignore_patterns` on its periodic pair refresh, so
  a newly ignored file no longer triggers a debounced sync pass
- **`drivesync conflicts list` accepts the pair id positionally** — as the
  help text promised (`conflicts list 2`), instead of silently listing
  every pair; `resolve` now rejects a missing path with a clear message
- **Launching DriveSync again restores the hidden window** — with "Stay in
  the system tray" enabled, closing the window hides it; on KDE Plasma the
  pinned launcher then starts a second process, which used to only show an
  "already running" dialog. A per-user activation socket now asks the
  resident GUI to present its window, so clicking the pinned icon brings it
  back (and the tray menu's "Open DriveSync" still works too)
- **`drivesync sync` no longer crashes at the end** — `Worker.run_pass()`
  now returns the pass totals and an ok flag; the CLI prints the aggregate
  summary and exits 2 when any pair failed
- **A Google Doc export can no longer overwrite a user's local file** —
  a same-named local file is only treated as our export when the base row
  proves it; otherwise the pair reports a collision and leaves both sides
  untouched
- **Re-authorizing no longer wipes saved OAuth client credentials** — only
  non-empty values from the form overwrite them
- **An emptied base snapshot is refused instead of re-deciding every path**
  (recovery stays available with `drivesync sync --full`)
- Repointing a pair at a different local or remote tree clears its stale
  base rows and markers
- **Dashboard API hardening** — a custom `X-DriveSync` header plus
  loopback-only `Host`/`Origin` checks block cross-site requests and DNS
  rebinding; `client_secret` is redacted in responses; user/file text is
  HTML-escaped in the UI; `config.json`/`state.db` are `0600` in a `0700` dir
- **Token lifecycle** — token responses carry `expires_at`, refreshes are
  locked and persisted only when they actually happen (server, GUI, worker
  and doctor), and re-auth falls back to an ephemeral loopback port
- **Retries** — 429/5xx and transient network failures back off with jitter
  and honour `Retry-After`; upload/download bodies are rewound before a
  retry; rate-limit and storage-quota 403s now get accurate hints
- **Shared Drives** — `supportsAllDrives` is sent for every Files call and
  `includeItemsFromAllDrives` for queries, so pairs in Shared Drives work
- **Worker reliability** — a pair that failed full-scans on its next pass
  while the changes token keeps advancing; `last_error` clears on recovery;
  pause/resume uses one loop and a pause event instead of thread recreation
- **Performance** — first-scan folder listing runs in parallel, md5 hashes
  are memoized per pass, SQLite runs in WAL/NORMAL with one commit per sync
  op, and multipart upload boundaries are random
- **Settings hot-reload** — the worker re-reads `config.json` each pass;
  only `server_port`, `auth_port`, `tray_enabled` and `watch_enabled` still
  need a restart

### Added
- **Themes now separate base palettes from style variants** — Light, Dark
  and System are the base themes; Liquid glass and Graphite each ship in
  explicit **light and dark** variants (`glass-light`, `glass-dark`,
  `graphite-light`, `graphite-dark`). The desktop Options tab and the
  dashboard group the two families, and configs still using the old
  `glass` / `graphite` keys load as their dark variants
- **Sync preview (dry-run) everywhere** — see exactly what a pass WOULD do
  before letting it happen: `engine.preview_pair()` runs the same scan and
  three-way decision as a real pass but stops before the apply phase (no
  transfers, no deletions, no base writes, no log rows), returning ops in
  pass order (mkdirs, renames, transfers, deletions) with the deletion
  count blocked by the safety limit reported explicitly. Surfaces as a
  "Preview changes…" dialog on the GUI Folders tab, a Preview button per
  pair on the dashboard, and `GET /api/pairs/<id>/preview` (ops capped at
  500 rows; counts always complete)
- **Per-pair "pause until tomorrow"** — a Folders-tab button and a
  dashboard button park one pair until tomorrow 09:00 local (stored as
  UTC ISO in the new `paused_until` pairs column); the worker skips paused
  pairs, auto-resumes with a logged `auto-resume` event when the deadline
  passes, and self-heals junk values; clicking again cancels the pause.
  Also settable via the dashboard PATCH (`paused_until`) and validated in
  `update_pair`
- **Conflict notifications now have an action** — when a conflict arrives
  while the window is hidden, the desktop notification carries a
  "Review conflicts" button that jumps to the pair and opens its review
  dialog (daemons without action support just show the plain message)
- **The dashboard is themeable and bilingual** — the same seven themes as
  the desktop app (Light, Dark, System, Liquid glass light/dark with real
  `backdrop-filter` blur, Graphite light/dark) and the same seven accent
  colors, chosen from a new "Appearance & language" card; one 🌐 button
  switches
  the whole dashboard to Spanish (~150 strings, same gettext-lite scheme
  as the GTK app). Choices persist per browser in localStorage; the
  System option keeps following the OS setting
- **Options tab in the desktop app** — everything personal lives in one
  place now:
  - **Theme**: Light, Dark, System (follows the desktop's dark preference
    via GNOME color-scheme or KDE `kdeglobals`), plus **Liquid glass** and
    **Graphite** style variants in explicit light and dark versions;
    applied instantly, persisted in `config.json`
  - **Accent color**: seven swatches (blue, teal, green, purple, pink,
    orange, red) tinting action buttons, the focused entry border, the
    active tab and the list-selection highlight
  - **Language / Idioma**: one button switches the whole interface between
    English and Spanish instantly — tabs, buttons, dialogs, column
    headers, tray menu and status lines re-label live (dialogs built on
    demand translate at creation; ~250 strings catalogued, with an AST
    coverage checker keeping call sites and the catalog in step)
  - **Startup toggles**: *Start DriveSync at login* writes/removes an XDG
    autostart entry (`~/.config/autostart/drivesync.desktop`, marker-keyed
    so a foreign launcher is never deleted); *Stay in the system tray*
    flips `tray_enabled` and starts/stops the tray icon live
  - **About**: the app version moved here — the window title is now just
    "DriveSync"
- **Version history before a pull overwrites a file** — the previous local
  version is copied to
  `~/.local/share/drivesync/history/<pair>/<path>/` (UTC-stamped name,
  capped at `history_versions`, default 10 per file, oldest pruned; 0
  disables). Restores are a plain file copy. Uploads need no counterpart:
  Drive keeps its own revisions, and deletes already land in the trash
- **Conflict copies are manageable from the terminal** —
  `drivesync conflicts list [--pair N]` and
  `drivesync conflicts resolve <copy-path> [--pair N] --keep
  copy|original|both`, sharing the dashboard's review engine so headless
  setups get the same three resolutions
- **Drive-wide search** — the desktop **Drive** tab's filter box now
  searches every folder in the account on Enter (`name contains`, folders
  first, double-click a result to browse it), and the dashboard's folder
  picker has a search field that finds a folder anywhere when binding a
  pair (`GET /api/drive/<account>/search`)
- **Dark mode for the dashboard** — the web UI follows
  `prefers-color-scheme: dark`, matching the GTK window's theme behavior
- **Drive storage quota is visible** — the desktop **Accounts** tab gained
  a Storage column ("12.4 GB / 15 GB used", fetched in the background), and
  the dashboard's account cards show the same usage
- **Per-pair symlink policy** (`skip` by default, `follow` optional) —
  links are no longer stat-ed as their target: a symlink to a file outside
  the tree can no longer be uploaded as if it were the file, and a linked
  folder can no longer look "empty" (which read as mass deletions).
  `follow` syncs link targets, guarded by a (st_dev, st_ino) set so a link
  loop cannot diverge the walk
- **Case-insensitive pair mode** — pairs on FAT/exFAT/NTFS folders can
  enable it in the pair editor (`case_insensitive`): paths differing only
  in letter case compare as one (local on-disk spelling wins, parents
  canonicalized before children), so case-only renames never churn and two
  same-case Drive files are reported instead of colliding on disk
- **state.db auto-backup + recovery** — every clean pass (rate-limited to
  one per 10 minutes) snapshots the database to `state.db.bak` via
  SQLite's online backup API; on the next start a corrupt or missing
  `state.db` is restored from that backup automatically and reported in
  the activity log instead of stopping the app
- **systemd watchdog** — the service unit ships `Type=notify` +
  `WatchdogSec=180`; the worker sends `READY=1` on start and `WATCHDOG=1`
  while alive (heartbeat fed by the pass loop, the idle wait — sliced so
  an hours-long sync interval keeps pinging — and every transfer chunk,
  so slow but moving uploads/downloads are never mistaken for a hang).
  A genuinely hung worker now gets restarted instead of stalling sync
  silently; outside systemd everything stays a no-op
- **Choose the sync scope before the first sync** — the desktop app's
  **Add folder…** dialog and the dashboard's **Sync scope** field list the
  Drive folder's subfolders up front and save the choice with the pair, so
  the first pass never downloads folders you did not pick. The desktop
  dialog can also create the pair **paused** for review. Existing pairs can
  still change scope from **Subfolders…** / the pairs table
- Desktop GUI conveniences: **Open local folder** (and double-click on a
  pair) opens it in the file manager; pause/resume in the Activity tab;
  activity **search** plus a detail dialog on double-click; Drive-tab
  **filter**, **My Drive** jump and **Select none**; a Cloud Console link
  in Setup; and an empty-pairs hint explaining the pre-sync scope choice
- Dashboard Settings now exposes the bandwidth cap, sync window,
  `watch_enabled` and `incremental_scan`
- Pair creation validates that the remote id is a folder and rejects
  duplicate or nested local roots; `allow_deletes` is accepted on create
- CI covers Python 3.9/3.11/3.12/3.14 and runs a fatal-error lint pass;
  `pyproject.toml` declares `watch`/`tray`/`gui` optional dependencies
- **Conflict-copy review** — files kept as `name.conflict-…` copies by the
  keep-both policy can now be resolved from the desktop app (**Conflicts…**
  on the Folders tab) and the dashboard (a **Conflicts** button per pair):
  keep the copy (it wins at the original path on both sides), keep the
  original (the copy is discarded everywhere), or leave both. Backed by
  `drivesync.conflicts`, a `GET/POST /api/pairs/<id>/conflicts` API, and a
  log entry per resolution
- **Pair editing in the UI** — direction, conflict policy and the deletes
  switch are editable from the desktop app (**Edit…**) and the dashboard
  (an **Edit…** modal per row) instead of CLI-only
- **Per-pair last-sync status** — the Folders table (desktop) and both pair
  tables (dashboard) show each pair's last pass: time ago, ok/ERROR, change
  counts and pending conflicts, straight from the worker's status
- **Global pause + next-run countdown** — a Pause/Resume sync button lives
  in the desktop headerbar (reachable from every tab) and the subtitle
  counts down to the next scheduled pass; the dashboard worker card shows
  the same next-run time
- Dashboard activity gained a search box (scans up to 1000 recent entries)
  and a click-to-open detail dialog for every row
- The tray's Pause item now reads the real state ("Pause background sync" /
  "Resume background sync") on both tray backends

### Changed
- **Stable package names** — the RPM no longer carries a distro tag
  (`%{?dist}`), so every build produces `drivesync-<ver>-1.noarch.rpm`
  instead of a host-specific name; the spec now installs into
  `/usr/lib/.../site-packages` (not `/usr/local/lib`) and requires the GTK 3
  typelib; the deb depends on `gir1.2-gtk-3.0`; the AppImage uses the right
  icon path and includes `watchdog`
- **Tray icon fixed** — the desktop app prefers the native
  AppIndicator binding and only falls back to pystray where it can run
  safely, so closing the window no longer hides it behind an icon that
  never appears
- **AppImage bundles its own CPython** (python-build-standalone) by default,
  so the image no longer depends on the build host's python version;
  `DS_USE_HOST_PYTHON=1` restores the old distro-python behaviour (needed
  only if you want the GTK window inside the AppImage)
- **Single version source** — `drivesync/__init__.__version__` is the one
  place a release number is written; `pyproject.toml` derives the package
  version from it and all build scripts already read it from there

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
