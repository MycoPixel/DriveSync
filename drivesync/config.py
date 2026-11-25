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
    "transfer_retries": 2,        # extra attempts for a failed transfer (0-10)
    "transfer_retry_base": 2.0,   # seconds; the wait doubles each retry,
                                  # capped at 30 s (0-30)
    "resumable_threshold": 8 << 20,  # bytes; uploads at least this big use
                                     # resumable sessions (0 = always)
    "tray_enabled": True,         # system tray icon (needs pystray + pillow)
    "watch_enabled": True,        # instant local-change sync (needs watchdog)
    "bandwidth_limit_kb": 0,      # uploads+downloads cap in KB/s; 0 = unlimited
    "peak_from": "",              # HH:MM — peak window start (empty = no
    "peak_to": "",                # peak window); may span midnight
    "bandwidth_limit_kb_peak": 0,  # cap INSIDE the peak window; 0 = fall
                                  # back to bandwidth_limit_kb there
    "active_from": "",            # HH:MM — background sync only inside the
    "active_to": "",              # window (empty = always; may span midnight)
    "respect_metered": True,      # defer automatic passes on metered
                                  # connections (Phase 4.3)
    "metered_action": "pause",    # pause | throttle — what metered means
    "metered_bandwidth_kb": 1024, # cap while throttled; 0 = no extra cap
    "digest_enabled": False,      # daily "what synced" summary (Phase 4.4)
    "digest_time": "18:00",       # HH:MM — fires on the first tick at/after
    "digest_skip_empty": True,    # stay quiet when nothing happened
    "ignore_patterns": ["*.tmp", "*.swp", "*.partial", "*.drivesync",
                        ".DS_Store", "Thumbs.db"],
    "server_port": 5678,
    "auth_port": 53137,           # loopback port for the OAuth redirect
    "client_id": "",              # user's own Google OAuth desktop client
    "client_secret": "",
    "convert_google_docs": True,  # download Docs/Sheets/Slides as docx/xlsx/pptx
    "export_formats": {},         # per-kind override, e.g. {"document": "pdf"}
    "incremental_scan": True,     # use the Drive changes feed instead of
                                  # re-listing the whole tree every pass
    "history_versions": 10,       # old local versions kept per file before a
                                  # pull overwrites it; 0 = keep none
    "theme": "system",            # system | light | dark | glass-light |
                                  # glass-dark | graphite-light | graphite-dark
    "accent": "blue",             # buttons + selection highlight color
    "language": "en",             # interface language: en | es
    "start_at_login": False,      # XDG autostart entry (desktop sessions)
    # Desktop window state (GTK app only): restored on the next launch.
    "window_width": 900,
    "window_height": 640,
    "window_maximized": False,
    "last_tab": 0,
}


