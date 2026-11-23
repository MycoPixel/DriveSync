"""Portable settings export/import: config + pairs, never credentials."""

import json
import os

import pytest

from drivesync.config import load_config, save_config
from drivesync.db import Store
from drivesync.portable import FORMAT, export_settings, import_settings


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


def test_export_carries_pairs_but_never_credentials(tmp_path, store,
                                                    monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path))
    save_config({**load_config(), "client_secret": "super-secret",
                 "client_id": "client-123", "sync_interval": 45,
                 "window_width": 1234})
    account = store.add_account("me@example.com", {
        "access_token": "tok", "refresh_token": "refresh"})
    store.add_pair(account["id"], str(tmp_path / "Docs"), "remote-1",
                   remote_path="My Drive/Docs", direction="upload")

    out = tmp_path / "export.json"
    export_settings(store, out)
    data = json.loads(out.read_text())

    assert data["format"] == FORMAT and data["version"] == 1
    assert data["config"]["sync_interval"] == 45
    assert data["config"]["client_id"] == "client-123"
    assert "client_secret" not in data["config"]
    assert "window_width" not in data["config"]
    assert "access_token" not in out.read_text()
    assert data["pairs"] == [{
        "account_email": "me@example.com",
        "local_path": str(tmp_path / "Docs"),
        "remote_id": "remote-1",
        "remote_path": "My Drive/Docs",
        "direction": "upload",
        "conflict_policy": "newer",
        "enabled": True,
        "allow_deletes": True,
        "include_patterns": [],
        "symlink_policy": "skip",
        "case_insensitive": False,
        "bandwidth_limit_kb": None,
        "peak_from": None,
        "peak_to": None,
        "bandwidth_limit_kb_peak": None,
    }]
    if os.name == "posix":
        assert (out.stat().st_mode & 0o777) == 0o600


def test_import_merges_config_and_matches_accounts_by_email(tmp_path, store,
                                                            monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path))
    save_config({**load_config(), "client_secret": "keep-me"})
    account = store.add_account("me@example.com", {"refresh_token": "r"})
    payload = {
        "format": FORMAT, "version": 1,
        "config": {"sync_interval": 99, "client_secret": "stolen",
                   "window_width": 111},
        "pairs": [
            {"account_email": "me@example.com",
             "local_path": str(tmp_path / "Docs"), "remote_id": "r1"},
            {"account_email": "missing@example.com",
             "local_path": str(tmp_path / "Other"), "remote_id": "r2"},
        ],
    }
    src = tmp_path / "in.json"
    src.write_text(json.dumps(payload))

    report = import_settings(store, src)
    assert report["config_keys"] == ["sync_interval"]
    assert len(report["added"]) == 1
    assert "missing@example.com" in report["skipped"][0]
    cfg = load_config()
    assert cfg["sync_interval"] == 99
    assert cfg["client_secret"] == "keep-me"          # never overwritten
    assert cfg["window_width"] == 900                  # machine-specific
    pairs = store.pairs()
    assert len(pairs) == 1
    assert pairs[0]["account_id"] == account["id"]
    assert pairs[0]["remote_id"] == "r1"


def test_import_dry_run_writes_nothing(tmp_path, store, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path))
    store.add_account("me@example.com", {"refresh_token": "r"})
    src = tmp_path / "in.json"
    src.write_text(json.dumps({
        "format": FORMAT, "version": 1,
        "config": {"sync_interval": 77},
        "pairs": [{"account_email": "me@example.com",
                   "local_path": str(tmp_path / "Docs"),
                   "remote_id": "r1"}],
    }))
    report = import_settings(store, src, dry_run=True)
    assert report["added"] and report["dry_run"] is True
    assert load_config()["sync_interval"] == 30
    assert store.pairs() == []


def test_import_rejects_foreign_or_oversized_files(tmp_path, store):
    bad = tmp_path / "bad.json"
    bad.write_text('{"hello": "world"}')
    with pytest.raises(ValueError, match="format marker"):
        import_settings(store, bad)

    huge = tmp_path / "huge.json"
    huge.write_text("x" * ((1 << 20) + 1))
    with pytest.raises(ValueError, match="too large"):
        import_settings(store, huge)


def test_import_skips_overlapping_pair_without_aborting(tmp_path, store,
                                                        monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path))
    account = store.add_account("me@example.com", {"refresh_token": "r"})
    local = str(tmp_path / "Docs")
    store.add_pair(account["id"], local, "r1")
    src = tmp_path / "in.json"
    src.write_text(json.dumps({
        "format": FORMAT, "version": 1, "config": {},
        "pairs": [
            {"account_email": "me@example.com", "local_path": local,
             "remote_id": "r1"},                       # duplicate
            {"account_email": "me@example.com", "local_path": local,
             "remote_id": "r2"},                       # overlaps
            {"account_email": "me@example.com",
             "local_path": str(tmp_path / "Other"), "remote_id": "r3"},
        ],
    }))
    report = import_settings(store, src)
    assert len(report["added"]) == 1
    assert len(report["skipped"]) == 2
    assert {p["remote_id"] for p in store.pairs()} == {"r1", "r3"}
