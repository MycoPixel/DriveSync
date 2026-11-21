"""Metered-connection detection (Phase 4.3).

Answers "is the active network connection metered?" for the worker's
pause / throttle decision. Detection order:

1. ``DRIVE_SYNC_METERED`` environment override (``yes`` / ``no``) — the
   test hook, and an escape hatch for setups NetworkManager cannot see
   (VPN-only machines, containers). A junk value falls through to real
   detection.
2. NetworkManager over ``busctl`` (a plain property read — no python-dbus
   dependency, works headless).
3. ``nmcli`` device listing (``GENERAL.METERED``) as a fallback for
   systems where busctl is unavailable.
4. Unknown → ``(None, "unavailable")``. Callers FAIL OPEN: sync runs as
   if the network were not metered. Missing/broken NetworkManager must
   never disable syncing on a normal desktop.

Results are cached for ``TTL`` seconds so the worker loop can poll each
tick without spawning a subprocess every pass.
"""
import os
import re
import shutil
import subprocess
import time

TTL = 30.0                      # seconds a detection result stays valid

# NM_METERED_* values from org.freedesktop.NetworkManager: 0 unknown,
# 1 yes, 2 no, 3 guess-yes, 4 guess-no. Guesses are honoured — NM only
# guesses when the user has not set the flag, and guessing wrong costs a
# capped-plan user real money while failing open is safe for sync.
_BUSCTL_VALUE = {"1": True, "2": False, "3": True, "4": False}
_NMCLI_VALUE = {"yes": True, "guess-yes": True,
                "no": False, "guess-no": False}

_cache = {"value": None, "source": "", "at": 0.0}


def is_metered(max_age=TTL):
    """(metered, source) — metered is True/False, or None when unknown."""
    now = time.monotonic()
    if _cache["at"] and (now - _cache["at"]) < max_age:
        return _cache["value"], _cache["source"]
    value, source = _detect()
    _cache.update(value=value, source=source, at=now)
    return value, source


def clear_cache():
    """Drop the cached result (config changes, tests, manual refresh)."""
    _cache.update(value=None, source="", at=0.0)


def _detect():
    env = os.environ.get("DRIVE_SYNC_METERED", "").strip().lower()
    if env in ("yes", "true", "1"):
        return True, "env"
    if env in ("no", "false", "0"):
        return False, "env"
    value = _from_busctl()
    if value is not None:
        return value, "NetworkManager"
    value = _from_nmcli()
    if value is not None:
        return value, "NetworkManager"
    return None, "unavailable"


def _from_busctl():
    """Read NM's Metered property; None when busctl/NM are unavailable."""
    if shutil.which("busctl") is None:
        return None
    try:
        proc = subprocess.run(
            ["busctl", "get-property", "org.freedesktop.NetworkManager",
             "/org/freedesktop/NetworkManager",
             "org.freedesktop.NetworkManager", "Metered"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    m = re.search(r"(\d+)\s*$", proc.stdout)
    return _BUSCTL_VALUE.get(m.group(1)) if m else None


def _from_nmcli():
    """Parse 'GENERAL.METERED:<value>' lines from nmcli device show."""
    if shutil.which("nmcli") is None:
        return None
    try:
        proc = subprocess.run(
            ["nmcli", "-t", "-f", "GENERAL.METERED", "device", "show"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        _, _, value = line.strip().partition(":")
        if value in _NMCLI_VALUE:
            return _NMCLI_VALUE[value]
    return None
