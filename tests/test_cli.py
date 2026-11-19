"""CLI behaviour tests: getting-started banner, validation, friendly errors.

Error paths use sys.exit("message") — pytest.raises catches the SystemExit
before the interpreter prints it, so the message is asserted via
exc.value.code (same convention as the gui-hint test).
"""

import pytest

from drivesync.cli import _open_browser, build_parser, main


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated app data dir (config.json + state.db live here)."""
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _pair(home):
    """One account + pair written straight into the test store."""
    from drivesync.db import Store
    s = Store(home / "state.db")
    s.add_account("a@example.com", {})
    p = s.add_pair(1, str(home.parent / "docs"), "abc123", "My Drive/docs")
    s.close()
    return p


# ---- bare invocation ----------------------------------------------------
def test_bare_invocation_prints_next_steps(home, capsys):
    main([])                       # banner, not an argparse error
    out = capsys.readouterr().out
    assert "Next steps" in out and "drivesync gui" in out


# ---- config -------------------------------------------------------------
def test_config_rejects_unknown_key(home):
    with pytest.raises(SystemExit) as exc:
        main(["config", "set", "syn_interval=60"])      # deliberate typo
    msg = str(exc.value.code)
    assert "unknown setting" in msg and "sync_interval" in msg


def test_config_rejects_out_of_range_value(home):
    with pytest.raises(SystemExit) as exc:
        main(["config", "set", "sync_interval=1"])
    assert "between 5 and 86400" in str(exc.value.code)


def test_config_set_persists(home, capsys):
    main(["config", "set", "sync_interval=120"])
    assert "saved: sync_interval = 120" in capsys.readouterr().out
    from drivesync.config import load_config
    assert load_config()["sync_interval"] == 120


# ---- pairs --------------------------------------------------------------
def test_pairs_add_requires_connected_account(home):
    docs = home.parent / "docs"
    docs.mkdir()
    with pytest.raises(SystemExit) as exc:
        main(["pairs", "add", str(docs), "--remote-id", "abc123"])
    assert "no account #1" in str(exc.value.code)


def test_pairs_set_rejects_unknown_field(home):
    p = _pair(home)
    with pytest.raises(SystemExit) as exc:
        main(["pairs", "set", "--pair-id", str(p["id"]), "conflictpolicy=both"])
    msg = str(exc.value.code)
    assert "unknown pair field" in msg and "conflict_policy" in msg


def test_pairs_set_rejects_missing_pair(home):
    with pytest.raises(SystemExit) as exc:
        main(["pairs", "set", "--pair-id", "99", "enabled=true"])
    assert "no sync pair #99" in str(exc.value.code)


def test_pairs_set_validates_direction(home):
    p = _pair(home)
    with pytest.raises(SystemExit) as exc:
        main(["pairs", "set", "--pair-id", str(p["id"]), "direction=sideways"])
    assert "direction must be one of" in str(exc.value.code)


def test_pairs_set_accepts_lone_key_value(home):
    """'pairs set enabled=false' (no --pair-id preceding positional quirk)."""
    p = _pair(home)
    main(["pairs", "set", "--pair-id", str(p["id"]), "enabled=false"])
    from drivesync.db import Store
    assert Store(home / "state.db").pair(p["id"])["enabled"] == 0


# ---- sync / serve -------------------------------------------------------
def test_sync_without_pairs_prints_guidance(home, capsys):
    main(["sync"])
    out = capsys.readouterr().out
    assert "No enabled sync pairs" in out and "pairs add" in out


def test_sync_unknown_pair(home):
    with pytest.raises(SystemExit) as exc:
        main(["sync", "--pair", "42"])
    assert "no sync pair #42" in str(exc.value.code)


def test_sync_reports_aggregate_totals(home, monkeypatch, capsys):
    from drivesync import engine
    import drivesync.worker as worker_mod

    _pair(home)

    class FakeWorker:
        def __init__(self, store, cfg, config_source=None):
            self.args = (store, cfg, config_source)

        def run_pass(self, pair_id=None, force_full=False):
            stats = engine.SyncStats()
            stats.pushed += 2
            stats.pulled += 1
            return stats, True

    monkeypatch.setattr(worker_mod, "Worker", FakeWorker)
    with pytest.raises(SystemExit) as exc:
        main(["sync"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "pushed 2" in out and "pulled 1" in out and "errors 0" in out


def test_sync_exits_2_when_pass_failed(home, monkeypatch, capsys):
    from drivesync import engine
    import drivesync.worker as worker_mod

    _pair(home)

    class FakeWorker:
        def __init__(self, store, cfg, config_source=None):
            pass

        def run_pass(self, pair_id=None, force_full=False):
            stats = engine.SyncStats()
            stats.errors += 1
            return stats, False

    monkeypatch.setattr(worker_mod, "Worker", FakeWorker)
    with pytest.raises(SystemExit) as exc:
        main(["sync"])
    assert exc.value.code == 2
    assert "errors 1" in capsys.readouterr().out


def test_serve_pre_probe_prints_hint(home, monkeypatch, capsys):
    """Werkzeug exits the process on EADDRINUSE, so the CLI must probe."""
    import drivesync.cli as cli

    monkeypatch.setattr(cli, "_port_busy", lambda url: True)
    with pytest.raises(SystemExit) as exc:
        main(["serve"])
    assert exc.value.code == 1
    assert "systemctl --user status drivesync" in capsys.readouterr().err


def test_serve_port_busy_prints_hint(home, monkeypatch, capsys):
    import drivesync.server as srv

    def busy(*a, **k):
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(srv, "run_server", busy)
    with pytest.raises(SystemExit) as exc:
        main(["serve"])
    assert exc.value.code == 1
    assert "systemctl --user status drivesync" in capsys.readouterr().err


def test_open_browser_best_effort(monkeypatch):
    calls = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda u: calls.append(u))
    _open_browser("http://127.0.0.1:5678")
    assert calls == ["http://127.0.0.1:5678"]


def test_open_browser_swallows_errors(monkeypatch):
    import webbrowser

    def no_display(u):
        raise RuntimeError("no browser")

    monkeypatch.setattr(webbrowser, "open", no_display)
    _open_browser("http://127.0.0.1:5678")          # must not raise


# ---- status / doctor ----------------------------------------------------
def test_status_hints_when_empty(home, capsys):
    main(["status"])
    out = capsys.readouterr().out
    assert "drivesync auth" in out and "pairs add" in out


def test_doctor_clean_install_passes(home, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["doctor"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "doctor" in out and "no account authorized yet" in out


def test_worker_init_prunes_activity_log(tmp_path):
    """Constructing a Worker bounds the activity log (serve/sync both do)."""
    from datetime import datetime, timedelta, timezone

    from drivesync.db import Store
    from drivesync.worker import Worker

    s = Store(tmp_path / "state.db")
    old = (datetime.now(timezone.utc) - timedelta(days=40)) \
        .isoformat(timespec="seconds")
    for _ in range(3):
        s._exec("INSERT INTO events(ts, level, action) VALUES (?,?,?)",
                (old, "info", "old"))
    s.log(1, "fresh", "", "")
    Worker(s, {"sync_interval": 30})                # not started; init prunes
    assert s.count_events() == 1
    s.close()


# ---- 0.4.0 additions ----------------------------------------------------
def test_sync_full_flag_parses():
    args = build_parser().parse_args(["sync", "--full", "--pair", "3"])
    assert args.full and args.pair == 3


def test_config_set_bandwidth(home, capsys):
    main(["config", "set", "bandwidth_limit_kb=512"])
    assert "saved: bandwidth_limit_kb = 512" in capsys.readouterr().out
    from drivesync.config import load_config
    assert load_config()["bandwidth_limit_kb"] == 512


def test_config_rejects_negative_bandwidth(home):
    with pytest.raises(SystemExit) as exc:
        main(["config", "set", "bandwidth_limit_kb=-5"])
    assert "unlimited" in str(exc.value.code)


def test_config_rejects_bad_schedule(home):
    with pytest.raises(SystemExit) as exc:
        main(["config", "set", "active_from=8am"])
    assert "HH:MM" in str(exc.value.code)
