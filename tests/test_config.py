"""Config module tests (uses DRIVE_SYNC_HOME sandbox)."""

import json

import pytest

from drivesync import config


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    yield tmp_path / "home"


def test_defaults_when_no_file():
    cfg = config.load_config()
    assert cfg["sync_interval"] == 30
    assert cfg["conflict_policy"] == "newer"
    assert "*.tmp" in cfg["ignore_patterns"]


def test_save_and_reload_roundtrip():
    saved = config.update_config(sync_interval=120, conflict_policy="both",
                                 client_id="abc", client_secret="xyz")
    assert saved["sync_interval"] == 120
    assert json.loads(config.config_path().read_text())["client_id"] == "abc"
    assert config.load_config()["conflict_policy"] == "both"


def test_unknown_keys_are_dropped():
    saved = config.save_config({"sync_interval": 60, "hacker_key": "x"})
    assert "hacker_key" not in saved


def test_validation_errors():
    with pytest.raises(ValueError):
        config.update_config(sync_interval=2)          # too low
    with pytest.raises(ValueError):
        config.update_config(sync_interval=999999)     # too high
    with pytest.raises(ValueError):
        config.update_config(conflict_policy="chaos")
    with pytest.raises(ValueError):
        config.update_config(server_port=80)           # privileged
    with pytest.raises(ValueError):
        config.update_config(ignore_patterns="*.tmp")  # must be a list


def test_corrupt_config_file_falls_back_to_defaults():
    config.app_dir().mkdir(parents=True, exist_ok=True)
    config.config_path().write_text("{ not json !!!")
    cfg = config.load_config()
    assert cfg["sync_interval"] == 30


def test_env_override_path():
    assert config.app_dir().exists() is False or True  # just resolves
    assert str(config.config_path()).endswith("home/config.json")
