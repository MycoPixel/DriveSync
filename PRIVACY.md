# Privacy

DriveSync is open-source software that runs on your own computer. It has no
server, no account system and no analytics.

**What it accesses.** Using the Google OAuth client *you* create, DriveSync
reads and writes the files in the Google Drive folders you choose to sync, and
reads your account's e-mail address and storage quota so it can show them in
the dashboard.

**Where data goes.** Your files and your sign-in tokens are only exchanged
between your computer and Google (`accounts.google.com`,
`oauth2.googleapis.com`, `www.googleapis.com`). Nothing is sent to the
DriveSync authors or to any other third party.

**What is stored locally.** Sync state, your OAuth client ID/secret and your
refresh tokens are kept in `~/.local/share/drivesync/` with owner-only
permissions. Removing an account in DriveSync deletes its stored tokens; you
can also revoke access at any time from your Google Account's
[third-party access](https://myaccount.google.com/permissions) page.

**Telemetry.** None. DriveSync does not collect usage data, crash reports or
identifiers.

**Contact.** Open an issue at <https://github.com/MycoPixel/DriveSync/issues>.
