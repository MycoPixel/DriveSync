"""Security helpers: traversal-safe joins and the installation audit."""

import os

import pytest

from drivesync.config import config_path, db_path, load_config, save_config
from drivesync.db import Store
from drivesync.security import apply_fixes, audit, safe_join


def test_safe_join_allows_normal_nested_paths(tmp_path):
    target = safe_join(tmp_path, "a/b/c.txt")
    assert target == str(tmp_path / "a" / "b" / "c.txt")
    assert safe_join(tmp_path, "") == str(tmp_path)
    assert safe_join(tmp_path, "a/./b") == str(tmp_path / "a" / "b")


@pytest.mark.parametrize("rel", [
    "../outside.txt",
    "a/../../outside.txt",
    "/etc/passwd",
    "a//../../etc/passwd",
    "bad\x00name",
])
def test_safe_join_rejects_traversal(tmp_path, rel):
    with pytest.raises(ValueError):
        safe_join(tmp_path, rel)


def test_safe_join_rejects_symlink_escape_unless_explicitly_allowed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        safe_join(root, "link/secret.txt")
    # symlink_policy=follow explicitly opts into this
    assert safe_join(root, "link/secret.txt", allow_symlink_escape=True) == \
        str(root / "link" / "secret.txt")


def test_audit_reports_and_fixes_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path))
    save_config({**load_config(), "client_secret": "s"})
    store = Store(db_path())
    try:
        report = audit(store)
        assert report["ok"], [c for c in report["checks"] if not c["ok"]]

        os.chmod(config_path(), 0o644)
        os.chmod(db_path(), 0o644)
        report = audit(store)
        assert not report["ok"]
        failed = {c["name"] for c in report["checks"] if not c["ok"]}
        assert {"config private", "state database private"} <= failed
        assert apply_fixes(report) >= 2
        assert (config_path().stat().st_mode & 0o777) == 0o600
        assert (db_path().stat().st_mode & 0o777) == 0o600
    finally:
        store.close()


def test_audit_never_prints_the_client_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path))
    save_config({**load_config(), "client_secret": "do-not-leak-me"})
    store = Store(db_path())
    try:
        text = repr(audit(store))
    finally:
        store.close()
    assert "do-not-leak-me" not in text
