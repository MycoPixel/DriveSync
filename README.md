# DriveSync

**A Google Drive sync client for Linux desktops.** DriveSync keeps folders on
your machine in two-way sync with your Google Drive — plus upload-only and
download-only modes — with a local web dashboard for accounts, sync pairs,
activity and settings.

It is an original, open-source alternative to commercial Drive clients
(inspired by what Insync offers, but independent and MIT-licensed): your
tokens and files stay on your machine and talk to Google directly.

## Features

- **Two-way sync** with three-way reconciliation (local ↔ Drive ↔ last-synced
  snapshot), so deletes, edits, new files and new folders propagate correctly
- **Google Docs conversion** — Docs, Sheets, Slides and Drawings are downloaded
  as `.docx`, `.xlsx`, `.pptx` and `.png` files you can open on your desktop;
  edits on either side sync back (see "Google Docs conversion" below)
- **Per-pair direction**: `bidirectional`, `upload` (local → Drive), or
  `download` (Drive → local)
- **Conflict policies**: keep the newer file, always local, always remote, or
  keep both copies (the loser is saved as `name.conflict-<timestamp>.ext`);
  copies left by the keep-both policy can be reviewed afterwards — keep the
  copy, the original, or both — from the desktop app's **Conflicts…** button
  or the dashboard's per-pair **Conflicts** button
- **Multiple Google accounts**, multiple clouds folders, many pairs at once
- **Rename detection** — moving/renaming locally or on Drive updates the
  other side without re-uploading
- **Selective sync** — per pair: choose which subfolders mirror; the rest
  stays on Drive and on disk untouched, and can be fetched any time from the
  Drive tab (nothing is deleted by narrowing a scope)
- **On-demand downloads** — the desktop app's Drive tab lists your whole
  Drive (My Drive, Shared with me and Shared drives) like a virtual drive
  without the mount: browse, then download a file or a whole folder to any
  destination, on your say-so, without creating a sync pair
- **Shared Drives** — organizational drives appear as folder roots in the
  dashboard, the desktop Drive tab and the pair picker; a drive itself can
  be a sync target, and every Files call opts into Shared Drives
- **Portable settings** — `drivesync config export` / `import` moves your
  preferences and pair definitions to another machine; OAuth tokens and the
  client secret are never included, and the receiving machine authorizes
  its own account
- **Optional read-only FUSE mount** — `drivesync mount ~/Drive` exposes a
  Drive folder as a normal filesystem for apps that cannot use the browser
  (needs the optional `fusepy` + libfuse; writes return `EROFS`)
- **Dolphin/KDE context menu** — right-click a folder to sync it now or
  open the add-pair dialog pre-filled with it
- **Full Drive browser** — a dedicated Drive tab in the dashboard (and a
  richer desktop Drive tab): clickable breadcrumbs with the folder ID on
  show, Name/Type/Size/Modified/Owner columns, filter by file type, sort
  by name/date/size, a Drive-wide name search, and one-click "use this
  folder" to send any folder to the sync-pair form — no more copying IDs
  out of the Drive web URL
- **Resumable transfers** — large uploads use Google's resumable-session
  protocol with the session persisted between passes, downloads keep
  their progress in a `.drivesync.part` file, and transient failures are
  retried with backoff — a broken connection costs the missing bytes, not
  the whole transfer
- **Daily digest** — once a day at a time you pick, a notification
  summarizing what synced (pushed, pulled, deleted, conflicts,
  errors); quiet days can stay quiet
- **Metered-connection awareness** — on a mobile hotspot or capped
  plan, automatic syncing pauses (or throttles to a safe rate) until
  the network is unmetered; manual syncs are always your call
- **Peak/off-peak bandwidth, globally or per folder** — give sync a
  slower cap during the hours you need the line for work, unlimited or
  faster the rest of the day; the bucket swaps at the window boundary
  without a restart, and any pair can override the global caps with its
  own window and limits
- **Consistent desktop and web interfaces** — plan reasons, transfer
  sizes and status colors render from one shared presentation module, so
  the GTK app and the web dashboard never drift apart
