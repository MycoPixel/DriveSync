"""Portable settings: export/import config and sync pairs.

The exported file is deliberately *not* a backup of the data directory.
It carries preferences and pair definitions so a second computer can be
brought up quickly, but never OAuth tokens and never the client secret:
those are credentials, they belong to one machine/user, and leaking them
through a copied JSON file would be a security bug. The receiving machine
authorizes its own accounts (``drivesync auth``); imported pairs are matched
to existing accounts by e-mail.

Format (JSON, UTF-8)::

    {
      "format": "drivesync-config",
      "version": 1,
      "exported_at": "2026-11-22T...",
      "config": {...},           # non-secret settings
      "pairs": [{account_email, local_path, remote_id, ...}]
    }
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import DEFAULT_CONFIG, load_config, save_config
from .db import parse_include_patterns

FORMAT = "drivesync-config"
FORMAT_VERSION = 1
MAX_IMPORT_BYTES = 1 << 20          # 1 MiB is far more than this format needs

# Credentials must never travel in an export. client_id alone is not a
# secret, but pairing it with the machine's own secret is confusing; both
# are left for the user to re-enter on the new machine.
SECRET_CONFIG_KEYS = {"client_secret"}

# Machine-specific window state: copying it to another screen would open
# the window at a size/position that may not fit. Everything else is a
# genuine preference.
MACHINE_CONFIG_KEYS = {"window_width", "window_height", "window_maximized",
                       "last_tab"}


def _exportable_config(cfg):
    return {k: v for k, v in cfg.items()
            if k in DEFAULT_CONFIG
            and k not in SECRET_CONFIG_KEYS
            and k not in MACHINE_CONFIG_KEYS}


def _pair_payload(pair, email):
    return {
        "account_email": email,
        "local_path": pair["local_path"],
        "remote_id": pair["remote_id"],
        "remote_path": pair["remote_path"] or "",
        "direction": pair["direction"],
        "conflict_policy": pair["conflict_policy"],
        "enabled": bool(pair["enabled"]),
        "allow_deletes": bool(pair["allow_deletes"]),
        "include_patterns": parse_include_patterns(pair["include_patterns"]),
        "symlink_policy": pair["symlink_policy"] or "skip",
        "case_insensitive": bool(pair["case_insensitive"]),
        "bandwidth_limit_kb": pair["bandwidth_limit_kb"],
        "peak_from": pair["peak_from"] or None,
        "peak_to": pair["peak_to"] or None,
        "bandwidth_limit_kb_peak": pair["bandwidth_limit_kb_peak"],
    }


def export_settings(store, path, cfg=None):
    """Write settings + pair definitions to `path`; returns the payload."""
    cfg = cfg or load_config()
    emails = {a["id"]: a["email"] for a in store.accounts()}
    payload = {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": _exportable_config(cfg),
        "pairs": [_pair_payload(p, emails.get(p["account_id"]))
                  for p in store.pairs()],
    }
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # write atomically and 0600: even without secrets this is personal data
    fd, tmp = tempfile.mkstemp(prefix=".drivesync-export-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return payload


def _read_payload(path):
    path = Path(path).expanduser()
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if size > MAX_IMPORT_BYTES:
        raise ValueError(f"{path} is too large to be a settings export "
                         f"({size} bytes; max {MAX_IMPORT_BYTES})")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"not a valid DriveSync settings file: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise ValueError("not a DriveSync settings file (missing format marker)")
    if payload.get("version") != FORMAT_VERSION:
        raise ValueError(f"unsupported settings version: "
                         f"{payload.get('version')!r} (expected "
                         f"{FORMAT_VERSION})")
    return payload


def import_settings(store, path, dry_run=False):
    """Merge an exported file into this machine.

    Returns a report: applied config keys, added/skipped pairs and warnings.
    With dry_run=True nothing is written (used by ``config import --dry-run``).
    """
    payload = _read_payload(path)
    incoming = payload.get("config") or {}
    if not isinstance(incoming, dict):
        raise ValueError("config section must be an object")
    current = load_config()
    updates = {k: v for k, v in incoming.items()
               if k in DEFAULT_CONFIG and k not in SECRET_CONFIG_KEYS}
    # never import another machine's window geometry
    for key in MACHINE_CONFIG_KEYS:
        updates.pop(key, None)

    report = {"config_keys": sorted(updates), "added": [], "skipped": [],
              "warnings": [], "dry_run": bool(dry_run)}

    if not dry_run and updates:
        save_config({**current, **updates})

    emails = {a["email"].lower(): a["id"] for a in store.accounts()}
    existing = {(p["account_id"], p["local_path"], p["remote_id"])
                for p in store.pairs()}
    for item in payload.get("pairs") or []:
        if not isinstance(item, dict):
            report["warnings"].append("ignored a pair that is not an object")
            continue
        email = str(item.get("account_email") or "").strip().lower()
        local = str(item.get("local_path") or "").strip()
        remote = str(item.get("remote_id") or "").strip()
        label = f"{local} <-> {remote or '?'}"
        account_id = emails.get(email)
        if account_id is None:
            report["skipped"].append(
                f"{label}: no authorized account for {email or '(no e-mail)'}")
            continue
        if not local or not remote:
            report["skipped"].append(f"{label}: missing local path or Drive id")
            continue
        if (account_id, local, remote) in existing:
            report["skipped"].append(f"{label}: already present")
            continue
        if dry_run:
            report["added"].append(label)
            continue
        try:
            store.add_pair(
                account_id, local, remote,
                remote_path=str(item.get("remote_path") or ""),
                direction=str(item.get("direction") or "bidirectional"),
                conflict_policy=str(item.get("conflict_policy") or "newer"),
                enabled=1 if item.get("enabled", True) else 0,
                allow_deletes=1 if item.get("allow_deletes", True) else 0,
                include_patterns=item.get("include_patterns") or [],
                symlink_policy=str(item.get("symlink_policy") or "skip"),
                case_insensitive=1 if item.get("case_insensitive") else 0,
                bandwidth_limit_kb=item.get("bandwidth_limit_kb"),
                peak_from=item.get("peak_from"),
                peak_to=item.get("peak_to"),
                bandwidth_limit_kb_peak=item.get("bandwidth_limit_kb_peak"))
        except ValueError as exc:
            report["skipped"].append(f"{label}: {exc}")
            continue
        existing.add((account_id, local, remote))
        report["added"].append(label)
    return report
