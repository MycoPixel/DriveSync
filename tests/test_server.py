"""Dashboard API smoke tests via Flask's test client."""

import pytest

from drivesync.server import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    from drivesync.db import Store
    store = Store(tmp_path / "state.db")
    app = create_app(store, worker=None)
    c = app.test_client()
    c.environ_base["HTTP_X_DRIVESYNC"] = "1"
    yield c, store
    store.close()


def fake_folder_drive(monkeypatch, mime_type=None):
    """Make pair validation see a fake Drive folder (no HTTP)."""
    from drivesync.drive import FOLDER_MIME

    class FakeDrive:
        def __init__(self, provider, limiter=None):
            pass

        def get(self, file_id, fields=None):
            return {"id": file_id, "name": file_id,
                    "mimeType": mime_type or FOLDER_MIME}

    monkeypatch.setattr("drivesync.server.Drive", FakeDrive)


def test_health_and_status(client):
    c, _ = client
    assert c.get("/api/health").get_json()["ok"] is True
    st = c.get("/api/status").get_json()
    assert st["ok"] and "version" in st and st["worker"] is None


def test_index_served(client):
    c, _ = client
    html = c.get("/").get_data(as_text=True)
    assert "DriveSync" in html and "app.js" in html


def test_settings_form_covers_worker_settings(client):
    """The form exposes the knobs the README promises (issue: UI gaps)."""
    c, _ = client
    html = c.get("/").get_data(as_text=True)
    for field in ("setBandwidth", "setActiveFrom", "setActiveTo",
                  "setWatch", "setIncremental"):
        assert field in html


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


def test_add_pair_and_delete(client, tmp_path, monkeypatch):
    fake_folder_drive(monkeypatch)
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


def test_add_pair_rejects_a_non_folder_remote_id(client, tmp_path, monkeypatch):
    fake_folder_drive(monkeypatch, mime_type="application/pdf")
    c, store = client
    store.add_account("me@example.com", {})
    docs = tmp_path / "docs"
    docs.mkdir()
    resp = c.post("/api/pairs", json={
        "account_id": 1, "local_path": str(docs), "remote_id": "f1"})
    assert resp.status_code == 400
    assert "not a Drive folder" in resp.get_json()["error"]
    assert store.pairs() == []


def test_settings_roundtrip_and_validation(client):
    c, _ = client
    resp = c.post("/api/settings", json={"sync_interval": 90})
    assert resp.get_json()["settings"]["sync_interval"] == 90
    bad = c.post("/api/settings", json={"sync_interval": 1})
    assert bad.status_code == 400


def test_sync_without_worker_returns_503(client):
    c, _ = client
    assert c.post("/api/sync").status_code == 503


def test_api_rejects_requests_without_csrf_header(client):
    """A cross-site page cannot send a custom header without a CORS
    preflight, which this server never grants."""
    c, _ = client
    assert c.get("/api/health", headers={"X-DriveSync": "0"}).status_code == 403
    assert c.get("/api/health", headers={"X-DriveSync": "1"}).status_code == 200
    # the static UI stays readable (it contains no secrets)
    assert c.get("/", headers={"X-DriveSync": "0"}).status_code == 200


def test_api_rejects_rebinding_host_and_cross_origin(client):
    c, _ = client
    assert c.get("/api/health",
                 headers={"Host": "evil.example"}).status_code == 403
    assert c.get("/api/health",
                 headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.get("/api/health",
                 headers={"Origin": "null"}).status_code == 403
    assert c.get("/api/health",
                 headers={"Origin": "http://127.0.0.1:5678"}).status_code == 200


def test_settings_never_return_the_client_secret(client):
    c, _ = client
    from drivesync import config
    config.update_config(client_id="cid", client_secret="topsecret")

    body = c.get("/api/settings").get_json()["settings"]
    assert body["client_secret"] == "***"
    assert body["client_secret_set"] is True

    posted = c.post("/api/settings",
                    json={"sync_interval": 60}).get_json()["settings"]
    assert posted["client_secret"] == "***"
    assert "topsecret" not in c.get("/api/settings").get_data(as_text=True)

    # posting the redacted object back must not overwrite the real secret
    c.post("/api/settings", json={"client_secret": "***"})
    assert config.load_config()["client_secret"] == "topsecret"


def test_drive_for_persists_refreshed_tokens(client, monkeypatch):
    """Dashboard requests must not refresh from the same stale row forever."""
    import json

    import drivesync.auth as auth

    _, store = client
    store.add_account("me@example.com", {
        "access_token": "old", "refresh_token": "r", "client_id": "cid",
        "client_secret": "csec", "expires_at": 0})
    monkeypatch.setattr(auth, "refresh_tokens",
                        lambda *a: {"access_token": "fresh",
                                    "expires_in": 3600})

    class FakeDrive:
        def __init__(self, provider):
            provider()                    # what Drive._headers() does

        def list_children(self, parent):
            return []

    monkeypatch.setattr("drivesync.server.Drive", FakeDrive)
    c = create_app(store, worker=None).test_client()
    c.environ_base["HTTP_X_DRIVESYNC"] = "1"
    assert c.get("/api/drive/1/folders").get_json()["ok"] is True

    stored = json.loads(store.account(1)["token_json"])
    assert stored["access_token"] == "fresh"
    assert stored["expires_at"] > 0


def test_settings_post_hot_reloads_worker(client):
    """Saved settings reach the running worker without a restart."""
    _, store = client
    seen = {}

    class FakeWorker:
        def apply_config(self, cfg):
            seen.update(cfg)

    c = create_app(store, worker=FakeWorker()).test_client()
    c.environ_base["HTTP_X_DRIVESYNC"] = "1"
    resp = c.post("/api/settings", json={"sync_interval": 77})
    assert resp.get_json()["ok"]
    assert seen["sync_interval"] == 77


def test_accounts_add_keeps_saved_credentials(client, monkeypatch):
    """Re-authorizing without posting credentials must not wipe them."""
    c, _ = client
    from drivesync import config
    import drivesync.server as srv

    config.update_config(client_id="cid", client_secret="csec")
    monkeypatch.setattr(srv, "interactive_auth",
                        lambda cfg: {"access_token": "tok",
                                     "refresh_token": "r"})
    monkeypatch.setattr(srv, "whoami",
                        lambda tok: {"emailAddress": "me@example.com"})

    resp = c.post("/api/accounts", json={})
    assert resp.get_json()["ok"]
    cfg = config.load_config()
    assert cfg["client_id"] == "cid" and cfg["client_secret"] == "csec"

    c.post("/api/accounts", json={"client_id": "new",
                                  "client_secret": "newsec"})
    cfg = config.load_config()
    assert cfg["client_id"] == "new" and cfg["client_secret"] == "newsec"


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


def test_pair_allow_deletes_roundtrip(client, tmp_path, monkeypatch):
    fake_folder_drive(monkeypatch)
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