- **Explainable sync plans** — the sync pass runs as scanner -> planner ->
  executor, and the pair's Preview dialog shows every planned operation
  with the reason it was planned ("local changes since last sync",
  "deleted on Drive — propagating locally", "conflict — policy 'newer'
  keeps the Drive version (newer)") and the payload size for transfers,
  before anything touches disk or Drive
- **Recovery interface** — a Recovery page in the dashboard (plus a
  `drivesync recovery` command) that shows what the safety nets caught and
  how to get files back: held mass-deletions to confirm or keep, conflict
  copies, the recoverable local trash, Google's own Drive trash, previous
  versions kept before overwrites, and a decision history. Nothing is ever
  destroyed outright: local deletions move to a trash folder, remote ones
  to Drive's trash, overwritten bytes into version history — and a mass
  deletion is never propagated until you confirm it
- **Selective sync via ignore patterns** (glob style, per app)
- **Activity log** with conflicts and errors surfaced in the dashboard —
  auto-pruned (30 days, capped at 5,000 rows) so the state database stays small
- **Pair management in the UI** — add, pause/resume, edit direction and
  conflict policy, and watch each pair's last-sync status (time, ok/error,
  change counts) directly in the Folders table / pairs tables
- **Background worker** on a timer + "Sync now" button; systemd user unit
  for autostart
- **Instant local-change sync** — an inotify watcher (watchdog) pushes local
  edits in about a second; polling remains the fallback, so nothing breaks
  without it
- **Desktop notifications** when new conflicts or errors arrive while the
  desktop app is hidden to the tray
- **Bandwidth cap + sync window** — limit transfers (`bandwidth_limit_kb`)
  and restrict background syncing to a daily window (`active_from`/`active_to`)
- **Local dashboard** at `http://127.0.0.1:5678` — bound to localhost only

## Requirements

- Linux (anything with Python 3.9+; developed and tested on x86-64)
- Python 3.9+, `pip`/`venv`
- A Google account

## Install

From a package (recommended — no terminal needed beyond the install command,
and the software center can do even that):

```bash
sudo apt install ./drivesync_0.6.1-1_all.deb      # Debian/Ubuntu/Mint
sudo dnf install ./drivesync-0.6.1-1.noarch.rpm   # Fedora/RHEL/openSUSE
```

Packages install the app user-locally (`~/.local/share/drivesync`, venv
included), put `drivesync` on your PATH, add a menu launcher, and drop a
systemd user unit (`systemctl --user enable --now drivesync` to autostart).
Build them yourself with `packaging/build-deb.sh` and `packaging/build-rpm.sh`
(see `packaging/README.md`), or grab them from the releases page.

There is also a zero-install AppImage (`packaging/build-appimage.sh`) that
bundles its own CPython, so the CLI, dashboard and tray run anywhere the
glibc is new enough. Its `gui` command opens the native GTK window using the
desktop's PyGObject/GTK 3 (the one piece a bundled interpreter cannot load),
and prints the install command if they are missing; running the AppImage with
no arguments (or its menu entry) opens that window. For a fully self-contained
install, use the deb/rpm.

For a sandboxed install, `packaging/build-flatpak.sh` builds a Flatpak on
`org.gnome.Platform` (see `packaging/README.md`). It keeps data under
`~/.var/app/io.github.drivesync.DriveSync` and uses the desktop portal for
file pickers; enable autostart from the dashboard instead of systemd.

The desktop window remembers its size and last tab. Useful shortcuts:
`Ctrl+R` sync all, `Ctrl+F` search the current list, `Ctrl+,` open Options,
`F5` refresh the current view, and `Ctrl+Q` quit.

From source:

```bash
cd drivesync
./install.sh
```

