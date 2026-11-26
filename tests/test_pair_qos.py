"""Phase 4.2 — per-pair QoS overrides.

Storage: four nullable columns on `pairs` (None = inherit global).
Engine: _effective_limit_kb(pair) nests pair overrides over the global
rate; _bucket_for_pair caches buckets by resolved rate. Tests pin the
worker clock like test_schedule.py so they are wall-clock independent.
"""

from datetime import datetime, time as dtime

import pytest

from drivesync.db import _validate_pair_qos
from drivesync import engine
from drivesync.worker import Worker, in_window

AT_PEAK = datetime(2026, 11, 21, 23, 45)      # inside 22:00-06:00 windows
OFF_PEAK = datetime(2026, 11, 21, 12, 0)


def _pin_clock(monkeypatch, moment):
    import drivesync.worker as w

    class _FakeDT(datetime):
        @classmethod
        def now(cls):
            return moment

    monkeypatch.setattr(w, "datetime", _FakeDT)


def _pair(store, **over):
    store.add_account("a@example.com", {})
    base = dict(account_id=1, local_path="/tmp/qos-pair", remote_id="r",
                bandwidth_limit_kb=8, peak_from="22:00", peak_to="06:00",
                bandwidth_limit_kb_peak=4)
    base.update(over)
    return store.add_pair(**base)


def _worker(env, cfg_over=None):
    cfg = {"bandwidth_limit_kb": 64, "peak_from": "22:00",
           "peak_to": "06:00", "bandwidth_limit_kb_peak": 16}
    cfg.update(cfg_over or {})
    return Worker(env.store, cfg)


# ---- storage + validation -----------------------------------------------------

def test_pair_columns_default_to_null(env):
    p = _pair(env.store, bandwidth_limit_kb=None, peak_from=None,
              peak_to=None, bandwidth_limit_kb_peak=None)
    assert p["bandwidth_limit_kb"] is None
    assert p["peak_from"] is None and p["peak_to"] is None
    assert p["bandwidth_limit_kb_peak"] is None


def test_add_pair_persists_overrides(env):
    p = _pair(env.store)
    assert p["bandwidth_limit_kb"] == 8
    assert p["peak_from"] == "22:00" and p["peak_to"] == "06:00"
    assert p["bandwidth_limit_kb_peak"] == 4


def test_validate_pair_qos_matrix():
    assert _validate_pair_qos(None, None, None, None) == (None,) * 4
    assert _validate_pair_qos("", "", "", "") == (None,) * 4
    assert _validate_pair_qos(8, "22:00", "06:00", 4) == \
        (8, "22:00", "06:00", 4)
    assert _validate_pair_qos("8", "22:00", "06:00", "4") == \
        (8, "22:00", "06:00", 4)                       # strings normalize
    with pytest.raises(ValueError):
        _validate_pair_qos(-1, None, None, None)
    with pytest.raises(ValueError):
        _validate_pair_qos("fast", None, None, None)
    with pytest.raises(ValueError):
        _validate_pair_qos(8, "25:00", "06:00", None)
    with pytest.raises(ValueError):
        _validate_pair_qos(8, "22:00", None, None)     # half window
    with pytest.raises(ValueError):
        _validate_pair_qos(8, None, "06:00", None)
    with pytest.raises(ValueError):
        _validate_pair_qos(8, "22:00", "6:00", None)   # not HH:MM shape


def test_update_pair_partial_edit_validates_resolved_quartet(env):
    p = _pair(env.store)                    # window 22:00-06:00 already set
    # peak cap alone is fine: the window is already complete
    env.store.update_pair(p["id"], bandwidth_limit_kb_peak=12)
    assert env.store.pair(p["id"])["bandwidth_limit_kb_peak"] == 12
    # clearing one half of the live window must be rejected
    with pytest.raises(ValueError):
        env.store.update_pair(p["id"], peak_to=None)
    # setting a window on a pair with no window needs both halves
    p2 = _pair(env.store, local_path="/tmp/qos-other",
               bandwidth_limit_kb=None, peak_from=None,
               peak_to=None, bandwidth_limit_kb_peak=None)
    with pytest.raises(ValueError):
        env.store.update_pair(p2["id"], peak_from="09:00")
    env.store.update_pair(p2["id"], peak_from="09:00", peak_to="17:00")
    assert env.store.pair(p2["id"])["peak_to"] == "17:00"


def test_migrated_columns_exist(env):
    cols = {row[1] for row in
            env.store._conn.execute("PRAGMA table_info(pairs)")}
    assert {"bandwidth_limit_kb", "peak_from", "peak_to",
            "bandwidth_limit_kb_peak"} <= cols


# ---- rate resolution ----------------------------------------------------------

def test_no_override_inherits_global(env, monkeypatch):
    _pin_clock(monkeypatch, AT_PEAK)
    p = _pair(env.store, bandwidth_limit_kb=None, peak_from=None,
              peak_to=None, bandwidth_limit_kb_peak=None)
    w = _worker(env)
    assert w._effective_limit_kb(p) == 16          # global peak cap live


