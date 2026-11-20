"""Version retention (S5): the old local bytes survive a pull overwrite."""

import os

import pytest

from conftest import Env
from drivesync.config import app_dir


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _edit_remote(e, fid, text):
    """Mutate the fake Drive file's content and metadata (version bump)."""
    src = e.root / "editor-save.txt"
    src.write_text(text)
    e.drive.update_content(fid, str(src))


def _history_versions(e):
    hist = app_dir() / "history" / str(e.pair["id"])
    if not hist.exists():
        return {}
    return {rel: sorted(os.listdir(hist / rel))
            for rel in sorted(os.listdir(hist))
            if (hist / rel).is_dir()}


def test_pull_keeps_the_previous_version(home):
    e = Env(home.parent)
    (e.local / "a.txt").write_text("v1")
    e.sync()                                    # uploads v1
    fid = next(f["id"] for f in e.drive.files.values()
               if f.get("name") == "a.txt")
    _edit_remote(e, fid, "v2")                  # Drive-side edit
    stats, ok = e.sync()                        # pulls v2 over local v1
    assert ok and int(stats.pulled) == 1
    assert (e.local / "a.txt").read_text() == "v2"
    hist = _history_versions(e)
    assert list(hist) == ["a.txt"] and len(hist["a.txt"]) == 1
    assert (app_dir() / "history" / str(e.pair["id"]) / "a.txt"
            / hist["a.txt"][0]).read_text() == "v1"


def test_versions_are_capped_and_pruned(home):
    e = Env(home.parent)
    e.cfg["history_versions"] = 2
    (e.local / "a.txt").write_text("v0")
    e.sync()
    fid = next(f["id"] for f in e.drive.files.values()
               if f.get("name") == "a.txt")
    for text in ("v1", "v2", "v3"):
        _edit_remote(e, fid, text)
        e.sync()
    hist = _history_versions(e)["a.txt"]
    assert len(hist) == 2                       # capped at 2, oldest pruned
    kept = [app_dir() / "history" / str(e.pair["id"]) / "a.txt" / h
            for h in hist]
    assert all(p.read_text() in ("v1", "v2") for p in kept)  # v0 pruned


def test_history_versions_zero_disables(home):
    e = Env(home.parent)
    e.cfg["history_versions"] = 0
    (e.local / "a.txt").write_text("v1")
    e.sync()
    fid = next(f["id"] for f in e.drive.files.values()
               if f.get("name") == "a.txt")
    _edit_remote(e, fid, "v2")
    e.sync()
    assert (e.local / "a.txt").read_text() == "v2"
    assert _history_versions(e) == {}


def test_history_survives_two_pulls_in_one_second(home):
    e = Env(home.parent)
    (e.local / "a.txt").write_text("v1")
    e.sync()
    fid = next(f["id"] for f in e.drive.files.values()
               if f.get("name") == "a.txt")
    for text in ("v2", "v3"):
        _edit_remote(e, fid, text)
        e.sync()
    hist = _history_versions(e)["a.txt"]
    assert len(hist) == 2                       # same-second pulls disambiguated
    assert len({h for h in hist}) == 2


def test_config_rejects_bad_history_versions(monkeypatch, tmp_path):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    from drivesync.config import update_config
    with pytest.raises(ValueError):
        update_config(history_versions=-1)
    with pytest.raises(ValueError):
        update_config(history_versions="many")
    assert update_config(history_versions=25)["history_versions"] == 25


def test_untracked_new_file_download_has_nothing_to_keep(home):
    e = Env(home.parent)
    e.drive.put_remote_file("new.txt", "from drive")
    stats, ok = e.sync()
    assert ok and int(stats.pulled) == 1
    assert _history_versions(e) == {}           # nothing was overwritten
