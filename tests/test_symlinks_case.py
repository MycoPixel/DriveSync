"""Per-pair symlink policy and case-insensitive mode (S7)."""

import os

import pytest

from conftest import Env
from drivesync.db import Store
from drivesync.engine import canonicalize_case, scan_local


# ---- scan_local: symlink policy --------------------------------------------
def test_skip_policy_ignores_symlinks(tmp_path):
    (tmp_path / "real.txt").write_text("x")
    os.symlink(tmp_path / "real.txt", tmp_path / "link.txt")
    os.symlink("/etc", tmp_path / "etc")          # link to outside the tree
    items = scan_local(tmp_path, [], symlink_policy="skip")
    assert "real.txt" in items
    assert "link.txt" not in items and "etc" not in items


def test_skip_policy_is_the_default(tmp_path):
    (tmp_path / "real.txt").write_text("x")
    os.symlink(tmp_path / "real.txt", tmp_path / "link.txt")
    assert "link.txt" not in scan_local(tmp_path, [])


def test_follow_policy_records_links_and_descends(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "inner.txt").write_text("abc")
    os.symlink(tmp_path / "real", tmp_path / "alias")
    os.symlink(tmp_path / "real" / "inner.txt", tmp_path / "alias_file")
    items = scan_local(tmp_path, [], symlink_policy="follow")
    assert items["alias"]["kind"] == "dir"
    assert "alias/inner.txt" in items
    assert items["alias_file"]["kind"] == "file"
    assert items["alias_file"]["size"] == 3      # the target's size


def test_follow_policy_survives_link_loops(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    (a / "x.txt").write_text("1")
    os.symlink(b, a / "up")                       # a/up -> b
    os.symlink(a, b)                              # b -> a: a cycle
    items = scan_local(tmp_path, [], symlink_policy="follow")   # must not hang
    assert "a/x.txt" in items


def test_follow_policy_skips_broken_links(tmp_path):
    os.symlink(tmp_path / "gone", tmp_path / "dangling")
    assert scan_local(tmp_path, [], symlink_policy="follow") == {}


def test_e2e_outside_link_is_not_uploaded(tmp_path, monkeypatch):
    """Default pairs never sync symlink bytes as the target's content."""
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    secret = tmp_path / "outside.txt"
    secret.write_text("secret")
    e = Env(tmp_path)
    (e.local / "note.txt").write_text("mine")
    os.symlink(secret, e.local / "leak.txt")
    stats, ok = e.sync()
    assert ok and int(stats.pushed) == 1         # only note.txt
    assert e.drive.read_remote("note.txt") == b"mine"
    with pytest.raises(Exception):
        e.drive.read_remote("leak.txt")


# ---- canonicalize_case -------------------------------------------------------
def test_case_mode_uses_the_local_spelling():
    local = {"Report.txt": {"kind": "file", "size": 5, "mtime": 1.0,
                            "md5": None}}
    remote = {"report.txt": {"kind": "file", "id": "x", "md5": "m",
                             "version": "2", "mtime": 2.0, "size": 5,
                             "mime": "text/plain"}}
    base = {}
    l2, r2, b2, shadowed = canonicalize_case(local, remote, base)
    assert "Report.txt" in r2 and "report.txt" not in r2
    assert shadowed == []


def test_case_mode_anchors_on_base_when_local_lacks_the_file():
    local = {}
    base = {"Docs": {"kind": "dir", "rel_path": "Docs"}}
    remote = {"docs/note.txt": {"kind": "file", "id": "x", "md5": "m",
                                "version": "1", "mtime": 1.0, "size": 1,
                                "mime": "text/plain"}}
    _l, r2, _b, shadowed = canonicalize_case(local, remote, base)
    assert "Docs/note.txt" in r2                 # parent re-spelled from base
    assert shadowed == []


def test_case_mode_reports_remote_case_collisions():
    def fake(rel):
        return {"kind": "file", "id": rel, "md5": rel, "version": "1",
                "mtime": 1.0, "size": 1, "mime": "text/plain"}
    remote = {"A.txt": fake("A.txt"), "a.txt": fake("a.txt")}
    seen = []
    _l, r2, _b, _s = canonicalize_case({}, remote, {},
                                       notify=lambda rel, src: seen.append(rel))
    assert len(r2) == 1                          # one of them kept
    assert seen == ["a.txt"]                     # the other was reported


def test_case_mode_lists_shadowed_base_rows():
    base = {"photos": {"kind": "dir", "rel_path": "photos"},
            "Photos": {"kind": "dir", "rel_path": "Photos"}}
    _l, _r, b2, shadowed = canonicalize_case({}, {}, base)
    assert len(b2) == 1 and len(shadowed) == 1  # one duplicate row dropped
    assert shadowed[0].lower() == "photos"


# ---- e2e: a case-only remote rename does not churn --------------------------
def _drive_fid(e, name):
    return next(f["id"] for f in e.drive.files.values()
                if f.get("name") == name and not f.get("trashed"))


def test_e2e_case_only_rename_is_absorbed(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    e = Env(tmp_path)
    e.store.update_pair(e.pair["id"], case_insensitive=1)
    e.pair = e.store.pair(e.pair["id"])
    (e.local / "Report.txt").write_text("hello")
    stats, ok = e.sync()
    assert ok and int(stats.pushed) == 1
    e.drive.rename(_drive_fid(e, "Report.txt"), "report.txt")
    # a case-only rename keeps the md5, so the pair stays aligned with zero
    # work — no delete+create churn, no re-download, on-disk spelling kept
    stats, ok = e.sync()
    assert ok and int(stats.pushed) == 0 and int(stats.pulled) == 0
    assert (e.local / "Report.txt").exists()
    assert (e.local / "report.txt").exists() is False
    stats, ok = e.sync()                         # and it stays quiet
    assert ok and int(stats.pushed) == 0 and int(stats.pulled) == 0


def test_e2e_case_collision_is_logged_not_synced(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    e = Env(tmp_path)
    e.store.update_pair(e.pair["id"], case_insensitive=1)
    e.pair = e.store.pair(e.pair["id"])
    e.drive.put_remote_file("A.txt", "one")
    e.drive.put_remote_file("a.txt", "two")
    stats, ok = e.sync()
    assert ok                                    # logged, not a pass failure
    actions = [ev["action"] for ev in e.store.recent_events(limit=50)]
    assert "case-collision" in actions


def test_pair_fields_persist(tmp_path):
    store = Store(tmp_path / "state.db")
    pair = store.add_pair(1, str(tmp_path), "rid", symlink_policy="follow",
                          case_insensitive=1)
    assert pair["symlink_policy"] == "follow"
    assert pair["case_insensitive"] == 1
    updated = store.update_pair(pair["id"], symlink_policy="skip",
                                case_insensitive=0)
    assert updated["symlink_policy"] == "skip"
    assert updated["case_insensitive"] == 0
    with pytest.raises(ValueError):
        store.update_pair(pair["id"], symlink_policy="delete")
    store.close()
