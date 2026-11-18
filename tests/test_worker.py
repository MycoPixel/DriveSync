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