Or manually:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m drivesync serve
```

Uninstalling keeps your sync data (`~/.local/share/drivesync`) by default:

```bash
./uninstall.sh            # remove the app, keep config/tokens/sync state
./uninstall.sh --purge    # remove everything, including your sync data
```

## One-time setup: your own Google OAuth client

DriveSync talks to Google with **your own** OAuth credentials — no third
party ever sees your tokens.

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) →
   create/select a project.
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **Google Auth Platform → Branding** → fill in the app name (any name, e.g.
   "DriveSync (personal)"), your support e-mail and the developer contact
   e-mail. Then fill in the three fields Google requires before it lets you
   publish, even though they are not marked with a red `*` while the app is in
   Testing: **Application home page**, **Application privacy policy link**
   and **Authorized domains** (see [Publishing the app](#publishing-the-app)
   below). Leave **App logo** and **Application terms of service link**
   empty.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**
   → type **Desktop app**.
5. Copy the **Client ID** and **Client secret**.

Then connect an account:

```bash
drivesync auth    # asks for client ID/secret once, then opens Google consent
```

The first run pastes the **Client ID** and **Client secret** into the terminal
(secret input is hidden) — DriveSync saves them and never asks again. A
browser tab then opens for you to approve access; on the "unverified app"
warning (normal for personal test apps), click *Continue*. Tokens are stored
in `~/.local/share/drivesync/`.

…or enter the credentials in the dashboard's **Accounts** tab instead — it
stores them and starts the same consent flow in one step.

### Publishing the app

Google expires the sign-in of apps whose consent screen is in **Testing**
after **7 days**, so DriveSync would ask you to *Reconnect* every week (it
shows "sign-in expired" and a Reconnect button in the dashboard). Setting the
status to **In production** removes that expiry. You do **not** need to buy a
domain, build a website or submit the app for Google verification — you only
need to get past the console's checks:

1. On the **Branding** page, fill in these three fields with any free public
   links on the *same* domain, for example this project's pages on GitHub:
   - **Application home page:** `https://github.com/MycoPixel/DriveSync`
   - **Application privacy policy link:**
     `https://github.com/MycoPixel/DriveSync/blob/master/PRIVACY.md`
   - **Authorized domains → Add domain:** `github.com` (just the domain: no
     `https://`, no slashes)

   A GitHub profile or a public Google Doc works the same way. Keep **App
   logo** empty: uploading one makes Google require verification.
2. Open **Audience** and click **Publish app** → **Confirm**. The status
   becomes **In production**.
3. Reconnect your account in DriveSync once (a sign-in created while the app
   was in Testing keeps its 7-day expiry).

When you sign in, Google shows an "unverified app" warning: click *Advanced →
Continue*. Unverified apps are limited to 100 users — you are the only one.
Your credentials are still your own and stay on your machine.

> The console's rules change from time to time; these are the steps that
> worked at the time of writing (November 2026).

## Usage

### Add your first sync pair

```bash
# find the Drive folder ID in its web URL: .../folders/<ID>
drivesync pairs add ~/DriveDocuments --remote-id 1a2B3cD4eF5g \
    --remote-path "My Drive/Documents" \
    --include "Documents/Projects,Documents/Archive"
```

`--include` (or **Folders → Subfolders…** in the desktop app) turns on
selective sync: only the chosen subfolders mirror; loose files in the pair
root always sync. Everything else stays on Drive and on disk untouched and
can be downloaded on demand from the Drive tab. The desktop app's **Add
folder…** dialog and the dashboard's **Sync scope** field let you choose
that scope *before* the pair exists — so the very first pass already skips
unwanted folders (the desktop dialog can also create the pair paused for
review).

or use **Sync pairs → Add a sync pair → Browse…** in the dashboard, which
lets you click through your Drive folders, or open the dashboard's
**Drive** tab, navigate (or search) to the destination folder and press
**"Use this folder for a new sync pair"**. In the desktop app the Drive
tab's **"Sync this folder…"** button does the same job: the Add-folder
dialog opens with the account, folder ID and display name already filled.

### Run it

```bash
drivesync gui                   # desktop app: setup, sign-in, folders, activity
drivesync serve                 # foreground headless: dashboard + worker
drivesync serve --open          # same, and it opens the dashboard in a browser
systemctl --user enable --now drivesync   # or as a login service
```

The desktop app (`drivesync gui`) is what the menu launcher opens: one GTK
window with Setup / Accounts / Folders / Drive / Activity / Options tabs,
so a fresh
machine is configured entirely without the browser — except Google's own
consent page, which must open in a browser once per account (that step
happens on google.com by design). Adding a pair needs no manual folder IDs:
the “Browse Drive…” button walks My Drive and Shared-with-me folders — the
same picker the dashboard uses. The **Drive tab** is the on-demand side:
it lists your whole Drive with sizes and dates and downloads any file or
folder to wherever you point it — no pair needed, like Google Drive on
Windows minus the mount. The **Folders** tab lists pairs with their scope;
**Add folder…** can pick subfolders up front (and start paused) so the first
sync is already selective, and **Subfolders…** changes the scope later.
`serve` and the systemd unit are the headless/service mode;
running both is safe — a cross-process lock means only one syncs at a
time. Headless machines without GTK: `drivesync gui` prints the package to
install and exits.

