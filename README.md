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
  keep both copies (the loser is saved as `name.conflict-<timestamp>.ext`)
- **Multiple Google accounts**, multiple clouds folders, many pairs at once
- **Rename detection** — moving/renaming locally or on Drive updates the
  other side without re-uploading
- **Selective sync via ignore patterns** (glob style, per app)
- **Activity log** with conflicts and errors surfaced in the dashboard
- **Background worker** on a timer + "Sync now" button; systemd user unit
  for autostart
- **Local dashboard** at `http://127.0.0.1:5678` — bound to localhost only

## Requirements

- Linux (anything with Python 3.9+; developed and tested on x86-64)
- Python 3.9+, `pip`/`venv`
- A Google account

## Install

From a package (recommended — no terminal needed beyond the install command,
and the software center can do even that):

```bash
sudo apt install ./drivesync_0.2.0-1_all.deb      # Debian/Ubuntu/Mint
sudo dnf install ./drivesync-0.2.0-1.noarch.rpm   # Fedora/RHEL/openSUSE
```

Packages install the app user-locally (`~/.local/share/drivesync`, venv
included), put `drivesync` on your PATH, add a menu launcher, and drop a
systemd user unit (`systemctl --user enable --now drivesync` to autostart).
Build them yourself with `packaging/build-deb.sh` and `packaging/build-rpm.sh`
(see `packaging/README.md`), or grab them from the releases page.

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

## One-time setup: your own Google OAuth client

DriveSync talks to Google with **your own** OAuth credentials — no third
party ever sees your tokens.

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) →
   create/select a project.
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **APIs & Services → OAuth consent screen** → External → fill the app name
   and your email → add yourself as a **test user**.
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

> While your OAuth consent screen is in "testing" mode, only the users you
> added as test users (i.e., you) can authorize — which is exactly what you
> want for personal use.

## Usage

### Add your first sync pair

```bash
# find the Drive folder ID in its web URL: .../folders/<ID>
drivesync pairs add ~/DriveDocuments --remote-id 1a2B3cD4eF5g \
    --remote-path "My Drive/Documents"
```

or use **Sync pairs → Add a sync pair → Browse…** in the dashboard, which
lets you click through your Drive folders.

### Run it

```bash
drivesync gui                   # desktop app: setup, sign-in, folders, activity
drivesync serve                 # foreground headless: dashboard + worker
systemctl --user enable --now drivesync   # or as a login service
```

The desktop app (`drivesync gui`) is what the menu launcher opens: one GTK
window with Setup / Accounts / Folders / Activity tabs, so a fresh machine
is configured entirely without the browser — except Google's own consent
page, which must open in a browser once per account (that step happens on
google.com by design). `serve` and the systemd unit are the headless/service
mode; running both is safe — a cross-process lock means only one syncs at a
time. Headless machines without GTK: `drivesync gui` prints the package to
install and exits.

Open <http://127.0.0.1:5678> — status, pairs, activity and settings.

### CLI reference

```
drivesync gui                         native desktop app (GTK window)
drivesync serve                       dashboard + background worker
drivesync auth                        authorize a Google account
drivesync accounts list|remove
drivesync pairs add|list|set|remove   manage sync pairs
drivesync sync [--pair N]             run one sync pass now and exit
drivesync status                      accounts, pairs, recent activity
drivesync config get|set              interval, conflict policy, ignores
drivesync doctor                      sanity-check the installation
```

### Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| Sync interval | 30 s | How often pairs are rescanned |
| Conflict policy | `newer` | Who wins when both sides changed |
| Ignore patterns | `*.tmp`, `*.swp`, … | Globs skipped on both sides |
| Convert Google Docs | on | Native docs downloaded as Office files |
| Server port | 5678 | Local dashboard port |

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

## Current limitations (honest list)

- **Polling, not inotify** — changes are picked up on the next scan (default
  30 s), not instantly
- **Google-native exports are re-downloaded in full** when the doc changes —
  a large Sheet edited in the browser transfers its whole export again
- **Local mtimes must differ for change detection** — if an editor writes a
  same-size file with the same mtime, run `drivesync sync` (or the dashboard)
  after a `touch`-only save; a `--full` rehash mode is planned
- No bandwidth throttling or end-to-end encryption
- The dashboard is localhost-only by design; don't expose it

## Development

```bash
python3 -m pytest tests/        # 94 tests: engine, store, config, API
drivesync doctor                # environment check
```

The engine's decision step is a pure function (maps in, ops out) tested
against a mock Drive — see `tests/test_engine.py` for the full matrix
(uploads, downloads, conflicts, deletes, renames, directions, ignores).

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Google or any commercial
Drive client.
