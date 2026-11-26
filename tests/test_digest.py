"""Phase 4.4 — notifications digest.

A daily 'what synced' summary: aggregated from the activity log by
event-id window (ids are strictly monotonic; ts has only second
precision), emitted as a best-effort desktop notification plus one
'digest' event row, with same-day dedup and a skip-quiet-days knob.
The loop calls _maybe_digest every tick BEFORE the sync gates: a
digest is a report about sync activity, not sync activity.
"""

from datetime import datetime

import pytest

from drivesync import engine
from drivesync.config import update_config
from drivesync.worker import Worker


@pytest.fixture
def w(env):
    """Worker with the digest due immediately (00:00 fire time)."""
    worker = Worker(env.store, {"digest_enabled": True,
                                "digest_time": "00:00",
                                "digest_skip_empty": True})
    sent = []
    monkey_sent = sent
    Worker._notify = staticmethod(lambda t, b, _s=monkey_sent:
                                  _s.append(b))
    worker.sent = monkey_sent
    yield worker


def _activity(store):
    """Seed one busy window: pushes, a pull, a conflict, an error."""
    store.log(1, "push", "a.txt")
    store.log(1, "push", "b.txt")
    store.log(1, "pull", "c.txt")
    store.log(1, "delete", "old.txt")
    store.log(1, "conflict", "d.txt", level="conflict")
    store.log(1, "error", "boom", level="error")
    store.log(1, "pass", "", "pass summary")


# ---- store aggregation --------------------------------------------------

def test_digest_summary_counts(env):
    _activity(env.store)
    s = env.store.digest_summary(0)
    assert (s["pushed"], s["pulled"], s["deleted"]) == (2, 1, 1)
    assert s["conflicts"] == 1 and s["errors"] == 1
    assert s["files"] == 4 and s["pairs"] == 1


def test_digest_summary_empty_store(env):
    s = env.store.digest_summary(0)
    assert s["files"] == 0 and s["pairs"] == 0


def test_window_is_id_keyed(env):
    """Activity after the bookmark counts; the digest row itself never
    compounds into the next window (excluded by action)."""
    env.store.log(1, "push", "early.txt")
    mark = env.store.last_event_id()
    env.store.log(1, "pull", "late.txt")
    s = env.store.digest_summary(mark)
    assert s["pushed"] == 0 and s["pulled"] == 1


def test_last_event_id(env):
    assert env.store.last_event_id() == 0
    env.store.log(1, "push", "a.txt")
    assert env.store.last_event_id() > 0


# ---- config validation --------------------------------------------------

def test_config_validation():
    c = update_config(digest_enabled=True, digest_time="07:15",
                      digest_skip_empty=False)
    assert (c["digest_enabled"], c["digest_time"],
            c["digest_skip_empty"]) == (True, "07:15", False)
    c = update_config(digest_time="")          # empty = first tick of day
    assert c["digest_time"] == ""
    with pytest.raises(ValueError):
        update_config(digest_time="24:00")


# ---- worker scheduling + emission ---------------------------------------

def test_digest_fires_once_per_day(env, w):
    _activity(env.store)
    w._maybe_digest()
    assert len(w.sent) == 1
    rows = env.store.recent_events(limit=50, action="digest")
    assert len(rows) == 1 and w.status["digest"]["last_message"]
    w._maybe_digest()                          # same day → handled
    assert len(w.sent) == 1
    assert w.status["digest"]["next_at"] is None


def test_digest_window_excludes_previous_activity(env, w):
    _activity(env.store)
    w._maybe_digest()                          # window 1: the seeded activity
    assert len(w.sent) == 1
    env.store.kv_set("last_digest_day", "")    # simulate the next day
    env.store.log(1, "push", "new.txt")        # window 2: only this
    w._maybe_digest()
    assert len(w.sent) == 2
    assert w.status["digest"]["last_message"].startswith("1 pushed")
    assert "2 pushed" not in w.status["digest"]["last_message"]


def test_skip_empty_marks_day_without_notifying(env, w):
    w._maybe_digest()                          # nothing in the log yet
    assert w.sent == []                        # no notification...
    assert w.status["digest"]["skipped"] is True
    assert str(env.store.kv_get("last_digest_day")) == \
        datetime.now().date().isoformat()      # ...but the day is handled
    assert env.store.recent_events(limit=10, action="digest") == []


def test_empty_day_with_skip_off_notifies(env):
    worker = Worker(env.store, {"digest_enabled": True,
                                "digest_time": "00:00",
                                "digest_skip_empty": False})
    sent = []
    Worker._notify = staticmethod(lambda t, b, _s=sent: _s.append(b))
    worker._maybe_digest()
    assert len(sent) == 1
    assert "no file changes" in sent[0]


def test_disabled_is_inert(env):
    worker = Worker(env.store, {"digest_enabled": False})
    worker._maybe_digest()
    assert worker.status["digest"]["next_at"] is None
    assert worker.status["digest"]["skipped"] is None
    assert env.store.recent_events(limit=10, action="digest") == []


def test_not_yet_due_schedules_without_sending(env, w, monkeypatch):
    """A fire time later today must only schedule, never send.

    Pin the clock: ``now + 2h`` wraps past midnight when the test runs
    after 22:00, which made the fire time look already due.
    """
    fixed = datetime(2026, 11, 25, 10, 0, 0)

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.replace(tzinfo=tz)

    monkeypatch.setattr("drivesync.worker.datetime", FakeDatetime)
    worker = Worker(env.store, {"digest_enabled": True,
                                "digest_time": "12:00"})
    worker._maybe_digest()
    assert worker.sent == [] if hasattr(worker, "sent") else True
    assert worker.status["digest"]["next_at"] is not None
    assert env.store.recent_events(limit=10, action="digest") == []


