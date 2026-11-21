"""Worker scheduling window + sync request queue behaviour."""

from datetime import time as dtime

from drivesync.worker import Worker, in_window


def test_in_window_bounds():
    assert in_window(dtime(9, 0), "", "")
    assert in_window(dtime(9, 0), "08:00", "23:00")
    assert not in_window(dtime(3, 0), "08:00", "23:00")
    assert not in_window(dtime(23, 0), "08:00", "23:00")   # end exclusive


def test_in_window_overnight():
    assert in_window(dtime(23, 30), "22:00", "06:00")
    assert in_window(dtime(2, 0), "22:00", "06:00")
    assert not in_window(dtime(12, 0), "22:00", "06:00")


def test_in_window_partial_bounds():
    assert in_window(dtime(9, 0), "08:00", "")
    assert not in_window(dtime(7, 0), "08:00", "")
    assert in_window(dtime(3, 0), "", "06:00")
    assert not in_window(dtime(7, 0), "", "06:00")


def test_in_window_invalid_degrades_to_always():
    assert in_window(dtime(3, 0), "bad", "23:00")
    assert in_window(dtime(3, 0), "08:00", "b:d")


def test_sync_now_queues_and_coalesces(env):
    w = Worker(env.store, env.cfg)
    w.sync_now(env.pair["id"])
    w.sync_now(env.pair["id"])           # duplicate requests coalesce
    assert w._pending == {env.pair["id"]}


def test_run_pass_accepts_id_list(env, monkeypatch):
    """The watcher path passes a list of pair ids; one pair, empty dir."""
    env.store.add_account("a@example.com", {})   # env pairs reference account 1
    w = Worker(env.store, env.cfg)
    monkeypatch.setattr(w, "make_drive", lambda acc: env.drive)  # no HTTP
    w.run_pass([env.pair["id"]])         # list form
    assert w.status["last_stats"]["pair_id"] == env.pair["id"]
    assert env.store.count_events() >= 1     # summary row logged


def test_run_pass_returns_totals(env, monkeypatch):
    """'drivesync sync' unpacks (stats, ok) — never return None."""
    env.store.add_account("a@example.com", {})
    (env.local / "a.txt").write_text("alpha")
    w = Worker(env.store, env.cfg)
    monkeypatch.setattr(w, "make_drive", lambda acc: env.drive)
    stats, ok = w.run_pass()
    assert ok is True
    assert int(stats.pushed) == 1 and int(stats.errors) == 0
    transfers = w.status["transfers"]
    assert len(transfers) == 1
    assert transfers[0]["direction"] == "upload"
    assert transfers[0]["path"] == "a.txt"
    assert transfers[0]["status"] == "completed"
    assert transfers[0]["size"] == 5


def test_run_pass_reports_failure_and_marks_pair_stale(env, monkeypatch):
    env.store.add_account("a@example.com", {})
    env.store.update_pair(env.pair["id"],
                          local_path=str(env.local / "missing"))
    w = Worker(env.store, env.cfg)
    monkeypatch.setattr(w, "make_drive", lambda acc: env.drive)
    stats, ok = w.run_pass()
    assert ok is False and int(stats.errors) == 1
    assert env.store.kv_get(f"stale:{env.pair['id']}") == "1"


def test_run_pass_reports_skip_while_running(env):
    w = Worker(env.store, env.cfg)
    w.status["running"] = True
    stats, ok = w.run_pass()
    assert ok is False and int(stats.errors) == 0
    assert any(e["action"] == "sync-skipped"
               for e in env.store.recent_events(limit=5))


def test_config_reload_updates_interval_and_limiter(env):
    w = Worker(env.store, {"sync_interval": 30, "bandwidth_limit_kb": 0},
               config_source=lambda: {"sync_interval": 90,
                                      "bandwidth_limit_kb": 64})
    w._reload_config()
    assert w.interval == 90 and w._limit_kb == 64 and w.limiter is not None


def test_interval_override_survives_config_reload(env):
    w = Worker(env.store, {"sync_interval": 30}, interval=5,
               config_source=lambda: {"sync_interval": 90})
    w._reload_config()
    assert w.interval == 5


def test_pause_resume_keeps_the_thread(env):
    w = Worker(env.store, env.cfg)
    w.start()
    thread = w._thread
    try:
        w.pause()
        assert w.status["paused"] and w._paused.is_set()
        w.resume()
        assert not w.status["paused"] and not w._paused.is_set()
        assert w._thread is thread        # no thread recreation
        assert thread.is_alive()
    finally:
        w.stop()


def test_run_pass_clears_previous_error(env, monkeypatch):
    env.store.add_account("a@example.com", {})
    w = Worker(env.store, env.cfg)
    monkeypatch.setattr(w, "make_drive", lambda acc: env.drive)
    w.status["last_error"] = "old failure"
    stats, ok = w.run_pass([env.pair["id"]])
    assert ok and w.status["last_error"] is None


def test_run_pass_records_per_pair_status(env, monkeypatch):
    """The GUI/dashboard read status['pairs'] for the per-pair columns."""
    env.store.add_account("a@example.com", {})
    (env.local / "a.txt").write_text("alpha")
    w = Worker(env.store, env.cfg)
    monkeypatch.setattr(w, "make_drive", lambda acc: env.drive)
    w.run_pass([env.pair["id"]])
    info = w.status["pairs"][env.pair["id"]]
    assert info["ok"] is True and info["error"] is None
    assert info["stats"]["pushed"] == 1
    assert info["ts"] > 0


def test_run_pass_records_error_per_pair(env, monkeypatch):
    env.store.add_account("a@example.com", {})
    env.store.update_pair(env.pair["id"],
                          local_path=str(env.local / "missing"))
    w = Worker(env.store, env.cfg)
    monkeypatch.setattr(w, "make_drive", lambda acc: env.drive)
    w.run_pass([env.pair["id"]])
    info = w.status["pairs"][env.pair["id"]]
    assert info["ok"] is False
    assert info["error"]                 # message pulled from the event log


def test_loop_arms_next_run_and_pause_clears_it(env):
    """The countdown comes from status['next_run'] (None while paused)."""
    import time as _time
    w = Worker(env.store, {"sync_interval": 500})
    w.start()
    try:
        deadline = _time.time() + 5
        while _time.time() < deadline and w.status["next_run"] is None:
            _time.sleep(0.05)
        assert w.status["next_run"] is not None
        assert 0 < w.status["next_run"] - _time.time() <= 500
        w.pause()
        deadline = _time.time() + 5
        while _time.time() < deadline and w.status["next_run"] is not None:
            _time.sleep(0.05)
        assert w.status["next_run"] is None
    finally:
        w.stop()
