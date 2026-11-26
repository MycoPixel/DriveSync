"""Phase 4.3 — metered-network awareness.

Detection (netstate) answers "is the connection metered?" with a fail-
open contract: unknown must never stop syncing. The worker defers only
AUTOMATIC passes while metered + pause, caps every rate while throttle,
and mirrors the last detection into status["network"] for both UIs.
DRIVE_SYNC_METERED is the env override (and the test hook).
"""


import pytest

from drivesync import engine, netstate
from drivesync.worker import Worker


@pytest.fixture(autouse=True)
def _fresh_netcache(monkeypatch):
    """Never inherit a cached detection (or the developer's env) between
    tests; also scrub the override so busctl/nmcli paths are exercised."""
    monkeypatch.delenv("DRIVE_SYNC_METERED", raising=False)
    netstate.clear_cache()
    yield
    netstate.clear_cache()


def _metered_worker(store, **cfg_over):
    cfg = {"respect_metered": True, "metered_action": "pause",
           "metered_bandwidth_kb": 32, "bandwidth_limit_kb": 64,
           "peak_from": "", "peak_to": "", "bandwidth_limit_kb_peak": 0,
           "sync_interval": 60}
    cfg.update(cfg_over)
    return Worker(store, cfg)


# ---- netstate detection -------------------------------------------------