def test_pair_bandwidth_override_wins_anywhere(env, monkeypatch):
    _pin_clock(monkeypatch, OFF_PEAK)
    p = _pair(env.store, peak_from=None, peak_to=None,
              bandwidth_limit_kb_peak=None)
    assert _worker(env)._effective_limit_kb(p) == 8
    _pin_clock(monkeypatch, AT_PEAK)
    assert _worker(env)._effective_limit_kb(p) == 8   # no pair peak cap


def test_pair_peak_window_switches_to_pair_peak(env, monkeypatch):
    _pin_clock(monkeypatch, AT_PEAK)
    p = _pair(env.store)
    assert _worker(env)._effective_limit_kb(p) == 4
    _pin_clock(monkeypatch, OFF_PEAK)
    assert _worker(env)._effective_limit_kb(p) == 8   # pair general cap


def test_pair_peak_zero_falls_back_then_global(env, monkeypatch):
    _pin_clock(monkeypatch, AT_PEAK)
    p = _pair(env.store, bandwidth_limit_kb_peak=0)
    assert _worker(env)._effective_limit_kb(p) == 8   # 0 -> pair general
    p2 = _pair(env.store, local_path="/tmp/qos-zero",
               bandwidth_limit_kb=None, bandwidth_limit_kb_peak=0,
               peak_from="22:00", peak_to="06:00")
    assert _worker(env)._effective_limit_kb(p2) == 16  # -> global peak cap


def test_pair_window_without_caps_is_a_noop(env, monkeypatch):
    _pin_clock(monkeypatch, OFF_PEAK)
    p = _pair(env.store, bandwidth_limit_kb=None,
              bandwidth_limit_kb_peak=None)         # window only
    assert _worker(env)._effective_limit_kb(p) == 64  # global base


def test_midnight_span_reuses_in_window():
    assert in_window(dtime(23, 45), "22:00", "06:00")
    assert not in_window(dtime(12, 0), "22:00", "06:00")


# ---- bucket cache --------------------------------------------------------------

def test_bucket_cache_reuses_and_rebuilds(env, monkeypatch):
    _pin_clock(monkeypatch, AT_PEAK)
    p = _pair(env.store)
    w = _worker(env)
    b1, kb1 = w._bucket_for_pair(p)
    assert kb1 == 4 and b1 is w._bucket_for_pair(p)[0]   # cached identity
    env.store.update_pair(p["id"], bandwidth_limit_kb_peak=32)
    p = env.store.pair(p["id"])
    b2, kb2 = w._bucket_for_pair(p)
    assert kb2 == 32 and b2 is not b1
    # a global rate change clears inherited caches too
    _pin_clock(monkeypatch, OFF_PEAK)
    w.apply_config({"bandwidth_limit_kb": 128, "peak_from": "",
                    "peak_to": "", "bandwidth_limit_kb_peak": 0})
    b3, kb3 = w._bucket_for_pair(env.store.pair(p["id"]))
    assert kb3 == 8 and b3.rate == 8 * 1024


def test_unlimited_pair_gets_none_bucket(env):
    p = _pair(env.store, bandwidth_limit_kb=0, peak_from=None,
              peak_to=None, bandwidth_limit_kb_peak=None)
    bucket, kb = _worker(env)._bucket_for_pair(p)
    assert kb == 0 and bucket is None


# ---- pass loop wiring ----------------------------------------------------------

def test_pass_loop_sets_drive_limiter_per_pair(env, monkeypatch):
    _pin_clock(monkeypatch, OFF_PEAK)
    env.store.add_account("a@example.com", {})
    p1 = _pair(env.store, local_path="/tmp/qos-a", bandwidth_limit_kb=8,
               peak_from=None, peak_to=None, bandwidth_limit_kb_peak=None)
    p2 = _pair(env.store, local_path="/tmp/qos-b", bandwidth_limit_kb=24,
               peak_from=None, peak_to=None, bandwidth_limit_kb_peak=None)
    seen = []

    def fake_sync_pair(store, cfg, pair, drive, **kw):
        seen.append((pair["id"], drive.limiter,
                     drive.limiter.rate if drive.limiter else None))
        return type("S", (), {"pushed": 0, "pulled": 0, "deleted": 0,
                              "renamed": 0, "conflicts": 0,
                              "errors": 0})(), True

    monkeypatch.setattr(engine, "sync_pair", fake_sync_pair)
    w = _worker(env, {"bandwidth_limit_kb": 0, "peak_from": "",
                      "peak_to": "", "bandwidth_limit_kb_peak": 0})
    w.run_pass()
    rates = {pid: rate for pid, _, rate in seen}
    assert rates[p1["id"]] == 8 * 1024
    assert rates[p2["id"]] == 24 * 1024
    # the per-pair status carries the resolved rate for the UIs
    assert w.status["pairs"][p1["id"]]["bandwidth_kb"] == 8
    assert w.status["pairs"][p2["id"]]["bandwidth_kb"] == 24
