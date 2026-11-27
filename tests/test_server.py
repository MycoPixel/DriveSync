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
    c.environ_base["HTTP_X_DRIVESYNC"] = app.config["API_TOKEN"]
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


def test_pair_form_defaults_new_pairs_to_paused_and_explains_review(client):
    c, _ = client
    html = c.get("/").get_data(as_text=True)
    assert 'id="pairStartPaused" type="checkbox" checked' in html
    assert "first-sync-review-hint" in html


def test_settings_form_covers_worker_settings(client):
    """The form exposes the knobs the README promises (issue: UI gaps)."""
    c, _ = client
    html = c.get("/").get_data(as_text=True)
    for field in ("setBandwidth", "setActiveFrom", "setActiveTo",
                  "setWatch", "setIncremental"):
        assert field in html


def test_transfer_queue_endpoint_is_empty_without_worker(client):
    c, _ = client
    response = c.get("/api/transfers")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "transfers": []}


def test_transfer_queue_endpoint_returns_newest_first(client, tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    from drivesync.db import Store
    store = Store(tmp_path / "other-state.db")

    class FakeWorker:
        status = {"transfers": [
            {"id": "old", "status": "completed"},
            {"id": "new", "status": "uploading"},
        ]}

    from drivesync.server import create_app
    app = create_app(store, worker=FakeWorker())
    c = app.test_client()
    c.environ_base["HTTP_X_DRIVESYNC"] = app.config["API_TOKEN"]
    try:
        rows = c.get("/api/transfers").get_json()["transfers"]
        assert [row["id"] for row in rows] == ["new", "old"]
    finally:
        store.close()


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
        "direction": "download", "enabled": 0})
    assert good.status_code == 200
    body = good.get_json()
    assert body["ok"] and body["pair"]["direction"] == "download"
    assert body["pair"]["enabled"] is False
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
    preflight, which this server never grants; the value is a per-session
    random token, not a constant."""
    c, _ = client
    tok = c.application.config["API_TOKEN"]
    assert c.get("/api/health", headers={"X-DriveSync": "0"}).status_code == 403
    assert c.get("/api/health", headers={"X-DriveSync": "1"}).status_code == 403
    assert c.get("/api/health", headers={"X-DriveSync": tok}).status_code == 200
    # the static UI stays readable (it contains no secrets)
    assert c.get("/", headers={"X-DriveSync": "0"}).status_code == 200
    assert tok in c.get("/").get_data(as_text=True)


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
    app = create_app(store, worker=None)
    c = app.test_client()
    c.environ_base["HTTP_X_DRIVESYNC"] = app.config["API_TOKEN"]
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

    app = create_app(store, worker=FakeWorker())
    c = app.test_client()
    c.environ_base["HTTP_X_DRIVESYNC"] = app.config["API_TOKEN"]
    resp = c.post("/api/settings", json={"sync_interval": 77})
    assert resp.get_json()["ok"]
    assert seen["sync_interval"] == 77


def test_accounts_add_keeps_saved_credentials(client, monkeypatch):
    """Re-authorizing without posting credentials must not wipe them."""
    c, _ = client
    from drivesync import config
    import drivesync.server as srv
    import time

    config.update_config(client_id="cid", client_secret="csec")
    monkeypatch.setattr(srv, "interactive_auth",
                        lambda cfg, cancel_event=None: {
                            "access_token": "tok", "refresh_token": "r"})
    monkeypatch.setattr(srv, "whoami",
                        lambda tok: {"emailAddress": "me@example.com"})

    def start_and_wait(body):
        resp = c.post("/api/accounts", json=body)
        assert resp.status_code == 202
        job = resp.get_json()["job"]
        for _ in range(100):
            status = c.get(f"/api/accounts/auth/{job}").get_json()
            if status["status"] in ("done", "error", "cancelled"):
                return status
            time.sleep(0.01)
        raise AssertionError("sign-in job did not finish")

    assert start_and_wait({})["status"] == "done"
    cfg = config.load_config()
    assert cfg["client_id"] == "cid" and cfg["client_secret"] == "csec"

    assert start_and_wait({"client_id": "new",
                           "client_secret": "newsec"})["status"] == "done"
    cfg = config.load_config()
    assert cfg["client_id"] == "new" and cfg["client_secret"] == "newsec"


def test_account_removal_can_revoke_the_google_grant(client, monkeypatch):
    c, store = client
    from drivesync import server as srv
    store.add_account("me@example.com", {"access_token": "tok",
                                         "refresh_token": "refresh"})
    seen = {}
    monkeypatch.setattr(srv, "revoke_token",
                        lambda tok: (seen.update(tok) or (True, "revoked")))
    resp = c.delete("/api/accounts/1", json={"revoke": True})
    body = resp.get_json()
    assert body["ok"] and body["revoked"]["ok"] is True
    assert seen["refresh_token"] == "refresh"
    assert store.account(1) is None


def test_account_removal_without_revoke_leaves_the_grant(client, monkeypatch):
    c, store = client
    from drivesync import server as srv
    store.add_account("me@example.com", {"refresh_token": "refresh"})
    monkeypatch.setattr(srv, "revoke_token",
                        lambda tok: (_ for _ in ()).throw(
                            AssertionError("must not revoke")))
    assert c.delete("/api/accounts/1").get_json()["revoked"] is None


def test_oauth_test_endpoint_reports_bad_client(client, monkeypatch):
    c, _ = client
    from drivesync import server as srv
    monkeypatch.setattr(srv, "validate_client",
                        lambda cid, csec: (False, "bad client"))
    resp = c.post("/api/oauth/test", json={"client_id": "x",
                                           "client_secret": "y"})
    assert resp.status_code == 400 and "bad client" in resp.get_json()["error"]


def test_read_only_scope_refuses_writable_pairs(client, tmp_path, monkeypatch):
    c, store = client
    from drivesync import config
    config.update_config(drive_scope="readonly")
    fake_folder_drive(monkeypatch)
    store.add_account("me@example.com", {})
    docs = tmp_path / "docs"
    docs.mkdir()
    resp = c.post("/api/pairs", json={
        "account_id": 1, "local_path": str(docs), "remote_id": "abc",
        "direction": "bidirectional"})
    assert resp.status_code == 400
    assert "read-only" in resp.get_json()["error"]


def test_events_filter_param(client):
    c, store = client
    store.log(None, "worker-error", "", "boom", level="error")
    rows = c.get("/api/events?level=error").get_json()["events"]
    assert len(rows) == 1 and rows[0]["level"] == "error"


def test_events_limit_degrades_instead_of_500(client):
    c, store = client
    store.log(None, "test", "", "one")
    assert c.get("/api/events?limit=nope").status_code == 200
    assert c.get("/api/events?limit=0").status_code == 200


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
    assert root["folders"][1] == {"id": "drives", "name": "Shared drives"}
    assert [f["id"] for f in root["folders"][2:]] == ["f1", "real1"]


def test_drive_folders_shared_drives_view(client, monkeypatch):
    """parent=drives lists the account's Shared Drives as folder roots."""
    from drivesync.drive import FOLDER_MIME

    c, store = client
    store.add_account("me@example.com", {
        "access_token": "tok", "refresh_token": "r", "expires_at": 9e12})

    class FakeDrive:
        def __init__(self, provider):
            pass

        def list_drives(self):
            return [{"id": "d1", "name": "Engineering"},
                    {"id": "d2", "name": "Marketing"}]

        def get(self, file_id, fields=None):
            return {"id": file_id, "name": file_id, "parents": [],
                    "driveId": file_id}

    monkeypatch.setattr("drivesync.server.Drive", FakeDrive)

    body = c.get("/api/drive/1/folders?parent=drives").get_json()
    assert body["ok"] is True
    assert body["current"] == {"id": "drives", "name": "Shared drives"}
    assert [f["id"] for f in body["folders"]] == ["d1", "d2"]
    assert all(f["mimeType"] == FOLDER_MIME for f in body["folders"])


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