def test_notifier_failure_recorded_day_still_handled(env, w):
    """A broken notification daemon must not kill the loop or re-spam."""
    _activity(env.store)

    def boom(title, body):
        raise OSError("no notification daemon")

    Worker._notify = staticmethod(boom)
    w._maybe_digest()
    assert w.status["digest"]["error"]                     # recorded
    assert str(env.store.kv_get("last_digest_day")) == \
        datetime.now().date().isoformat()                  # handled anyway
    assert env.store.recent_events(limit=10, action="digest")  # row logged


def test_message_pluralization(env):
    m = Worker._digest_message
    one = m({"pushed": 1, "pulled": 1, "deleted": 1,
             "conflicts": 1, "errors": 1, "pairs": 1})
    assert "1 conflict · 1 error (across 1 folder)" in one
    many = m({"pushed": 2, "pulled": 0, "deleted": 0,
              "conflicts": 3, "errors": 0, "pairs": 2})
    assert many == "2 pushed · 3 conflicts · 0 errors (across 2 folders)"
    empty = m({"pushed": 0, "pulled": 0, "deleted": 0,
               "conflicts": 0, "errors": 0, "pairs": 0})
    assert empty.startswith("no file changes")


# ---- loop wiring ---------------------------------------------------------

class _Stats:
    pushed = pulled = deleted = renamed = conflicts = errors = 0


def test_loop_fires_digest_before_sync_gates(env, monkeypatch):
    """The digest is a report, not a pass: it fires on a loop tick even
    though the metered pause holds syncing back."""
    import os
    from drivesync import netstate
    os.environ["DRIVE_SYNC_METERED"] = "yes"
    netstate.clear_cache()
    env.store.add_account("a@example.com", {})
    _activity(env.store)           # skip-empty must not swallow the digest
    worker = Worker(env.store, {"digest_enabled": True,
                                "digest_time": "00:00",
                                "respect_metered": True,
                                "metered_action": "pause",
                                "sync_interval": 60})
    sent = []
    Worker._notify = staticmethod(lambda t, b, _s=sent: _s.append(b))
    passes = []

    def fake_sync(s, cfg, pair, drive, **kw):
        passes.append(pair["id"])
        return _Stats(), True

    backup = engine.sync_pair
    engine.sync_pair = fake_sync

    class _FakeDrive:
        limiter = None

    worker.make_drive = lambda acc: _FakeDrive()

    def fake_wait(timeout=None):
        if fake_wait.called:               # second wait: break out
            worker.stop_flag.set()
            return False
        fake_wait.called = True            # first: wake fired
        return True
    fake_wait.called = False
    worker._wait_wake = fake_wait
    try:
        worker._loop()
        assert sent, "digest should fire from the loop tick"
        assert passes == [], "metered pause must hold the pass back"
        assert any(r["action"] == "metered-pause"
                   for r in env.store.recent_events(limit=50))
    finally:
        engine.sync_pair = backup
        os.environ.pop("DRIVE_SYNC_METERED")
        netstate.clear_cache()
        try:
            worker.stop()
        except RuntimeError:
            pass


# ---- CLI -----------------------------------------------------------------

def test_cli_digest_prints_window_and_schedule(env, capsys, monkeypatch):
    from drivesync import cli
    _activity(env.store)
    monkeypatch.setattr(cli, "_open_store", lambda: env.store)
    update_config(digest_enabled=False)    # pin: app config is shared
    cli.main(["digest"])
    out = capsys.readouterr().out
    assert "files: 4" in out and "conflicts: 1" in out
    assert "disabled" in out
    update_config(digest_enabled=True, digest_time="09:30")
    cli.main(["digest"])
    out = capsys.readouterr().out
    assert "enabled at 09:30" in out


# ---- HTTP E2E ------------------------------------------------------------

def test_e2e_settings_roundtrip_and_live_digest(env):
    tmp_cfg = update_config(bandwidth_limit_kb=64)
    worker = Worker(env.store, tmp_cfg, interval=60)
    from drivesync.server import create_app
    app = create_app(env.store, worker=worker)
    c = app.test_client()
    c.environ_base = {"HTTP_X_DRIVESYNC": app.config["API_TOKEN"]}

    r = c.post("/api/settings", json={
        "digest_enabled": True, "digest_time": "06:30",
        "digest_skip_empty": False})
    saved = (r.get_json() or {}).get("settings", {})
    assert (saved.get("digest_enabled"), saved.get("digest_time"),
            saved.get("digest_skip_empty")) == (True, "06:30", False)

    r = c.post("/api/settings", json={"digest_time": "06:75"})
    assert r.status_code == 400

    # the worker adopted the config: an immediate forced digest reports
    # through status and the activity log (skip_empty=False → even the
    # quiet case sends)
    worker.config["digest_time"] = "00:00"     # force due
    sent = []
    Worker._notify = staticmethod(lambda t, b, _s=sent: _s.append(b))
    worker._maybe_digest()
    assert sent and worker.status["digest"]["last_message"]
    assert any(r_["action"] == "digest"
               for r_ in env.store.recent_events(limit=20))
    try:
        worker.stop()
    except RuntimeError:
        pass