### Options tab: themes, colors, language, startup

The **Options** tab holds the personal settings:

| Option | Choices | Notes |
| --- | --- | --- |
| Theme | Light, Dark, System; style variants Liquid glass light/dark, Graphite light/dark | Base themes and decorative styles are separate; System follows the desktop's dark preference (GNOME color-scheme, with GTK_THEME fallback); applied and persisted instantly |
| Accent | blue, teal, green, purple, pink, orange, red | Tints action buttons, focused entry borders, the active tab and list selection |
| Language | English ⇄ Español | One button, live relabeling — dialogs translate as they open; the tray menu is rebuilt |
| Start at login | on/off | XDG autostart entry in `~/.config/autostart/drivesync.desktop` |
| Stay in the system tray | on/off | Closing the window keeps the app running behind the tray icon (off = closing exits). Launching DriveSync again — including clicking a pinned KDE launcher — brings the hidden window back |

The app version lives on the Options tab's **About** card, not in the
window title. Themes and accents are plain config keys (`theme`,
`accent`, `language`, `start_at_login` in `config.json`), so `drivesync
config set` works too — e.g. `drivesync config set theme=glass-dark`.
The old `glass` / `graphite` names still load and map to their dark variants.

Open <http://127.0.0.1:5678> — status, pairs, activity and settings. The
dashboard matches the desktop app now: an **Appearance & language** card
picks the same base themes and style variants (Light, Dark, System, Liquid
glass light/dark, Graphite light/dark)
and the same seven accent colors, and a 🌐 button switches the interface
to Spanish. Preferences are stored per browser (localStorage), so the
desktop app and the dashboard can look different if you like. Each pair
row has a **Preview** button — a dry-run listing exactly what the next
sync would change (uploads, downloads, deletions, renames, in pass order,
nothing applied) — and an **Until tomorrow** button that parks that pair
until tomorrow 09:00; the pause auto-expires with a logged event, and
clicking again cancels it.

### CLI reference

```
drivesync gui                         native desktop app (GTK window)
drivesync serve [--open]              dashboard + background worker
drivesync auth                        authorize a Google account
drivesync accounts list|remove
drivesync pairs add|list|set|remove   manage sync pairs
                                      (--include docs,photos = selective sync)
drivesync sync [--pair N|--path DIR] [--full]
                                      run one sync pass now and exit
drivesync status                      accounts, pairs, recent activity
drivesync conflicts list|resolve      review/resolve .conflict-… copies
                                      (resolve <path> --keep copy|original|both)
drivesync config get|set              interval, conflict policy, ignores
drivesync config export|import FILE   move settings + pairs between machines
                                      (--dry-run shows the import first)
drivesync recovery status|trash|…     deletion holds, trash, versions, history
drivesync mount DIR [--remote-id ID]  read-only FUSE mount (needs fusepy)
drivesync audit [--fix]               credential/file-permission check
drivesync doctor                      sanity-check the installation
```

### Read-only FUSE mount (optional)

`drivesync mount ~/Drive` exposes My Drive (or `--remote-id` a folder or a
Shared Drive) as a normal read-only filesystem, for apps that only know
about files. It needs `fusepy` and libfuse; neither is required by the rest
of the app:

```bash
sudo apt install fuse libfuse2        # Fedora: sudo dnf install fuse fuse-libs
pip install 'drivesync[fuse]'         # from a source checkout: pip install '.[fuse]'
drivesync mount ~/Drive --remote-id <folder-id>
```

The command blocks while the mount is active (Ctrl-C unmounts). Writes fail
with `EROFS`, Google-native documents are exported on first open, and
listings are cached for a few seconds so `ls`/`find` do not hammer the API.

### Version history (before a pull overwrites a file)

Pulls replace local bytes, and Drive keeps no trace of what used to be
there — so before a pull overwrites a tracked file, the previous version is
copied to `~/.local/share/drivesync/history/<pair>/<path>/` (named with a
UTC timestamp, capped at **10 versions per file**, oldest pruned; 0 in
**Settings → Kept versions per file** turns it off). Restoring is a plain
file copy back — or make that the local side and the next pass pushes it.
Uploads never need this: Drive keeps its own revision history for every
file, and deletes already land in DriveSync's recoverable trash.

