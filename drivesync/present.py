"""Presentation helpers shared by the GTK and web front-ends.

Pure functions — no gi/GLib imports here, so they are importable and
testable on headless machines. Phase 3.3 moved the plan-preview row
building, the scan summary note and the transfer-status colors into one
place so both front-ends stay consistent (the web UI renders reasons and
sizes since Phase 3.1; the GTK dialog reuses exactly the same logic).
"""

from .i18n import t, T

# prepended to the action label of destructive ops (deletes, folder
# removals) in plan previews — symbol-only, so it needs no translation
DESTRUCTIVE_MARK = "⚠ "

# transfer queue status → cell foreground (None = theme default)
STATUS_COLORS = {"failed": "#d93025", "retrying": "#e8710a"}


def human_bytes(n):
    """Byte count in human units (\"\" for unknown/zero-size rows)."""
    n = float(n or 0)
    if n <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def preview_rows(res):
    """One row per planned op: (name, path, size_text, detail, reason,
    destructive).

    Tolerates preview dicts from older shapes or the error fallback that
    lack the Phase 3.1 parallel arrays — those degrade to empty cells.
    """
    ops = res.get("ops") or []
    n = len(ops)
    reasons = res.get("reasons") or [None] * n
    sizes = res.get("sizes") or [None] * n
    destructive = res.get("destructive") or [False] * n
    rows = []
    for i, op in enumerate(ops):
        rows.append((
            op[0],
            op[1] if len(op) > 1 else "",
            human_bytes(sizes[i] if i < len(sizes) else None),
            op[2] if len(op) > 2 else "",
            reasons[i] if i < len(reasons) and reasons[i] else "",
            bool(destructive[i]) if i < len(destructive) else False,
        ))
    return rows


def scan_note(scan):
    """How the preview scanned: file counts and delta vs full."""
    mode = t("delta") if scan.get("delta_used") else t("full")
    return T("{l} local, {r} remote — {m} scan",
             l=scan.get("local_n", 0), r=scan.get("remote_n", 0), m=mode)


def transfer_status_color(status):
    """Cell foreground for a transfer-queue status (None = default)."""
    return STATUS_COLORS.get(status)
