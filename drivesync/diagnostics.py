"""Diagnostics: rotating log setup and a redacted support bundle.

``drivesync doctor --bundle`` writes a zip a maintainer can ask for without
the user having to find and scrub their config, tokens or full activity
log. Everything secret is masked before it leaves the machine.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import re
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import app_dir, config_path, db_path, load_config

LOG_NAME = "drivesync.log"
MAX_LOG_BYTES = 1 << 20          # 1 MiB per file
LOG_BACKUPS = 3

# Token shapes Google uses, plus generic bearer/client-secret patterns. The
# activity log normally holds paths and counts, but a future error message
# must not leak a credential into a shared bundle.
_SECRET_PATTERNS = (
    re.compile(r"ya29\.[A-Za-z0-9._-]+"),
    re.compile(r"1//[A-Za-z0-9._-]+"),
    re.compile(r"GOCSPX-[A-Za-z0-9._-]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(\"?(?:access_token|refresh_token|client_secret)\"?\s*[:=]\s*\"?)[^\",}\s]+"),
)
_SECRET_KEYS = {"client_secret", "access_token", "refresh_token",
                "id_token", "token", "token_json"}


def redact(text):
    """Mask Google credentials and secret-looking key/value pairs."""
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(
            lambda m: (m.group(1) if m.groups() else "") + "***", out)
    return out


class _RedactFilter(logging.Filter):
    def filter(self, record):
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


_configured = False


def configure_logging(level=logging.INFO):
    """Attach a rotating, redacting file handler to the root logger.

    Called by ``serve``. Returns the log path, or None when the data
    directory is not writable (a read-only home must not stop syncing).
    """
    global _configured
    path = app_dir() / LOG_NAME
    if _configured:
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS,
            encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_RedactFilter())
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    _configured = True
    return path


def _redacted_config():
    cfg = dict(load_config())
    for key in list(cfg):
        if key in _SECRET_KEYS:
            cfg[key] = "***" if cfg[key] else ""
    return cfg


def _quick_check():
    path = db_path()
    if not os.path.exists(path):
        return "no state.db yet"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
            return "\n".join(str(r[0]) for r in rows) or "ok"
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return f"quick_check failed: {exc}"


def build_bundle(destination=None, store=None):
    """Write a redacted support zip; returns its path.

    ``store`` is optional so the CLI can pass the already-open Store. When
    omitted, recent events are read from a temporary connection.
    """
    destination = Path(destination) if destination else (
        Path.cwd() / f"drivesync-diagnostics-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.zip")
    destination.parent.mkdir(parents=True, exist_ok=True)

    events = []
    try:
        if store is not None:
            events = [dict(r) for r in store.recent_events(limit=200)]
        elif os.path.exists(db_path()):
            conn = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                events = [dict(r) for r in conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT 200")]
            finally:
                conn.close()
    except Exception as exc:      # a broken DB is exactly what we want to see
        events = [{"error": str(exc)}]

    summary = "\n".join((
        f"DriveSync {__version__}",
        f"generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"platform: {platform.platform()}",
        f"python: {sys.version.split()[0]}",
        f"data dir: {app_dir()}",
        f"config: {config_path()}",
        f"database: {db_path()}",
        f"events included: {len(events)}",
        "",
        "database quick_check:",
        _quick_check(),
    ))

    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("summary.txt", redact(summary))
        zf.writestr("config.json",
                    redact(json.dumps(_redacted_config(), indent=2,
                                      sort_keys=True)))
        zf.writestr("events.json",
                    redact(json.dumps(events, indent=2, default=str)))
        log = app_dir() / LOG_NAME
        if log.exists():
            try:
                zf.writestr(LOG_NAME, redact(
                    log.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
    return destination