def _valid_hhmm(v):
    """'HH:MM' with 0<=HH<=23, 0<=MM<=59."""
    try:
        h, _, m = v.partition(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except ValueError:
        return False


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

    # transfer resilience knobs. They were never in DEFAULT_CONFIG, and
    # save_config drops unknown keys, so the GUI's Options > Transfers
    # values were silently discarded (and hand-edited ones wiped on the
    # next save). Ranges match the GUI spin buttons.
    try:
        retries = int(merged["transfer_retries"])
    except (TypeError, ValueError):
        raise ValueError("transfer_retries must be an integer")
    if not (0 <= retries <= 10):
        raise ValueError("transfer_retries must be between 0 and 10")
    merged["transfer_retries"] = retries
    try:
        retry_base = float(merged["transfer_retry_base"])
    except (TypeError, ValueError):
        raise ValueError("transfer_retry_base must be a number (seconds)")
    if not (0.0 <= retry_base <= 30.0):      # also rejects NaN / inf
        raise ValueError("transfer_retry_base must be between 0 and 30 seconds")
    merged["transfer_retry_base"] = round(retry_base, 1)
    try:
        threshold = int(merged["resumable_threshold"])
    except (TypeError, ValueError):
        raise ValueError("resumable_threshold must be an integer (bytes)")
    if not (0 <= threshold <= 512 << 20):
        raise ValueError(
            "resumable_threshold must be between 0 and 536870912 bytes (512 MiB)")
    merged["resumable_threshold"] = threshold
    merged["tray_enabled"] = bool(merged["tray_enabled"])
    merged["incremental_scan"] = bool(merged["incremental_scan"])
    merged["watch_enabled"] = bool(merged["watch_enabled"])

    try:
        hist = int(merged["history_versions"])
    except (TypeError, ValueError):
        raise ValueError("history_versions must be an integer")
    if not (0 <= hist <= 100):
        raise ValueError("history_versions must be between 0 (off) and 100")
    merged["history_versions"] = hist

    # appearance / locale (themes.ACCENTS/THEMES are dependency-free dicts)
    from . import themes
    merged["theme"] = themes.canonical(str(merged["theme"] or "system"))
    if merged["theme"] is None:
        raise ValueError(
            f"theme must be one of: {', '.join(themes.THEMES)}")
    merged["accent"] = str(merged["accent"] or "blue")
    if merged["accent"] not in themes.ACCENTS:
        raise ValueError(
            f"accent must be one of: {', '.join(themes.ACCENTS)}")
    merged["language"] = str(merged["language"] or "en")
    if merged["language"] not in ("en", "es"):
        raise ValueError("language must be 'en' or 'es'")
    merged["start_at_login"] = bool(merged["start_at_login"])

    # desktop window geometry (GTK app): keep junk from killing launch and
    # stop a stale maximized size from making the next window huge
    for key, lo, hi in (("window_width", 480, 10000),
                        ("window_height", 360, 10000)):
        try:
            value = int(merged[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be an integer")
        if not (lo <= value <= hi):
            raise ValueError(f"{key} must be between {lo} and {hi}")
        merged[key] = value
    merged["window_maximized"] = bool(merged["window_maximized"])
    try:
        tab = int(merged["last_tab"])
    except (TypeError, ValueError):
        raise ValueError("last_tab must be an integer")
    if not (0 <= tab <= 15):
        raise ValueError("last_tab must be between 0 and 15")
    merged["last_tab"] = tab

    try:
        bw = int(merged["bandwidth_limit_kb"])
    except (TypeError, ValueError):
        raise ValueError("bandwidth_limit_kb must be an integer (KB/s)")
    if bw < 0:
        raise ValueError("bandwidth_limit_kb must be 0 (unlimited) or higher")
    merged["bandwidth_limit_kb"] = bw

    for k in ("active_from", "active_to", "peak_from", "peak_to"):
        v = str(merged[k] or "").strip()
        if v and not _valid_hhmm(v):
            raise ValueError(f"{k} must be empty or HH:MM (e.g. 08:00)")
        merged[k] = v
    if (str(merged["peak_from"]).strip() != "") != \
            (str(merged["peak_to"]).strip() != ""):
        raise ValueError("peak_from and peak_to must be set together")

    try:
        bw_peak = int(merged["bandwidth_limit_kb_peak"])
    except (TypeError, ValueError):
        raise ValueError(
            "bandwidth_limit_kb_peak must be an integer (KB/s)")
    if bw_peak < 0:
        raise ValueError(
            "bandwidth_limit_kb_peak must be 0 (fall back to the general "
            "limit) or higher")
    merged["bandwidth_limit_kb_peak"] = bw_peak

    merged["respect_metered"] = bool(merged["respect_metered"])
    action = str(merged["metered_action"] or "pause").strip().lower()
    if action not in ("pause", "throttle"):
        raise ValueError("metered_action must be 'pause' or 'throttle'")
    merged["metered_action"] = action
    try:
        metered_bw = int(merged["metered_bandwidth_kb"])
    except (TypeError, ValueError):
        raise ValueError("metered_bandwidth_kb must be an integer (KB/s)")
    if metered_bw < 0:
        raise ValueError("metered_bandwidth_kb must be 0 (no throttle "
                         "cap) or higher")
    merged["metered_bandwidth_kb"] = metered_bw

    merged["digest_enabled"] = bool(merged["digest_enabled"])
    merged["digest_skip_empty"] = bool(merged["digest_skip_empty"])
    dv = str(merged["digest_time"] or "").strip()
    if dv and not _valid_hhmm(dv):
        raise ValueError("digest_time must be empty or HH:MM (e.g. 18:00)")
    merged["digest_time"] = dv      # empty = fire on the first tick daily

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

    home = app_dir()
    home.mkdir(parents=True, exist_ok=True)
    # the file holds the OAuth client secret: keep the dir and file private
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass
    tmp = config_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, config_path())
    try:
        os.chmod(config_path(), 0o600)
    except OSError:
        pass
    return merged


def update_config(**changes) -> dict:
    return save_config({**load_config(), **changes})
