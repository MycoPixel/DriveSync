"""Round 6: sync preview (dry-run), pause-until schedule, appearance keys."""

from datetime import datetime, timedelta, timezone

import pytest

from drivesync import engine
from drivesync.worker import Worker


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Same shape as test_server's fixture (kept local to this module)."""
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    from drivesync.db import Store
    from drivesync.server import create_app
    store = Store(tmp_path / "state.db")
    app = create_app(store, worker=None)
    c = app.test_client()
    c.environ_base["HTTP_X_DRIVESYNC"] = "1"
    yield c, store
    store.close()


def _seed_remote(env, name, content):
    """Put a file on the fake Drive via its real API."""
    seed = env.root / f"seed-{name}"
    seed.write_text(content)
    return env.drive.upload(seed, name, env.pair["remote_id"])


# ---- engine.preview_pair -------------------------------------------------------
def test_preview_matches_a_real_pass_but_touches_nothing(env):
    """preview_pair returns the same ops sync_pair would apply — without
    applying them, writing the base, or logging a single row."""
    (env.local / "a.txt").write_text("alpha")
    _seed_remote(env, "b.txt", "beta")
    before_base = env.base()
    before_events = env.store.count_events()
    before_files = sorted(p.name for p in env.local.iterdir())

    res = engine.preview_pair(env.store, env.cfg, env.pair, env.drive)

    assert res["error"] is None
    ops = {op[0] for op in res["ops"]}
    assert "upload" in ops and "download" in ops
    assert res["totals"]["uploads"] == 1 and res["totals"]["downloads"] == 1
    # nothing happened anywhere
    assert env.base() == before_base
    assert env.store.count_events() == before_events
    assert sorted(p.name for p in env.local.iterdir()) == before_files


def test_preview_order_matches_apply_order(env):
    """Ops come back in pass order: mkdirs, renames, transfers, deletions."""
    (env.local / "a.txt").write_text("alpha")
    (env.local / "sub").mkdir()
    _seed_remote(env, "gone.txt", "bye")
    res = engine.preview_pair(env.store, env.cfg, env.pair, env.drive)
    ranks = [{"mkdir_local": 0, "mkdir_remote": 0,
              "upload": 2, "download": 2,
              "delete_local": 3, "delete_remote": 3}.get(op[0], 2)
             for op in res["ops"]]
    assert ranks == sorted(ranks)


def test_preview_reports_fatal_scan_problems(env):
    env.store.update_pair(env.pair["id"], local_path=str(env.root / "missing"))
    env.pair = env.store.pair(env.pair["id"])
    res = engine.preview_pair(env.store, env.cfg, env.pair, env.drive)
    assert res["ops"] == []
    assert "local path missing" in res["error"]


def test_preview_flags_blocked_deletions(env):
    """The delete-safety limit is reported instead of silently filtering."""
    (env.local / "a.txt").write_text("x")
    _seed_remote(env, "f0.txt", "x")
    _seed_remote(env, "f1.txt", "x")
    env.sync()                                    # 3 files tracked
    env.cfg["delete_safety_limit"] = 1
    for name in ("a.txt", "f0.txt", "f1.txt"):
        (env.local / name).unlink()
    res = engine.preview_pair(env.store, env.cfg, env.pair, env.drive)
    assert res["blocked"] == 3
    assert all(op[0] not in engine.DESTRUCTIVE_OPS for op in res["ops"])
    assert res["totals"]["deletions"] == 0        # filtered OUT of the ops


def test_sync_pair_still_applies_after_the_refactor(env):
    """The extract-refactor must not change sync_pair's behavior."""
    (env.local / "a.txt").write_text("alpha")
    _seed_remote(env, "b.txt", "beta")
    stats, ok = env.sync()
    assert ok and int(stats.pushed) == 1 and int(stats.pulled) == 1
    assert (env.local / "b.txt").read_text() == "beta"
    # a second pass is a no-op: the upload and download really landed
    stats2, ok2 = env.sync()
    assert ok2 and int(stats2.pushed) == 0 and int(stats2.pulled) == 0


# ---- pause until tomorrow -----------------------------------------------------
def _ts(**kw):
    return datetime.now(timezone.utc) + timedelta(**kw)