### Recovery: when deletions are held

A pass that would delete more than `delete_safety_limit` files (default 10)
is stopped before anything happens: the deletions are listed on the
dashboard's **Recovery** page (and `drivesync recovery status`), and you
choose — **Allow these deletions (one pass)** applies them exactly once,
**Keep my files (deletes off)** switches the pair to deletes-off so files
are kept on both sides (re-enable from the pair editor). The hold survives
restarts and releases itself if the risky pass resolves. Everything the app
keeps for you is restorable from that page: the local trash (with the pass
stamp), Google Drive's trash, and the previous versions kept before an
overwrite pull.

### Per-pair hardening options (pair edit dialog / `drivesync pairs set`)

| Option | Default | Meaning |
| --- | --- | --- |
| Symlinks | `skip` | Links are left out of the pair — a link to a file outside the tree would otherwise be uploaded as if it were the file. `follow` syncs what they point at, guarded against link loops |
| Case-insensitive | off | For pairs on FAT/exFAT/NTFS folders: paths matching only by letter case compare as one path (the local on-disk spelling wins), so case-only renames do not churn and same-case Drive files cannot collide locally |

### Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| Sync interval | 30 s | How often pairs are rescanned |
| Conflict policy | `newer` | Who wins when both sides changed |
| Ignore patterns | `*.tmp`, `*.swp`, … | Globs skipped on both sides |
| Convert Google Docs | on | Native docs downloaded as Office files |
| Incremental scan | on | 1–2 API calls per pass via the changes feed |
| Watch local changes | on | Instant push via inotify (needs watchdog) |
| Kept versions per file | 10 | Old local versions saved before a pull overwrites; 0 = off |
| Bandwidth limit | 0 (unlimited) | KB/s cap on uploads + downloads combined |
| Sync window | always | `active_from`/`active_to` HH:MM; overnight works |
| Server port | 5678 | Local dashboard port |

Most settings are picked up on the **next sync pass** — the worker re-reads
`config.json` before every pass, so changes made in the dashboard or with
`drivesync config set` do not need a restart. These are read only at startup
and still need one: `server_port`, `auth_port`, `tray_enabled` and
`watch_enabled` (the watcher and tray are created when the app launches).

### Google Docs conversion

Google Docs/Sheets/Slides/Drawings are not stored as normal files on Drive —
they have no bytes to download directly. DriveSync exports them through the
Drive API into formats your desktop apps can open:

| Drive type | Exported as | Default format |
| --- | --- | --- |
| Google Docs | `Report.docx` | DOCX (Word) |
| Google Sheets | `Budget.xlsx` | XLSX (Excel) |
| Google Slides | `Deck.pptx` | PPTX (PowerPoint) |
| Google Drawings | `Logo.png` | PNG |

- **Edits made in the browser** re-export on the next sync pass (the file
  version bump is the change signal — exports are not byte-stable, so the
  engine never compares them by checksum).
- **Edits made locally** (e.g. in LibreOffice) are uploaded back into the
  same Google doc — Drive converts the content on update.
- Renaming the local file renames the Drive doc; renaming on Drive produces
  a fresh export under the new name.
- Deleting the local `.docx` trashes the Google doc, and vice versa, exactly
  like any other synced file. Local deletions always land in the recoverable
  trash dir first.
- Files with no meaningful offline format (Forms, shortcuts) are tracked but
  not downloaded.

Turn conversion off (docs stay cloud-only and are just tracked) in the
dashboard **Settings** tab or with:

```bash
drivesync config set convert_google_docs=false
```

Per-type format overrides (advanced):

```bash
drivesync config set 'export_formats={"document": "pdf", "spreadsheet": "ods"}'
```

Accepted short names: `docx odt rtf pdf txt epub html` (Docs),
`xlsx ods csv tsv pdf` (Sheets), `pptx odp pdf` (Slides), `png svg jpeg`
(Drawings).

## How sync works

Every pass builds three views of each pair: the current local tree, the
current Drive tree, and the *base* snapshot from the previous pass (SQLite at
`~/.local/share/drivesync/state.db`). Comparing all three yields the classic
outcomes — local change pushes, remote change pulls, both-changed is a
conflict handled by the policy, and one-sided deletions propagate unless the
other side changed since the base (that's a conflict too, so edits are never
silently destroyed).

