"""Drive-wide search (S2): drive.search() query building + dashboard endpoint.

The conflicts CLI (S1) tests live here too — both round on terminal access
to things the GUI already had.
"""

import time

import pytest

from drivesync.cli import main
from drivesync.config import app_dir
from drivesync.db import Store
from drivesync.drive import Drive


# ---- drive.search() ----------------------------------------------------------
class _JsonResp:
    status_code = 200
    text = ""
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _search_drive(captured, pages):
    """Drive whose session returns the given listing pages in order."""
    class Session:
        def __init__(self):
            self.n = 0

        def request(self, method, url, timeout=None, **kw):
            self.n += 1
            captured["q"] = kw["params"]["q"]
            captured["pageSize"] = kw["params"]["pageSize"]
            payload = pages[min(self.n, len(pages)) - 1]
            return _JsonResp(payload)

    drive = Drive(lambda: "tok")
    drive._session = Session()
    return drive


def test_search_builds_a_name_contains_query():
    captured = {}
    drive = _search_drive(captured, [{"files": [
        {"id": "f1", "name": "report.docx", "mimeType": "text/plain"}]}])
    out = drive.search("monthly 'report'", limit=50)
    assert captured["q"] == "name contains 'monthly report' and trashed=false"
    assert [f["id"] for f in out] == ["f1"]


def test_search_folders_only_narrows_the_query():
    captured = {}
    drive = _search_drive(captured, [{"files": []}])
    drive.search("Invoices", folders_only=True)
    assert captured["q"] == ("name contains 'Invoices' and trashed=false "
                             "and mimeType='application/vnd.google-apps.folder'")


def test_search_folders_sort_first_and_cap_results():
    captured = {}
    drive = _search_drive(captured, [{"files": [
        {"id": "f", "name": "zeta", "mimeType": "text/plain"},
        {"id": "d", "name": "alpha", "mimeType":
            "application/vnd.google-apps.folder"},
        {"id": "g", "name": "beta", "mimeType": "text/plain"}]}])
    out = drive.search("x", limit=2)
    assert captured["pageSize"] == 2
    assert [f["id"] for f in out] == ["d", "g"]      # folder first, then name


def test_search_empty_query_costs_no_call():
    captured = {}
    drive = _search_drive(captured, [{"files": [{"id": "x", "name": "x"}]}])
    assert drive.search("   ") == []
    assert "q" not in captured


def test_search_follows_pagination():
    captured = {}
    drive = _search_drive(captured, [
        {"files": [{"id": "a", "name": "a", "mimeType": "text/plain"}],
         "nextPageToken": "2"},
        {"files": [{"id": "b", "name": "b", "mimeType": "text/plain"}]}])
    out = drive.search("q", limit=10)
    assert [f["id"] for f in out] == ["a", "b"]


# ---- dashboard endpoint ------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    from drivesync.server import create_app
    store = Store(tmp_path / "state.db")
    store.add_account("me@example.com", {"access_token": "t",
                                         "refresh_token": "r",
                                         "expires_at": time.time() + 3600})
    app = create_app(store, worker=None)
    c = app.test_client()
    c.environ_base["HTTP_X_DRIVESYNC"] = app.config["API_TOKEN"]
    yield c, store
    store.close()


def test_search_endpoint_returns_rows(client, monkeypatch):
    c, _ = client
    from drivesync.drive import FOLDER_MIME

    class FakeDrive:
        def __init__(self, provider, limiter=None):
            pass

        def search(self, q, limit=50, folders_only=False):
            assert q == "invoice" and folders_only is True
            return [{"id": "abc", "name": "Invoices",
                     "mimeType": FOLDER_MIME}]

    monkeypatch.setattr("drivesync.server.Drive", FakeDrive)
    r = c.get("/api/drive/1/search?q=invoice&folders_only=1")
    assert r.status_code == 200
    items = r.get_json()["items"]
    assert [i["id"] for i in items] == ["abc"]