def test_paused_pair_is_skipped_until_expiry(env, monkeypatch):
    env.store.add_account("a@example.com", {})
    (env.local / "a.txt").write_text("alpha")
    env.store.update_pair(env.pair["id"],
                          paused_until=_ts(hours=6).isoformat())
    w = Worker(env.store, env.cfg)
    monkeypatch.setattr(w, "make_drive", lambda acc: env.drive)
    stats, ok = w.run_pass()
    assert ok is True and int(stats.pushed) == 0      # skipped, not an error
    assert (env.local / "a.txt").read_text() == "alpha"   # nothing happened
    assert env.store.kv_get(f"pair_synced:{env.pair['id']}") is None


def test_expired_pause_auto_resumes_with_a_log_row(env, monkeypatch):
    env.store.add_account("a@example.com", {})
    (env.local / "a.txt").write_text("alpha")
    env.store.update_pair(env.pair["id"],
                          paused_until=_ts(minutes=-2).isoformat())
    w = Worker(env.store, env.cfg)
    monkeypatch.setattr(w, "make_drive", lambda acc: env.drive)
    stats, ok = w.run_pass()
    assert ok and int(stats.pushed) == 1
    row = env.store.pair(env.pair["id"])
    assert row["paused_until"] is None                # cleared
    actions = [r["action"] for r in env.store.recent_events(limit=10)]
    assert "auto-resume" in actions


def test_paused_until_junk_value_is_cleared_not_fatal(env, monkeypatch):
    """A pre-validation junk row (hand-edited db) must not stop syncing."""
    env.store.add_account("a@example.com", {})
    env.store._exec("UPDATE pairs SET paused_until='not-a-date' WHERE id=?",
                    (env.pair["id"],))
    w = Worker(env.store, env.cfg)
    monkeypatch.setattr(w, "make_drive", lambda acc: env.drive)
    stats, ok = w.run_pass()
    assert ok is True                                  # pair synced, pause dropped
    assert env.store.pair(env.pair["id"])["paused_until"] is None


def test_update_pair_validates_paused_until(env):
    with pytest.raises(ValueError, match="paused_until"):
        env.store.update_pair(env.pair["id"], paused_until="day after tomorrow")
    env.store.update_pair(env.pair["id"], paused_until=None)   # clearing is fine


# ---- server surface -----------------------------------------------------------
def test_pair_json_exposes_paused_until(client):
    c, store = client
    store.add_account("a@example.com", {})
    pair = store.add_pair(1, "/tmp/px", "root123")
    store.update_pair(pair["id"],
                      paused_until="2026-11-21T09:00:00+00:00")
    data = c.get("/api/pairs").get_json()
    p = next(x for x in data["pairs"] if x["id"] == pair["id"])
    assert p["paused_until"] == "2026-11-21T09:00:00+00:00"


def test_patch_paused_until_via_dashboard(client):
    c, store = client
    store.add_account("a@example.com", {})
    pair = store.add_pair(1, "/tmp/px", "root123")
    r = c.patch(f"/api/pairs/{pair['id']}", json={"paused_until": "bad"})
    assert r.status_code == 400                      # junk refused
    r = c.patch(f"/api/pairs/{pair['id']}",
                json={"paused_until": "2026-11-21T09:00:00+00:00"})
    assert r.get_json()["pair"]["paused_until"].startswith("2026-11-21")
    r = c.patch(f"/api/pairs/{pair['id']}", json={"paused_until": None})
    assert r.get_json()["pair"]["paused_until"] is None


def test_preview_endpoint_needs_a_real_pair(client):
    c, _ = client
    assert c.get("/api/pairs/999/preview").status_code == 404


def test_preview_endpoint_returns_ops(client, tmp_path, monkeypatch):
    c, store = client
    store.add_account("a@example.com", {})
    local = tmp_path / "loc"
    local.mkdir()
    (local / "a.txt").write_text("alpha")
    pair = store.add_pair(1, str(local), "rootX")

    class FakeDrive:
        def __init__(self, provider, limiter=None):
            pass

    monkeypatch.setattr("drivesync.server.Drive", FakeDrive)
    monkeypatch.setattr(engine, "scan_remote",
                        lambda drive, root, patterns, export_map=None,
                        include=None: {})
    r = c.get(f"/api/pairs/{pair['id']}/preview")
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["totals"]["uploads"] == 1
    assert any(op[0] == "upload" and op[1] == "a.txt" for op in body["ops"])
