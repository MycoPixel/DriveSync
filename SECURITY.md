# Security

DriveSync is a single-user desktop application that talks to Google Drive
directly with the user's own OAuth client. This document records the threat
model, the controls in place, and how to report a problem.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Use the
repository's **Security → Advisories → Report a vulnerability** form
(GitHub private vulnerability reporting) with:

- affected version and platform,
- a description and, if possible, a minimal reproduction,
- impact (credential exposure, file loss, remote code execution, …).

If the private form is unavailable, open a minimal issue asking for a
private channel — without technical details — and a maintainer will follow
up. We aim to acknowledge within a few days and will credit reporters
unless they prefer otherwise.

## Threat model

The valuable assets are:

1. **Google OAuth refresh tokens** — stored in `state.db` (or the OS
   keyring when `keyring_tokens` is on); they grant access to the user's
   Drive until revoked.
2. **The user's local files** inside sync roots — the app may overwrite,
   move or delete them (always through the safety/recovery paths below).
3. **The local dashboard** — a loopback HTTP API guarded by a random
   per-process token; it can mutate files and trigger sign-in.

Out of scope: a local attacker who can already read the user's home
directory, a compromised Google account, and a compromised OS/browser.

## Controls

### Credentials

- `config.json` and `state.db` are chmod **0600**; the data directory is
  **0700**. `Store` also hardens `state.db-wal`, `state.db-shm` and the
  backup (SQLite otherwise creates sidecars with the process umask).
- The OAuth client secret is never sent to the dashboard: `/api/settings`
  returns `"***"` plus a `client_secret_set` boolean.
- `drivesync config export` never writes tokens, `client_secret`, or the
  machine-specific window state. Imports keep the local secret.
- OAuth uses the installed-app loopback flow with **PKCE (S256)**, a random
  `state` value checked on the callback, and a short-lived local callback
  server bound to `127.0.0.1`.
- **Read-only mode** (`drive_scope=readonly`) requests only
  `drive.readonly` from Google and refuses to create or switch a pair to
  any direction that would write to Drive.
- Removing an account can **revoke its grant at Google** in the same step
  (the Accounts dialog asks first), so a discarded token stops working
  even if a backup of `state.db` survives.
- **Optional OS keyring storage** (`keyring_tokens=true`) moves refresh
  tokens out of SQLite into the desktop keyring (Secret Service / KWallet);
  the database then holds only a `{"keyring": true}` marker. If no keyring
  backend is available the setting is ignored and SQLite is used, so the
  app never starts in a broken state.

### Local dashboard

- `serve` refuses any `--host` that is not loopback
  (`127.0.0.1`, `::1`, `localhost`). `0.0.0.0`/`::` are coerced to
  loopback in the CLI and rejected by `run_server`.
- Every `/api/*` request requires a **random per-server token**
  (`secrets.token_urlsafe(32)`), sent as `X-DriveSync` or as
  `Authorization: Bearer …`. The dashboard receives it in a
  `<meta name="drivesync-token">` tag on the page it serves; a cross-origin
  page cannot read that tag and cannot set the custom header without a
  CORS preflight, which the server never grants. Loopback `Host` and, when
  present, loopback `Origin` checks run as well. This blocks DNS rebinding
  and cross-site POSTs.
- Sign-in runs as a cancellable background job; cancelling stops the
  loopback callback server and no token is stored.
- The API returns JSON errors, never tracebacks or token values.

### Destructive operations

- Local deletions are moved to `<data>/trash/<pair>/<stamp>/<rel>`, not
  unlinked. Remote deletions go to Google Drive's trash.
- Overwrites keep previous bytes under `<data>/history/...`.
- `delete_safety_limit` pauses a pair when a pass would delete more files
  than the configured threshold; the Recovery page can release or keep the
  holds.
- Conflict copies are preserved on both sides and resolved only by an
  explicit user action.
- All path joins that accept an API-supplied relative path go through
  `security.safe_join`, which rejects absolute paths, NUL bytes, `..`
  components and symlink escapes. The sync engine's explicit
  `symlink_policy=follow` mode uses lexical containment only (following a
  user-created symlink is the documented feature).

### Dependencies and releases

- Runtime dependencies are pinned in `requirements.lock`; CI installs that
  file and runs `pip-audit -r requirements.lock` on the release
  interpreter, so the audit covers exactly what users get.
- CI runs a wider Ruff rule set (fatal errors plus unused/duplicate names
  and mutable default arguments) configured in `pyproject.toml`.
- Releases are built from tagged commits by GitHub Actions; the test matrix
  must pass before the draft release is published. Actions are pinned to
  commit SHAs and the Fedora container to a concrete release tag.
- Every package is installed and smoke-tested (`drivesync --version`) on
  the platform it targets before it is attached to the release.
- The `publish` job emits **signed build provenance** for the deb, rpm,
  AppImage and Flatpak assets, verifiable with `gh attestation verify`.
- The shipped systemd user unit runs with `NoNewPrivileges`, a private
  `/tmp`, a read-only system tree (`ProtectSystem=full`), restricted
  address families (AF_UNIX/INET/INET6), `RestrictSUIDSGID` and locked
  personality. `ProtectHome` stays off because syncing files under the
  home directory is the app's purpose.
- The Flatpak manifest uses the GNOME runtime and only adds Flask,
  requests and the app itself.

## Auditing an installation

```bash
drivesync audit          # check modes, token presence, world-writable files
drivesync audit --fix    # repair insecure permissions
```

The audit never prints secret values. A non-zero exit means at least one
check failed.

## Known limitations

- The dashboard token is a CSRF/rebinding defense, not user
  authentication: any process that can already read the user's session or
  files can obtain it. It is safe only because it is loopback-only; do not
  put it behind a reverse proxy or expose the port.
- A user who can run code as the same account can read the tokens; file
  permissions protect against other local users, not a compromised session.
  With `keyring_tokens` on, token theft additionally requires access to the
  unlocked desktop keyring.
- `drivesync mount` (FUSE) is read-only and still subject to the Drive
  account's own permissions.