def test_pair_json_includes_last_sync(client, tmp_path, monkeypatch):
    """pair rows carry the worker's per-pair status for the UI column."""
    fake_folder_drive(monkeypatch)
    c, store = client
    store.add_account("me@example.com", {})
    docs = tmp_path / "docs"
    docs.mkdir()
    app = create_app(store, worker=None)
    c2 = app.test_client()
    c2.environ_base["HTTP_X_DRIVESYNC"] = app.config["API_TOKEN"]
    pair = c2.post("/api/pairs", json={
        "account_id": 1, "local_path": str(docs),
        "remote_id": "abc", "direction": "download"}).get_json()["pair"]
    assert pair["last_sync"] is None          # no worker -> nothing recorded

    class StubWorker:
        status = {"pairs": {pair["id"]: {"ts": 1234.0, "ok": True,
                                         "stats": {"pushed": 1},
                                         "error": None}}}

    app2 = create_app(store, worker=StubWorker())
    c3 = app2.test_client()
    c3.environ_base["HTTP_X_DRIVESYNC"] = app2.config["API_TOKEN"]
    row = c3.get("/api/pairs").get_json()["pairs"][0]
    assert row["last_sync"]["ok"] is True
    assert row["last_sync"]["stats"]["pushed"] == 1


def test_conflicts_endpoints(client, tmp_path, monkeypatch):
    fake_folder_drive(monkeypatch)
    c, store = client
    store.add_account("me@example.com", {})
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.conflict-20240102-030405.txt").write_text("copy")
    pair_id = store.add_pair(1, str(docs), "abc", direction="download")["id"]
    # conflict copies are listed from the base snapshot (every copy the
    # engine creates has a row there) — plant the row a pass would leave
    store.base_put(pair_id, "a.conflict-20240102-030405.txt", "file",
                   remote_id="rx", local_size=4)
    body = c.get(f"/api/pairs/{pair_id}/conflicts").get_json()
    assert body["ok"] and len(body["conflicts"]) == 1
    item = body["conflicts"][0]
    assert item["path"] == "a.conflict-20240102-030405.txt"
    assert item["original"] == "a.txt" and item["local_exists"] is True

    # unknown path and bogus choice are clean 400s, never a traceback
    bad_path = c.post(f"/api/pairs/{pair_id}/conflicts/resolve",
                      json={"path": "a.txt", "choice": "keep_copy"})
    assert bad_path.status_code == 400
    bad_choice = c.post(f"/api/pairs/{pair_id}/conflicts/resolve",
                        json={"path": "a.conflict-20240102-030405.txt",
                              "choice": "bogus"})
    assert bad_choice.status_code == 400
    missing = c.get("/api/pairs/999/conflicts")
    assert missing.status_code == 404


