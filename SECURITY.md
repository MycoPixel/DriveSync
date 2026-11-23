# Security

DriveSync is a single-user desktop application that talks to Google Drive
directly with the user's own OAuth client. This document records the threat
model, the controls in place, and how to report a problem.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Contact the
maintainers privately (the repository's security advisory form, or the
e-mail address in `pyproject.toml`) with:

- affected version and platform,
- a description and, if possible, a minimal reproduction,
- impact (credential exposure, file loss, remote code execution, …).

We aim to acknowledge within a few days and will credit reporters unless
they prefer otherwise.

## Threat model

The valuable assets are:

1. **Google OAuth refresh tokens** — stored in `state.db`; they grant
   access to the user's Drive until revoked.
2. **The user's local files** inside sync roots — the app may overwrite,
   move or delete them (always through the safety/recovery paths below).
3. **The local dashboard** — an unauthenticated HTTP API on loopback that
   can read tokens indirectly and mutate files.

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

### Local dashboard

- `serve` refuses any `--host` that is not loopback
  (`127.0.0.1`, `::1`, `localhost`). `0.0.0.0`/`::` are coerced to
  loopback in the CLI and rejected by `run_server`.
- Every `/api/*` request requires the `X-DriveSync: 1` header (a custom
  header cannot be sent cross-origin without a CORS preflight, which the
  server never grants), plus loopback `Host` and, when present, loopback
  `Origin` checks. This blocks DNS rebinding and cross-site POSTs.
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

- CI runs `pip-audit` on every test matrix entry.
- Releases are built from tagged commits by GitHub Actions; artifacts are
  smoke-tested before publication.
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

- The dashboard has no user authentication. It is safe only because it is
  loopback-only; do not put it behind a reverse proxy or expose the port.
- A user who can run code as the same account can read the tokens; file
  permissions protect against other local users, not a compromised session.
- `drivesync mount` (FUSE) is read-only and still subject to the Drive
  account's own permissions.
