"""Application paths and JSON configuration with defaults."""

import copy
import json
import os
from pathlib import Path

DEFAULT_CONFIG = {
    "sync_interval": 30,          # seconds between full pair scans
    "conflict_policy": "newer",   # newer | local | remote | both
    "delete_safety_limit": 10,    # max deletes per pair per pass; 0 = unlimited
    "transfer_concurrency": 4,    # parallel uploads/downloads per pass (1-16)
    "tray_enabled": True,         # system tray icon (needs pystray + pillow)
    "ignore_patterns": ["*.tmp", "*.swp", "*.partial", ".DS_Store", "Thumbs.db"],
    "server_port": 5678,
    "auth_port": 53137,           # loopback port for the OAuth redirect
    "client_id": "",              # user's own Google OAuth desktop client
    "client_secret": "",
    "convert_google_docs": True,  # download Docs/Sheets/Slides as docx/xlsx/pptx
    "export_formats": {},         # per-kind override, e.g. {"document": "pdf"}
    "incremental_scan": True,     # use the Drive changes feed instead of
                                  # re-listing the whole tree every pass
}


def app_dir() -> Path:
    """Single data dir holding config.json and state.db.

    Override with DRIVE_SYNC_HOME (handy for tests and sandboxes).
    """
    env = os.environ.get("DRIVE_SYNC_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "drivesync"


def config_path() -> Path:
    return app_dir() / "config.json"


def db_path() -> Path:
    return app_dir() / "state.db"


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    p = config_path()
    if p.exists():
        try:
            stored = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                cfg.update({k: v for k, v in stored.items() if v is not None})
        except (json.JSONDecodeError, OSError):
            pass  # fall back to defaults; keep the app usable
    return cfg


def save_config(cfg: dict) -> dict:
    """Validate and persist config; returns the normalized dict."""
    merged = copy.deepcopy(DEFAULT_CONFIG)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})

    try:
        interval = int(merged["sync_interval"])
    except (TypeError, ValueError):
        raise ValueError("sync_interval must be an integer (seconds)")
    if not (5 <= interval <= 86400):
        raise ValueError("sync_interval must be between 5 and 86400 seconds")
    merged["sync_interval"] = interval

    if merged["conflict_policy"] not in ("newer", "local", "remote", "both"):
        raise ValueError("conflict_policy must be one of: newer, local, remote, both")

    try:
        limit = int(merged["delete_safety_limit"])
    except (TypeError, ValueError):
        raise ValueError("delete_safety_limit must be an integer")
    if limit < 0:
        raise ValueError("delete_safety_limit must be 0 (unlimited) or higher")
    merged["delete_safety_limit"] = limit

    try:
        conc = int(merged["transfer_concurrency"])
    except (TypeError, ValueError):
        raise ValueError("transfer_concurrency must be an integer")
    if not (1 <= conc <= 16):
        raise ValueError("transfer_concurrency must be between 1 and 16")
    merged["transfer_concurrency"] = conc
    merged["tray_enabled"] = bool(merged["tray_enabled"])
    merged["incremental_scan"] = bool(merged["incremental_scan"])

    if not isinstance(merged["ignore_patterns"], list) or \
            not all(isinstance(x, str) for x in merged["ignore_patterns"]):
        raise ValueError("ignore_patterns must be a list of strings")

    try:
        port = int(merged["server_port"])
    except (TypeError, ValueError):
        raise ValueError("server_port must be an integer")
    if not (1024 <= port <= 65535):
        raise ValueError("server_port must be between 1024 and 65535")
    merged["server_port"] = port

    merged["client_id"] = str(merged["client_id"]).strip()
    merged["client_secret"] = str(merged["client_secret"]).strip()

    merged["convert_google_docs"] = bool(merged["convert_google_docs"])
    fmts = merged["export_formats"]
    if fmts is None:
        fmts = {}
    if not isinstance(fmts, dict) or \
            not all(isinstance(k, str) and isinstance(v, str)
                    for k, v in fmts.items()):
        raise ValueError('export_formats must be a mapping like '
                         '{"document": "pdf", "spreadsheet": "csv"}')
    merged["export_formats"] = {str(k).strip(): str(v).strip().lower()
                                for k, v in fmts.items() if str(v).strip()}

    app_dir().mkdir(parents=True, exist_ok=True)
    tmp = config_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, config_path())
    return merged


def update_config(**changes) -> dict:
    return save_config({**load_config(), **changes})