def test_dashboard_bind_host_is_loopback_only():
    from drivesync.server import is_loopback_host

    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("192.168.1.10")
    assert not is_loopback_host("example.com")


def test_run_server_rejects_non_loopback_before_starting():
    from drivesync.server import run_server

    with pytest.raises(ValueError, match="must bind to localhost"):
        run_server(None, None, host="0.0.0.0")


# ------------------------------------------------ headers and JSON errors
def test_ui_and_api_carry_security_headers(client):
    c, _ = client
    for path in ("/", "/app.js", "/api/health"):
        r = c.get(path)
        csp = r.headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp
        assert "default-src 'none'" in csp
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["Referrer-Policy"] == "no-referrer"
    assert c.get("/api/health").headers["Cache-Control"] == "no-store"


def test_refused_requests_still_get_headers(client):
    c, _ = client
    r = c.get("/api/health", headers={"X-DriveSync": "0"})
    assert r.status_code == 403
    assert r.headers["X-Frame-Options"] == "DENY"


def test_ui_has_no_inline_styles_or_scripts(client):
    """The CSP forbids them, so the page must not depend on them."""
    import re

    c, _ = client
    html = c.get("/").get_data(as_text=True)
    assert ' style="' not in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"\son[a-z]+=\"", html)


@pytest.mark.parametrize("path", [
    "/api/recovery/deletions/allow",
    "/api/recovery/deletions/keep",
    "/api/recovery/trash/restore",
    "/api/recovery/versions/restore",
])
@pytest.mark.parametrize("body", [{"pair_id": "abc"}, {"pair_id": [1]},
                                  {"pair_id": {"x": 1}}, [1, 2], "text"])
def test_bad_recovery_input_is_a_json_400(client, path, body):
    c, _ = client
    r = c.post(path, json=body)
    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.is_json and r.get_json()["ok"] is False
    assert r.get_json()["error"]


def test_unknown_api_route_and_wrong_method_are_json(client):
    c, _ = client
    r = c.get("/api/does-not-exist")
    assert r.status_code == 404 and r.get_json()["ok"] is False
    r = c.delete("/api/health")
    assert r.status_code == 405 and r.get_json()["ok"] is False


def test_unexpected_exception_is_json_without_leaking_details(
        client, monkeypatch):
    c, _ = client

    def boom(*a, **k):
        raise RuntimeError("secret internal detail /home/user/.ssh")

    monkeypatch.setattr("drivesync.server.load_config", boom)
    r = c.get("/api/status")
    assert r.status_code == 500 and r.is_json
    assert "secret internal detail" not in r.get_data(as_text=True)


def test_missing_static_file_keeps_plain_404(client):
    c, _ = client
    assert c.get("/nope.js").status_code == 404


