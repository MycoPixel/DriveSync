"""Shared pytest fixtures for DriveSync tests."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from drivesync.db import Store

from mock_drive import MockDrive  # noqa: E402  (path set above)


class Env:
    """One isolated sync scenario: local dir + fake Drive + fresh store."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.local = tmp_path / "local"
        self.local.mkdir()
        self.drive = MockDrive(tmp_path / "cloud")
        self.store = Store(tmp_path / "state.db")
        self.cfg = {"ignore_patterns": [], "conflict_policy": "newer"}
        self.pair = self.store.add_pair(1, str(self.local),
                                        self.drive.root_id, "My Drive")

    def sync(self, force_full=False):
        from drivesync import engine
        stats, ok = engine.sync_pair(self.store, self.cfg, self.pair,
                                     self.drive, force_full=force_full)
        return stats, ok

    def set_direction(self, direction):
        self.store.update_pair(self.pair["id"], direction=direction)
        self.pair = self.store.pair(self.pair["id"])

    def set_policy(self, policy):
        self.store.update_pair(self.pair["id"], conflict_policy=policy)
        self.pair = self.store.pair(self.pair["id"])

    def set_deletes(self, on):
        self.store.update_pair(self.pair["id"], allow_deletes=int(on))
        self.pair = self.store.pair(self.pair["id"])

    def base(self):
        return {r["rel_path"]: dict(r)
                for r in self.store.base_entries(self.pair["id"])}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.store.close()
