"""Phase 4.1 — scheduled QoS: peak-window bandwidth switching.

The peak window reuses the sync-window's in_window() logic (midnight
spans included) to select which token-bucket rate the worker runs at.
"""

from datetime import datetime, time as dtime

import pytest

from drivesync.config import save_config, update_config
from drivesync.limits import TokenBucket
from drivesync.worker import Worker, in_window

# a module-level fake datetime: _effective_limit_kb/_update_limiter read
# the clock through drivesync.worker.datetime, so tests pin it here
AT_PEAK = datetime(2026, 11, 21, 23, 45)      # inside 22:00-06:00
OFF_PEAK = datetime(2026, 11, 21, 12, 0)      # outside that window


def _pin_clock(monkeypatch, moment):
    import drivesync.worker as w

    class _FakeDT(datetime):
        @classmethod
        def now(cls):
            return moment

    monkeypatch.setattr(w, "datetime", _FakeDT)


def _worker(env, cfg_over=None):
    cfg = {"bandwidth_limit_kb": 64, "peak_from": "22:00",
           "peak_to": "06:00", "bandwidth_limit_kb_peak": 8}
    cfg.update(cfg_over or {})
    return Worker(env.store, cfg)


# ---- rate selection -----------------------------------------------------------

def test_effective_limit_inside_peak_window(env, monkeypatch):
    _pin_clock(monkeypatch, AT_PEAK)
    assert _worker(env)._effective_limit_kb() == 8


def test_effective_limit_outside_peak_window(env, monkeypatch):
    _pin_clock(monkeypatch, OFF_PEAK)
    assert _worker(env)._effective_limit_kb() == 64


def test_no_peak_window_means_general_limit(env, monkeypatch):
    _pin_clock(monkeypatch, AT_PEAK)
    w = _worker(env, {"peak_from": "", "peak_to": ""})
    assert w._effective_limit_kb() == 64


def test_peak_limit_zero_falls_back_to_general(env, monkeypatch):
    _pin_clock(monkeypatch, AT_PEAK)
    w = _worker(env, {"bandwidth_limit_kb_peak": 0})
    assert w._effective_limit_kb() == 64


def test_unlimited_stays_unlimited_outside_peak(env, monkeypatch):
    _pin_clock(monkeypatch, OFF_PEAK)
    w = _worker(env, {"bandwidth_limit_kb": 0})
    assert w._effective_limit_kb() == 0


# ---- limiter swapping ----------------------------------------------------------

def test_update_limiter_swaps_rate_and_reports_status(env, monkeypatch):
    # construct OFF-peak so the init-time update performs no swap,
    # regardless of the real wall clock this test runs under
    _pin_clock(monkeypatch, OFF_PEAK)
    w = _worker(env)
    assert w.limiter.rate == 64 * 1024
    assert w.status["peak_active"] is False
    _pin_clock(monkeypatch, AT_PEAK)
    assert w._update_limiter() is True
    assert isinstance(w.limiter, TokenBucket)
    assert w.limiter.rate == 8 * 1024
    assert w.status["bandwidth_now_kb"] == 8
    assert w.status["peak_active"] is True
    # swapping back when the window ends
    _pin_clock(monkeypatch, OFF_PEAK)
    assert w._update_limiter() is True
    assert w.limiter.rate == 64 * 1024
    assert w.status["peak_active"] is False
    # idempotent when nothing changed
    assert w._update_limiter() is False


def test_update_limiter_builds_none_for_unlimited(env, monkeypatch):
    _pin_clock(monkeypatch, AT_PEAK)
    _pin_clock(monkeypatch, OFF_PEAK)
    w = _worker(env, {"bandwidth_limit_kb": 0,
                      "bandwidth_limit_kb_peak": 0})
    assert w.limiter is None
    _pin_clock(monkeypatch, AT_PEAK)
    # unlimited stays unlimited inside the peak window: rate unchanged,
    # but the status still reports the active peak window
    assert w._update_limiter() is False
    assert w.limiter is None
    assert w.status["bandwidth_now_kb"] == 0
    assert w.status["peak_active"] is True


def test_apply_config_swaps_to_peak_rate(env, monkeypatch):
    """The dashboard path: settings saved while the peak window is live.

    Regression: apply_config used to pre-assign the new general rate and
    then compare it with itself, so the peak swap never happened.
    """
    _pin_clock(monkeypatch, AT_PEAK)
    w = Worker(env.store, {"bandwidth_limit_kb": 64})
    assert w.limiter is not None and w.limiter.rate == 64 * 1024
    w.apply_config({"bandwidth_limit_kb": 64, "peak_from": "22:00",
                    "peak_to": "06:00", "bandwidth_limit_kb_peak": 8})
    assert w.limiter.rate == 8 * 1024
    assert w.status["peak_active"] is True


# ---- config validation ---------------------------------------------------------

def test_peak_window_validation(env):
    """save_config is the validator; the env fixture keeps its writes
    inside the test's DRIVE_SYNC_HOME."""
    base = dict(bandwidth_limit_kb=32, bandwidth_limit_kb_peak=8)
    ok = save_config({**base, "peak_from": "22:00", "peak_to": "06:00"})
    assert ok["bandwidth_limit_kb_peak"] == 8
    assert ok["peak_from"] == "22:00"
    with pytest.raises(ValueError):
        save_config({**base, "peak_from": "22:00"})               # no end
    with pytest.raises(ValueError):
        save_config({**base, "peak_to": "06:00"})                 # no start
    with pytest.raises(ValueError):
        save_config({**base, "peak_from": "25:00", "peak_to": "06:00"})
    with pytest.raises(ValueError):
        save_config({**base, "peak_from": "22:00", "peak_to": "06:00",
                     "bandwidth_limit_kb_peak": -1})
    # both empty = no peak window, perfectly valid (the key is kept,
    # it is just unused without a window)
    ok = save_config(base)
    assert ok["peak_from"] == "" and ok["bandwidth_limit_kb_peak"] == 8


def test_peak_roundtrip_via_update_config(env, monkeypatch):
    _pin_clock(monkeypatch, AT_PEAK)
    cfg = update_config(bandwidth_limit_kb=64,
                        bandwidth_limit_kb_peak=8,
                        peak_from="22:00", peak_to="06:00")
    w = Worker(env.store, cfg)
    assert w._effective_limit_kb() == 8


# ---- midnight-span reuse -------------------------------------------------------

def test_peak_window_spans_midnight():
    assert in_window(dtime(23, 45), "22:00", "06:00")
    assert in_window(dtime(2, 0), "22:00", "06:00")
    assert not in_window(dtime(12, 0), "22:00", "06:00")