**Incremental remote scans.** After the first (full) pass, each account keeps
a Drive *changes feed* token. Later passes rebuild the remote view from the
base snapshot plus the feed — one or two API calls per pass, regardless of
how many files or folders the pair has — so editing one file in a pair of
50,000 syncs in seconds, not minutes. Structural changes the feed cannot
express (a tracked folder renamed or moved, a whole folder arriving from
outside the pair) automatically fall back to one full scan; first runs and
disabled incremental scans always list the whole tree. Turn it off with:

```bash
drivesync config set incremental_scan=false
```

## Desktop app & tray

`drivesync gui` (also the menu launcher) is a resident desktop app: it runs
the sync worker, shows a Material-styled window, and keeps a system tray
icon when pystray is installed (`sudo dnf install python3-pystray`). Closing
the window only hides it — the tray icon stays, with **Open**, **Sync now**,
**Pause/Resume** and **Quit**. A **Pause/Resume sync** button also lives in
the headerbar (with a next-run countdown in the subtitle), reachable from
every tab. Without pystray, closing the window exits the app. A second
launch while it runs shows an "already running" notice instead of opening a
duplicate. While the window is hidden, new conflicts or errors surface as
desktop notifications (libnotify, best-effort — without it the Activity tab
is still the source of truth).

## Troubleshooting

Start with `drivesync doctor` — it checks dependencies, credentials and the
Drive API, and names the fix for each problem it finds. The common ones:

**Dashboard port busy** — the systemd service may already be running; its
dashboard is then already at <http://127.0.0.1:5678>. Stop it with
`systemctl --user stop drivesync`, or move the manual instance with
`drivesync config set server_port=5679`. `drivesync serve` prints this
instead of a traceback.

**Two copies installed (pip + package)** — a `pip install` copy lives in
`/usr/local/lib/python3.*/site-packages` and *shadows* the distro package's
copy in `/usr/lib/...`: the classic symptom is `drivesync --version` not
changing after a package upgrade. Remove the stray copy with
`python3 -m pip uninstall drivesync` (add `sudo` if it was installed as
root), or simply run `./uninstall.sh`.

**Tray icon missing** — needs `pystray` + `pillow` in the venv; on KDE
Plasma also `libayatana-appindicator-gtk3`. `drivesync serve` prints what
it lacks and how to install it.

**Transfers fail with TLS errors** — the venv's CA bundle is broken; the
message `drivesync serve` prints includes the exact commands to rebuild
the venv.

**Desktop app won't open** — `drivesync gui` needs GTK 3 bindings:
`sudo dnf install python3-gobject` (Fedora) or `sudo apt install python3-gi`
(Debian). On a headless machine, use `drivesync serve` and the dashboard.

## Security

OAuth tokens live in `state.db` (`0600`, data directory `0700`) and the
dashboard is loopback-only with a custom-header CSRF check. Check an
installation with:

```bash
drivesync audit          # file modes, tokens, world-writable files
drivesync audit --fix    # repair insecure permissions
```

The full threat model, controls and private reporting process are in
[SECURITY.md](SECURITY.md). `drivesync config export` never writes tokens
or the client secret, and the FUSE mount is read-only by construction.

## Current limitations (honest list)

- **Remote changes are polled** — local edits push instantly via the
  inotify watcher, but changes made on Drive arrive on the next scan
  (default 30 s)
- **Google-native exports are re-downloaded in full** when the doc changes —
  a large Sheet edited in the browser transfers its whole export again
- **Local mtimes must differ for change detection** — if an editor writes a
  same-size file with the same mtime, run `drivesync sync --full` (re-lists
  Drive and rehashes local files regardless of mtime)
- No end-to-end encryption
- The dashboard is localhost-only by design; don't expose it

## Development

```bash
python3 -m pytest tests/        # engine, store, config, CLI, API, GUI
drivesync doctor                # environment check
```

The engine's decision step is a pure function (maps in, ops out) tested
against a mock Drive — see `tests/test_engine.py` for the full matrix
(uploads, downloads, conflicts, deletes, renames, directions, ignores).

## Changelog

Release history and what changed in each version:
[CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Google or any commercial
Drive client.