def test_env_override_yes_and_no(monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_METERED", "yes")
    assert netstate.is_metered() == (True, "env")
    netstate.clear_cache()
    monkeypatch.setenv("DRIVE_SYNC_METERED", "no")
    assert netstate.is_metered() == (False, "env")


def test_env_override_junk_falls_through(monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_METERED", "maybe")
    # hermetic: never consult the host's real NetworkManager (a developer
    # machine with nmcli would otherwise return "no" instead of unknown)
    monkeypatch.setattr(netstate.shutil, "which", lambda name: None)
    value, source = netstate.is_metered()
    assert value is None and source == "unavailable"


def test_result_is_cached_until_ttl(monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_METERED", "yes")
    assert netstate.is_metered() == (True, "env")
    monkeypatch.setenv("DRIVE_SYNC_METERED", "no")
    # TTL still valid: the cached 'yes' survives the env change
    assert netstate.is_metered() == (True, "env")
    netstate.clear_cache()
    assert netstate.is_metered() == (False, "env")


def test_busctl_yes_no_and_guess(monkeypatch):
    """'busctl get-property' prints 'u <n>'; guesses honour the flag."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        value = cmd[-1]                       # last argv = "Metered"
        return type("P", (), {"returncode": 0,
                              "stdout": f"u {calls.index(value) + 1}\n",
                              "stderr": ""})()

    # simpler: patch per expected value
    def run_for(value):
        return lambda cmd, **kw: type("P", (), {
            "returncode": 0, "stdout": f"u {value}\n", "stderr": ""})()

    monkeypatch.setattr(netstate.shutil, "which",
                        lambda name: "/usr/bin/busctl" if name == "busctl"
                        else None)
    for value, expected in (("1", True), ("2", False),
                            ("3", True), ("4", False)):
        monkeypatch.setattr(netstate.subprocess, "run", run_for(value))
        assert netstate.is_metered(0) == (expected, "NetworkManager")


def test_busctl_failure_falls_through(monkeypatch):
    def broken_run(cmd, **kw):
        raise OSError("no busctl")

    monkeypatch.setattr(netstate.shutil, "which", lambda name: "/usr/bin/busctl")
    monkeypatch.setattr(netstate.subprocess, "run", broken_run)
    monkeypatch.setattr(netstate.shutil, "which",
                        lambda name: None)      # nmcli also missing
    value, source = netstate.is_metered(0)
    assert value is None and source == "unavailable"


def test_busctl_nonzero_return_falls_through(monkeypatch):
    monkeypatch.setattr(netstate.shutil, "which", lambda name: "/usr/bin/busctl")
    monkeypatch.setattr(netstate.subprocess, "run",
                        lambda cmd, **kw: type("P", (), {
                            "returncode": 1, "stdout": "", "stderr": "x"})())
    monkeypatch.setattr(netstate.shutil, "which",
                        lambda name: None if name == "nmcli" else "/usr/bin/busctl")
    value, source = netstate.is_metered(0)
    assert value is None and source == "unavailable"


def test_nmcli_parses_guess_values(monkeypatch):
    out = ("GENERAL.METERED:guess-yes\n" * 3)

    def run_for(out):
        return lambda cmd, **kw: type("P", (), {
            "returncode": 0, "stdout": out, "stderr": ""})()

    monkeypatch.setattr(netstate.shutil, "which", lambda name: "/usr/bin/nmcli")
    for out, expected in (("GENERAL.METERED:guess-yes\n", True),
                          ("GENERAL.METERED:no\n", False),
                          ("GENERAL.METERED:guess-no\n", False),
                          ("GENERAL.METERED:(unknown)\n", None),
                          ("", None)):
        monkeypatch.setattr(netstate.subprocess, "run", run_for(out))
        assert netstate.is_metered(0)[0] is expected


# ---- config validation --------------------------------------------------

def test_config_defaults_and_roundtrip():
    from drivesync import config
    merged = config.update_config(metered_action="throttle",
                                  metered_bandwidth_kb=128,
                                  respect_metered=False)
    assert merged["respect_metered"] is False
    assert merged["metered_action"] == "throttle"
    assert merged["metered_bandwidth_kb"] == 128
    with pytest.raises(ValueError):
        config.update_config(metered_action="ignore")
    with pytest.raises(ValueError):
        config.update_config(metered_bandwidth_kb=-5)
    config.update_config(metered_action="pause",
                         metered_bandwidth_kb=1024,
                         respect_metered=True)


# ---- worker: pause gate -------------------------------------------------

def test_pause_gate_blocks_with_one_log_row():
    store = _store_with_pair()
    w = _metered_worker(store)                    # action=pause; env unset?
    # detection is unavailable in tests → fail open unless env says yes
    netstate.clear_cache()
    import os
    os.environ["DRIVE_SYNC_METERED"] = "yes"
    try:
        netstate.clear_cache()
        assert w._metered_blocks_auto() is True
        assert w.status["network"]["metered"] is True
        assert w._metered_blocks_auto() is True   # deferred again
        rows = store.recent_events(limit=50, action="metered-pause")
        assert len(rows) == 1                     # transition, not per pass
    finally:
        os.environ.pop("DRIVE_SYNC_METERED")


def test_pause_gate_resumes_with_one_log_row():
    import os
    store = _store_with_pair()
    w = _metered_worker(store)
    os.environ["DRIVE_SYNC_METERED"] = "yes"
    netstate.clear_cache()
    w._metered_blocks_auto()
    os.environ["DRIVE_SYNC_METERED"] = "no"
    netstate.clear_cache()
    assert w._metered_blocks_auto() is False
    rows = store.recent_events(limit=50, action="metered-resume")
    assert len(rows) == 1
    w._metered_blocks_auto()                      # no second resume row
    assert len(store.recent_events(limit=50, action="metered-resume")) == 1
    os.environ.pop("DRIVE_SYNC_METERED")


def test_respect_off_ignores_metered_state():
    import os
    store = _store_with_pair()
    w = _metered_worker(store, respect_metered=False)
    os.environ["DRIVE_SYNC_METERED"] = "yes"
    netstate.clear_cache()
    assert w._metered_blocks_auto() is False
    assert w.status["network"]["metered"] is None   # never even checked
    os.environ.pop("DRIVE_SYNC_METERED")


def test_throttle_action_never_pauses():
    import os
    store = _store_with_pair()
    w = _metered_worker(store, metered_action="throttle")
    os.environ["DRIVE_SYNC_METERED"] = "yes"
    netstate.clear_cache()
    assert w._metered_blocks_auto() is False      # throttle, not pause
    os.environ.pop("DRIVE_SYNC_METERED")


# ---- worker: throttle cap -----------------------------------------------

def test_throttle_caps_global_unlimited_and_pairs(monkeypatch):
    import os
    store = _store_with_pair()
    # hermetic: the host's real NetworkManager must not decide the
    # "metered cleared" half of the test
    monkeypatch.setattr(netstate.shutil, "which", lambda name: None)
    os.environ["DRIVE_SYNC_METERED"] = "yes"
    netstate.clear_cache()
    w = _metered_worker(store, metered_action="throttle",
                        metered_bandwidth_kb=32, bandwidth_limit_kb=64)
    assert w._effective_limit_kb() == 32          # global 64 -> cap 32
    pair = store.pair(1)
    store.update_pair(1, bandwidth_limit_kb=8)    # explicit pair override
    pair = store.pair(1)
    assert w._effective_limit_kb(pair) == 8       # min(8, 32)
    store.update_pair(1, bandwidth_limit_kb=128)
    assert w._effective_limit_kb(store.pair(1)) == 32   # min(128, 32)
    w2 = _metered_worker(store, metered_action="throttle",
                         metered_bandwidth_kb=32, bandwidth_limit_kb=0)
    assert w2._effective_limit_kb() == 32         # unlimited -> cap
    os.environ.pop("DRIVE_SYNC_METERED")
    netstate.clear_cache()
    assert w._effective_limit_kb() == 64          # metered cleared
    assert w2._effective_limit_kb() == 0          # ...and unlimited again


def test_throttle_zero_cap_is_no_cap():
    import os
    store = _store_with_pair()
    os.environ["DRIVE_SYNC_METERED"] = "yes"
    netstate.clear_cache()
    w = _metered_worker(store, metered_action="throttle",
                        metered_bandwidth_kb=0, bandwidth_limit_kb=64)
    assert w._effective_limit_kb() == 64
    os.environ.pop("DRIVE_SYNC_METERED")


# ---- loop wiring ----------------------------------------------------------

class _Stats:
    pushed = pulled = deleted = renamed = conflicts = errors = 0


def test_loop_defers_automatic_pass_but_not_explicit():
    import os
    store = _store_with_pair()
    os.environ["DRIVE_SYNC_METERED"] = "yes"
    netstate.clear_cache()
    w = _metered_worker(store)
    called = []

    def fake_sync(s, cfg, pair, drive, **kw):
        called.append(pair["id"])
        return _Stats(), True

    engine_backup = engine.sync_pair
    engine.sync_pair = fake_sync

    class _FakeDrive:
        limiter = None              # the pass loop assigns per-pair buckets

    w.make_drive = lambda acc: _FakeDrive()
    try:
        def fake_wait(timeout=None):
            if fake_wait.called:       # second wait: break out
                w.stop_flag.set()
                return False
            fake_wait.called = True    # first wait: pretend wake fired
            return True
        fake_wait.called = False
        w._wait_wake = fake_wait
        w._loop()
        assert called == []            # automatic pass deferred
        assert any(r["action"] == "metered-pause"
                   for r in store.recent_events(limit=50))
        # explicit request (sync_now) is never blocked
        w.stop_flag.clear()
        w.sync_now(store.pair(1)["id"])
        os.environ["DRIVE_SYNC_METERED"] = "no"
        netstate.clear_cache()
        w.run_pass([store.pair(1)["id"]])
        assert called == [store.pair(1)["id"]]
    finally:
        engine.sync_pair = engine_backup
        os.environ.pop("DRIVE_SYNC_METERED")
        try:
            w.stop()
        except RuntimeError:
            pass


# ---- helpers --------------------------------------------------------------

def _store_with_pair(tmp_factory=None):
    """A standalone store with one pair (no account — detection tests
    never sync)."""
    from drivesync.db import Store
    import tempfile
    d = tempfile.mkdtemp()
    store = Store(f"{d}/state.db")
    store.add_account("a@example.com", {})
    store.add_pair(1, f"{d}/local", "remote-x")
    return store
