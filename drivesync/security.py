"""Small security primitives shared by the CLI, server and sync engine.

The app stores OAuth refresh tokens in ``state.db`` and moves files around
the user's chosen sync roots. Two mistakes are easy to make and expensive:
joining an untrusted relative path onto a root (``../../.ssh/id_rsa``) and
leaving a token-bearing file world-readable. The helpers here centralize
both checks so call sites cannot drift.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .config import app_dir, config_path, db_path


def safe_join(root, rel, allow_symlink_escape=False):
    """Join a *relative* path onto `root`, refusing escapes.

    Accepts the app's forward-slash rel-path convention (also tolerates
    backslashes from Windows-created names). Rejects absolute paths, NUL
    bytes, ``..`` components and targets that resolve outside the root
    (including through a symlink). Returns the normalized absolute path.

    ``allow_symlink_escape`` is for the sync engine's explicit
    ``symlink_policy=follow`` mode: lexical containment is still enforced,
    but a user-created symlink may point outside the pair root (that is the
    documented meaning of "follow"). Untrusted paths from the API (recovery,
    conflict resolution) must keep the default strict behaviour.
    """
    root_abs = os.path.realpath(str(root))
    text = str(rel or "").replace("\\", "/")
    if "\x00" in text:
        raise ValueError("path contains a NUL byte")
    if text.startswith("/"):
        raise ValueError("path must be relative to the sync folder")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("path must not contain '..'")
    target = os.path.normpath(os.path.join(root_abs, *parts))
    try:
        inside = os.path.commonpath([root_abs, target]) == root_abs
    except ValueError:          # different drives / weird paths
        inside = False
    if not inside:
        raise ValueError("path escapes the sync folder")
    if allow_symlink_escape:
        return target
    resolved = os.path.realpath(target)
    try:
        inside = os.path.commonpath([root_abs, resolved]) == root_abs
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("path escapes the sync folder through a symlink")
    return resolved


def _mode(path):
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def audit(store=None, cfg=None):
    """Inspect the local installation for common security problems.

    Returns ``{"ok": bool, "checks": [{"name", "ok", "detail", "fix"}]}``.
    `fix` is a callable that repairs the issue, or None. No secret values
    are ever included in the output.
    """
    from .config import load_config

    cfg = cfg or load_config()
    checks = []

    def add(name, ok, detail, fix=None):
        checks.append({"name": name, "ok": bool(ok), "detail": detail,
                       "fix": fix})

    data = app_dir()
    data_mode = _mode(data)
    add("data directory private", data_mode is not None and data_mode <= 0o700,
        f"{data} mode {oct(data_mode) if data_mode is not None else 'missing'}",
        (lambda: os.chmod(data, 0o700)) if data_mode is not None else None)

    for label, path in (("config", config_path()), ("state database", db_path())):
        if not os.path.exists(path):
            add(f"{label} private", True, "not created yet")
            continue
        mode = _mode(path)
        add(f"{label} private", mode is not None and mode <= 0o600,
            f"{path} mode {oct(mode) if mode is not None else '?'}",
            lambda p=path: os.chmod(p, 0o600))

    # WAL/SHM files carry the same rows as state.db; SQLite normally copies
    # the DB mode, but an old/foreign copy may not.
    for side in ("-wal", "-shm"):
        path = Path(str(db_path()) + side)
        if path.exists():
            mode = _mode(path)
            add(f"database {side} private", mode is not None and mode <= 0o600,
                f"{path} mode {oct(mode) if mode is not None else '?'}",
                lambda p=path: os.chmod(p, 0o600))

    secret = str(cfg.get("client_secret") or "")
    # Not having a client configured is a setup state, not a security
    # failure; the risk (an insecurely stored secret) is covered by the
    # config-file permission check above.
    add("OAuth client secret stored", True,
        "configured (value not shown)" if secret else
        "not configured yet; run 'drivesync auth' with a Google OAuth client")
    add("dashboard is loopback-only", True,
        "the server refuses non-loopback --host values at startup")

    if store is not None:
        accounts = store.accounts()
        add("authorized accounts", True,
            f"{len(accounts)} account(s); tokens live in the 0600 database")
        missing = [a["email"] for a in accounts if not a["token_json"]]
        add("tokens present for every account", not missing,
            "ok" if not missing else f"missing tokens: {', '.join(missing)}")

    # Anything world-writable in the data dir is suspicious (logs, exports).
    bad = []
    try:
        for entry in data.iterdir():
            mode = _mode(entry)
            if mode is not None and mode & 0o002:
                bad.append(f"{entry.name} ({oct(mode)})")
    except OSError:
        pass
    add("no world-writable files in data dir", not bad,
        "ok" if not bad else ", ".join(bad))

    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def apply_fixes(report):
    """Run the repair callables of failing checks; returns how many ran."""
    fixed = 0
    for check in report["checks"]:
        if not check["ok"] and check["fix"] is not None:
            try:
                check["fix"]()
                fixed += 1
            except OSError:
                pass
    return fixed
