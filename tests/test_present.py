"""Phase 3.3 — shared presentation helpers (GTK/web parity).

drivesync/present.py must stay importable WITHOUT PyGObject (the GTK
module pulls in gi at load), so these tests run headless and guard the
exact strings and shapes both front-ends rely on.
"""

from drivesync import i18n
from drivesync.present import (DESTRUCTIVE_MARK, STATUS_COLORS,
                               human_bytes, preview_rows, scan_note,
                               transfer_status_color)


# ---- human_bytes ------------------------------------------------------------

def test_human_bytes_formats_and_hides_empty():
    assert human_bytes(None) == ""
    assert human_bytes(0) == ""
    assert human_bytes(1) == "1 B"
    assert human_bytes(12288) == "12.0 KB"
    assert human_bytes(1024 * 1024) == "1.0 MB"


# ---- preview_rows -----------------------------------------------------------

def test_preview_rows_carries_reason_size_and_destructive():
    res = {
        "ops": [("upload", "a/b.bin", "replace"),
                ("delete_remote", "old.txt", "deleted on Drive"),
                ("mkdir_remote", "newdir", "")],
        "reasons": ["local changed since base", "deleted on Drive", ""],
        "sizes": [12288, None, None],
        "destructive": [False, True, False],
    }
    rows = preview_rows(res)
    assert [r[0] for r in rows] == ["upload", "delete_remote", "mkdir_remote"]
    assert rows[0] == ("upload", "a/b.bin", "12.0 KB", "replace",
                       "local changed since base", False)
    assert rows[1][4] == "deleted on Drive"
    assert rows[1][5] is True          # the GUI prefixes DESTRUCTIVE_MARK
    assert rows[2][2] == ""            # non-transfer: no size cell
    assert rows[2][4] == ""            # no reason -> empty, not None


def test_preview_rows_tolerates_missing_arrays_and_short_ops():
    # the preview error fallback returns {"ops": [], "totals": {}, ...}
    assert preview_rows({"ops": []}) == []
    # ops without detail/reason/sizes must not crash the dialog
    res = {"ops": [("upload", "x.bin")], "sizes": [500]}
    rows = preview_rows(res)
    assert rows == [("upload", "x.bin", "500 B", "", "", False)]


def test_destructive_mark_is_symbol_only():
    # a mark that survives every language without translation
    assert DESTRUCTIVE_MARK.strip() == "⚠"


# ---- scan_note --------------------------------------------------------------

def test_scan_note_reports_counts_and_mode():
    note = scan_note({"local_n": 12, "remote_n": 10, "delta_used": True})
    assert note == "12 local, 10 remote — delta scan"
    note = scan_note({"local_n": 3, "remote_n": 4, "delta_used": False})
    assert note == "3 local, 4 remote — full scan"


def test_scan_note_defaults_when_scan_is_sketchy():
    assert scan_note({}) == "0 local, 0 remote — full scan"


# ---- transfer status colors ---------------------------------------------------

def test_transfer_status_colors():
    assert transfer_status_color("failed") == STATUS_COLORS["failed"]
    assert transfer_status_color("retrying") == STATUS_COLORS["retrying"]
    assert transfer_status_color("completed") is None
    assert transfer_status_color("queued") is None


# ---- i18n coverage for the new strings ---------------------------------------

def test_new_strings_have_spanish_translations():
    for key in ("retrying", "Why", "delta", "full",
                "Transfers", "Retry attempts",
                "Backoff base (seconds)", "Resumable threshold (MiB)",
                "plus {n} other op(s)",
                "{l} local, {r} remote — {m} scan",
                "Retry and resume settings for file transfers. Applies "
                "from the next sync pass."):
        assert i18n._STRINGS.get(key, "").strip(), f"missing ES: {key}"