def test_accounts_flag_only_dead_sign_ins_as_reauth(client, monkeypatch):
    from drivesync.auth import ReauthRequired
    from drivesync.drive import DriveError

    c, store = client
    store.add_account("dead@x.com", {"refresh_token": "r"})
    store.add_account("offline@x.com", {"refresh_token": "r"})
    failures = iter([ReauthRequired("reconnect me"),
                     DriveError("network unreachable")])   # accounts by id

    class FailingDrive:
        def __init__(self, provider, limiter=None):
            pass

        def about(self):
            raise next(failures)

    monkeypatch.setattr("drivesync.server.Drive", FailingDrive)
    rows = {r["email"]: r
            for r in c.get("/api/accounts").get_json()["accounts"]}
    assert rows["dead@x.com"]["reauth_required"] is True
    assert rows["offline@x.com"]["reauth_required"] is False
    assert rows["offline@x.com"]["error"] == "network unreachable"


# ---- async sign-in cancellation -------------------------------------------
def test_sign_in_job_can_be_cancelled(client, monkeypatch):
    c, _ = client
    import threading
    import time
    from drivesync import config
    import drivesync.server as srv

    config.update_config(client_id="cid", client_secret="csec")
    started = threading.Event()

    def slow_auth(cfg, cancel_event=None):
        started.set()
        cancel_event.wait(2)
        raise srv.AuthCancelled("cancelled by the user")

    monkeypatch.setattr(srv, "interactive_auth", slow_auth)
    resp = c.post("/api/accounts", json={})
    assert resp.status_code == 202
    job = resp.get_json()["job"]
    assert started.wait(2)
    assert c.delete(f"/api/accounts/auth/{job}").status_code == 200

    status = None
    for _ in range(100):
        status = c.get(f"/api/accounts/auth/{job}").get_json()
        if status["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.01)
    assert status["status"] == "cancelled"
    assert c.get("/api/accounts").get_json()["accounts"] == []


# ---- pair removal undo ----------------------------------------------------
def test_pair_removal_can_be_undone_once(client, tmp_path):
    c, store = client
    store.add_account("me@example.com", {})
    local = tmp_path / "docs"
    local.mkdir()
    pair = store.add_pair(1, str(local), "abc")
    body = c.delete(f"/api/pairs/{pair['id']}").get_json()
    assert body["ok"] and body["undo"]
    assert c.get("/api/pairs").get_json()["pairs"] == []

    restored = c.post("/api/pairs/undo", json={"undo": body["undo"]})
    assert restored.status_code == 200
    assert restored.get_json()["pair"]["id"] == pair["id"]
    assert [p["id"] for p in c.get("/api/pairs").get_json()["pairs"]] == \
        [pair["id"]]
    # the token is single-use
    assert c.post("/api/pairs/undo",
                  json={"undo": body["undo"]}).status_code == 410


def test_read_only_scope_keeps_existing_pairs_download_only(client, tmp_path):
    c, store = client
    from drivesync import config
    config.update_config(drive_scope="readonly")
    store.add_account("me@example.com", {})
    pair = store.add_pair(1, str(tmp_path), "abc", direction="download")

    refused = c.patch(f"/api/pairs/{pair['id']}",
                      json={"direction": "upload"})
    assert refused.status_code == 400
    assert "read-only" in refused.get_json()["error"]

    # non-direction edits (pause, delete toggle, ...) still work
    assert c.patch(f"/api/pairs/{pair['id']}",
                   json={"enabled": 0}).status_code == 200


def test_sign_in_persists_the_selected_drive_scope(client, monkeypatch):
    c, _ = client
    import time
    from drivesync import config
    import drivesync.server as srv

    config.update_config(client_id="cid", client_secret="csec")
    monkeypatch.setattr(srv, "interactive_auth",
                        lambda cfg, cancel_event=None: {
                            "access_token": "tok", "refresh_token": "r"})
    monkeypatch.setattr(srv, "whoami",
                        lambda tok: {"emailAddress": "me@example.com"})
    resp = c.post("/api/accounts", json={"drive_scope": "readonly"})
    assert resp.status_code == 202
    job = resp.get_json()["job"]
    for _ in range(100):
        status = c.get(f"/api/accounts/auth/{job}").get_json()
        if status["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.01)
    assert status["status"] == "done"
    assert config.load_config()["drive_scope"] == "readonly"


def test_sign_in_rejects_an_unknown_drive_scope(client):
    c, _ = client
    from drivesync import config
    config.update_config(client_id="cid", client_secret="csec")
    resp = c.post("/api/accounts", json={"drive_scope": "write"})
    assert resp.status_code == 400
    assert "drive_scope" in resp.get_json()["error"]


# ---- input validation / hardening ----------------------------------------
def test_index_disables_caching_and_sets_isolation_headers(client):
    c, _ = client
    resp = c.get("/")
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert resp.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "geolocation=()" in resp.headers["Permissions-Policy"]


def test_pair_creation_rejects_non_string_paths(client):
    c, _ = client
    resp = c.post("/api/pairs", json={"local_path": 123,
                                      "account_id": 1, "remote_id": "x"})
    assert resp.status_code == 400
    assert "local_path must be a string" in resp.get_json()["error"]


def test_pair_creation_rejects_missing_fields(client):
    c, _ = client
    resp = c.post("/api/pairs", json={"account_id": 1, "remote_id": "x"})
    assert resp.status_code == 400
    assert "missing field: local_path" in resp.get_json()["error"]
    resp = c.post("/api/pairs", json={"account_id": 1,
                                      "local_path": "/tmp"})
    assert resp.status_code == 400
    assert "missing field: remote_id" in resp.get_json()["error"]


def test_pair_update_rejects_non_string_direction(client, tmp_path):
    c, store = client
    store.add_account("me@example.com", {})
    pair = store.add_pair(1, str(tmp_path), "abc")
    resp = c.patch(f"/api/pairs/{pair['id']}", json={"direction": 5})
    assert resp.status_code == 400
    assert "direction must be a string" in resp.get_json()["error"]


def test_large_pair_delete_offers_no_undo(client, tmp_path):
    c, store = client
    store.UNDO_MAX_ENTRIES = 0
    store.add_account("me@example.com", {})
    pair = store.add_pair(1, str(tmp_path), "abc")
    store.base_put(pair["id"], "a.txt", "file")
    body = c.delete(f"/api/pairs/{pair['id']}").get_json()
    assert body["ok"] is True and body["undo"] is None


def test_second_sign_in_is_refused_while_one_is_pending(client, monkeypatch):
    c, _ = client
    import threading
    import time
    from drivesync import config
    import drivesync.server as srv

    config.update_config(client_id="cid", client_secret="csec")
    release = threading.Event()

    def slow_auth(cfg, cancel_event=None):
        release.wait(5)
        return {"access_token": "t", "refresh_token": "r"}

    monkeypatch.setattr(srv, "interactive_auth", slow_auth)
    monkeypatch.setattr(srv, "whoami",
                        lambda tok: {"emailAddress": "me@example.com"})
    first = c.post("/api/accounts", json={})
    assert first.status_code == 202
    second = c.post("/api/accounts", json={})
    assert second.status_code == 409
    assert "already in progress" in second.get_json()["error"]
    release.set()
    job = first.get_json()["job"]
    for _ in range(200):
        if c.get(f"/api/accounts/auth/{job}").get_json()["status"] == "done":
            break
        time.sleep(0.01)


def test_cancel_returns_the_jobs_real_status(client, monkeypatch):
    c, _ = client
    import threading
    import time
    from drivesync import config
    import drivesync.server as srv

    config.update_config(client_id="cid", client_secret="csec")
    release = threading.Event()

    def slow_auth(cfg, cancel_event=None):
        cancel_event.wait(5)
        from drivesync.auth import AuthCancelled
        raise AuthCancelled("cancelled")

    monkeypatch.setattr(srv, "interactive_auth", slow_auth)
    monkeypatch.setattr(srv, "whoami",
                        lambda tok: {"emailAddress": "me@example.com"})
    job = c.post("/api/accounts", json={}).get_json()["job"]
    cancelled = c.delete(f"/api/accounts/auth/{job}").get_json()
    assert cancelled["status"] == "cancelled"
    # a second cancel must not pretend the job became cancelled again
    again = c.delete(f"/api/accounts/auth/{job}").get_json()
    assert again["status"] == "cancelled"
    release.set()
    for _ in range(200):
        status = c.get(f"/api/accounts/auth/{job}").get_json()["status"]
        if status != "pending":
            break
        time.sleep(0.01)


def test_cancel_reports_a_finished_job_as_done(client, monkeypatch):
    c, _ = client
    import time
    from drivesync import config
    import drivesync.server as srv

    config.update_config(client_id="cid", client_secret="csec")
    monkeypatch.setattr(srv, "interactive_auth",
                        lambda cfg, cancel_event=None: {
                            "access_token": "t", "refresh_token": "r"})
    monkeypatch.setattr(srv, "whoami",
                        lambda tok: {"emailAddress": "me@example.com"})
    job = c.post("/api/accounts", json={}).get_json()["job"]
    for _ in range(200):
        if c.get(f"/api/accounts/auth/{job}").get_json()["status"] == "done":
            break
        time.sleep(0.01)
    assert c.delete(f"/api/accounts/auth/{job}").get_json()["status"] == "done"