def test_search_endpoint_empty_query_is_empty(client):
    c, _ = client
    r = c.get("/api/drive/1/search?q=")
    assert r.status_code == 200 and r.get_json()["items"] == []


def test_search_endpoint_requires_account(client, monkeypatch):
    c, store = client
    store.delete_account(1)
    r = c.get("/api/drive/1/search?q=x")
    assert r.status_code == 502


# ---- conflicts CLI (S1) ------------------------------------------------------
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _conflict_pair():
    """A pair whose base holds one conflict copy that also exists on disk."""
    store = Store(app_dir() / "state.db")
    store.add_account("a@example.com", {"access_token": "t",
                                        "refresh_token": "r",
                                        "expires_at": time.time() + 3600})
    local = app_dir().parent / "docs"
    local.mkdir(parents=True, exist_ok=True)
    pair = store.add_pair(1, str(local), "rootX", "My Drive")
    rel = "notes.conflict-20260101-000000.txt"
    (local / rel).write_text("copy")
    store.base_put(pair["id"], rel, "file", remote_id="rid1")
    return store, pair, rel, local


def test_conflicts_list_reports_copies(home, capsys):
    store, pair, rel, local = _conflict_pair()
    try:
        main(["conflicts", "list"])
        out = capsys.readouterr().out
        assert rel in out and "notes.txt" in out     # original path shown
    finally:
        store.close()


def test_conflicts_list_empty(home, capsys):
    store = Store(app_dir() / "state.db")
    try:
        main(["conflicts", "list"])
        assert "No conflict copies" in capsys.readouterr().out
    finally:
        store.close()


def test_conflicts_resolve_keep_original(home, capsys, monkeypatch):
    store, pair, rel, local = _conflict_pair()
    trashed = []

    class FakeDrive:
        def __init__(self, provider, limiter=None):
            pass

        def trash(self, fid):
            trashed.append(fid)
            return {"id": fid}

    monkeypatch.setattr("drivesync.drive.Drive", FakeDrive)
    try:
        main(["conflicts", "resolve", rel, "--keep", "original"])
        assert trashed == ["rid1"]
        assert not (local / rel).exists()
        assert store.base_get(pair["id"], rel) is None
        assert "kept the original" in capsys.readouterr().out
    finally:
        store.close()


def test_conflicts_resolve_explicit_pair(home, capsys, monkeypatch):
    store, pair, rel, local = _conflict_pair()

    class FakeDrive:
        def __init__(self, provider, limiter=None):
            pass

        def trash(self, fid):
            return {"id": fid}

    monkeypatch.setattr("drivesync.drive.Drive", FakeDrive)
    try:
        main(["conflicts", "resolve", rel, "--pair", str(pair["id"]),
              "--keep", "original"])
        assert store.base_get(pair["id"], rel) is None
    finally:
        store.close()


def test_conflicts_resolve_requires_choice(home):
    store, _pair, rel, _local = _conflict_pair()
    try:
        with pytest.raises(SystemExit):
            main(["conflicts", "resolve", rel])
    finally:
        store.close()


def test_conflicts_resolve_unknown_path(home):
    store, _pair, _rel, _local = _conflict_pair()
    try:
        with pytest.raises(SystemExit):
            main(["conflicts", "resolve", "nope.conflict-20260101-000000.txt",
                  "--keep", "copy"])
    finally:
        store.close()


def test_pairs_set_accepts_symlink_policy(home):
    store = Store(app_dir() / "state.db")
    try:
        store.add_account("a@example.com", {"access_token": "t"})
        local = app_dir().parent / "docs2"
        local.mkdir(parents=True, exist_ok=True)
        pair = store.add_pair(1, str(local), "rootX")
        main(["pairs", "set", "--pair-id", str(pair["id"]),
              "symlink_policy=follow", "case_insensitive=true"])
        row = store.pair(pair["id"])
        assert row["symlink_policy"] == "follow"
        assert row["case_insensitive"] == 1
        with pytest.raises(SystemExit):
            main(["pairs", "set", "--pair-id", str(pair["id"]),
                  "symlink_policy=delete"])
    finally:
        store.close()
