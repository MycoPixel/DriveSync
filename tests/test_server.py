"""Dashboard API smoke tests via Flask's test client."""

import pytest

from drivesync.server import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    from drivesync.db import Store
    store = Store(tmp_path / "state.db")
    app = create_app(store, worker=None)
    yield app.test_client(), store
    store.close()


def test_health_and_status(client):
    c, _ = client
    assert c.get("/api/health").get_json()["ok"] is True
    st = c.get("/api/status").get_json()
    assert st["ok"] and "version" in st and st["worker"] is None


def test_index_served(client):
    c, _ = client
    html = c.get("/").get_data(as_text=True)
    assert "DriveSync" in html and "app.js" in html


def test_empty_lists(client):
    c, _ = client
    assert c.get("/api/pairs").get_json()["pairs"] == []
    assert c.get("/api/accounts").get_json()["accounts"] == []
    assert c.get("/api/events").get_json()["events"] == []


def test_add_pair_requires_existing_local_path(client):
    c, _ = client
    resp = c.post("/api/pairs", json={
        "account_id": 1, "local_path": "/no/such/dir", "remote_id": "root"})
    assert resp.status_code == 400 and resp.get_json()["ok"] is False


def test_add_pair_and_delete(client, tmp_path):
    c, store = client
    store.add_account("me@example.com", {})
    docs = tmp_path / "docs"
    docs.mkdir()
    good = c.post("/api/pairs", json={
        "account_id": 1, "local_path": str(docs),
        "remote_id": "abc123", "remote_path": "My Drive/docs",
        "direction": "download"})
    assert good.status_code == 200
    body = good.get_json()
    assert body["ok"] and body["pair"]["direction"] == "download"
    pid = body["pair"]["id"]
    assert c.patch(f"/api/pairs/{pid}", json={"enabled": 0}).get_json()["ok"]
    assert c.delete(f"/api/pairs/{pid}").get_json()["ok"]
    assert c.get("/api/pairs").get_json()["pairs"] == []


def test_settings_roundtrip_and_validation(client):
    c, _ = client
    resp = c.post("/api/settings", json={"sync_interval": 90})
    assert resp.get_json()["settings"]["sync_interval"] == 90
    bad = c.post("/api/settings", json={"sync_interval": 1})
    assert bad.status_code == 400


def test_sync_without_worker_returns_503(client):
    c, _ = client
    assert c.post("/api/sync").status_code == 503


def test_events_filter_param(client):
    c, store = client
    store.log(None, "worker-error", "", "boom", level="error")
    rows = c.get("/api/events?level=error").get_json()["events"]
    assert len(rows) == 1 and rows[0]["level"] == "error"


def test_drive_folders_endpoint(client, monkeypatch):
    """Regression: folder picker crashed with 500 (Drive.FOLDER_MIME does not
    exist — the constant is module-level in drivesync.drive)."""
    from drivesync.drive import FOLDER_MIME, SHORTCUT_MIME

    c, store = client
    store.add_account("me@example.com", {
        "access_token": "tok", "refresh_token": "r", "expires_at": 9e12})

    class FakeDrive:
        def __init__(self, provider):
            pass

        def list_children(self, parent):
            return [{"id": "f1", "name": "Photos", "mimeType": FOLDER_MIME},
                    {"id": "f2", "name": "notes.txt", "mimeType": "text/plain"},
                    {"id": "s1", "name": "Team Docs", "mimeType": SHORTCUT_MIME,
                     "shortcutDetails": {"targetId": "real1",
                                         "targetMimeType": FOLDER_MIME}}]

        def list_shared_folders(self):
            return [{"id": "sh1", "name": "From Other Account"}]

        def get(self, file_id, fields=None):
            return {"id": file_id, "name": "Sub", "parents": ["root"]}

    monkeypatch.setattr("drivesync.server.Drive", FakeDrive)

    body = c.get("/api/drive/1/folders?parent=abc").get_json()
    assert body["ok"] is True
    ids = [f["id"] for f in body["folders"]]
    assert ids == ["f1", "real1"]         # shortcut resolved, file skipped
    assert body["current"]["name"] == "Sub"  # non-root parent resolved

    root = c.get("/api/drive/1/folders").get_json()  # default parent=root
    assert root["ok"] and root["current"] is None
    assert root["folders"][0] == {"id": "shared", "name": "Shared with me"}
    assert [f["id"] for f in root["folders"][1:]] == ["f1", "real1"]


def test_drive_folders_shared_view(client, monkeypatch):
    """parent=shared lists folders shared with the account; the virtual view
    itself must not be offered as a pair target (server returns it as
    current, UI hides the use-row for it)."""
    from drivesync.drive import FOLDER_MIME

    c, store = client
    store.add_account("me@example.com", {
        "access_token": "tok", "refresh_token": "r", "expires_at": 9e12})

    class FakeDrive:
        def __init__(self, provider):
            pass

        def list_shared_folders(self):
            return [{"id": "sh1", "name": "From Other Account"},
                    {"id": "sh2", "name": "Family Pics"}]

        def get(self, file_id, fields=None):
            return {"id": file_id, "name": file_id, "parents": ["shared"]}

    monkeypatch.setattr("drivesync.server.Drive", FakeDrive)

    body = c.get("/api/drive/1/folders?parent=shared").get_json()
    assert body["ok"] is True
    assert body["current"] == {"id": "shared", "name": "Shared with me"}
    assert [f["id"] for f in body["folders"]] == ["sh1", "sh2"]


def test_pair_allow_deletes_roundtrip(client, tmp_path):
    c, store = client
    store.add_account("me@example.com", {})
    docs = tmp_path / "docs"
    docs.mkdir()
    resp = c.post("/api/pairs", json={
        "account_id": 1, "local_path": str(docs),
        "remote_id": "abc", "direction": "download"})
    body = resp.get_json()
    assert body["ok"] and body["pair"]["allow_deletes"] is True
    upd = c.patch(f"/api/pairs/{body['pair']['id']}",
                  json={"allow_deletes": 0}).get_json()
    assert upd["ok"] and upd["pair"]["allow_deletes"] is False
