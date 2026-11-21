"""Phase 2.5 — Google Drive browser: navigation, metadata, search, paging.

Covers the browser backend (breadcrumbs, file categories, owner display,
pagination) and the dashboard browser endpoints, including the roadmap's
error scenarios: empty folders, invalid folder ids, API/permission errors
and inaccessible accounts.
"""

import pytest

from drivesync.drive import (Drive, DriveError, FOLDER_MIME, SHORTCUT_MIME,
                             breadcrumbs, file_category, owner_name)

from test_drive import FakeResp


# ---- classification --------------------------------------------------

def test_file_category_buckets():
    assert file_category({"mimeType": FOLDER_MIME}) == "folder"
    assert file_category({"mimeType": "application/pdf"}) == "pdf"
    assert file_category({"mimeType": "image/png"}) == "image"
    assert file_category({"mimeType": "video/mp4"}) == "video"
    assert file_category({"mimeType": "audio/ogg"}) == "audio"
    assert file_category({"mimeType": "text/plain"}) == "text"
    assert file_category({"mimeType": "application/rss+xml"}) == "text"
    assert file_category({"mimeType": "application/json"}) == "text"
    assert file_category({"mimeType": "application/zip"}) == "archive"
    assert file_category({"mimeType": "application/x-7z-compressed"}) == \
        "archive"
    assert file_category({"mimeType": "application/octet-stream"}) == "other"
    assert file_category({}) == "other"


def test_file_category_google_native_kinds():
    apps = "application/vnd.google-apps."
    assert file_category({"mimeType": apps + "document"}) == "document"
    assert file_category({"mimeType": apps + "spreadsheet"}) == "spreadsheet"
    assert file_category({"mimeType": apps + "presentation"}) == "presentation"
    assert file_category({"mimeType": apps + "drawing"}) == "drawing"
    assert file_category({"mimeType": apps + "form"}) == "form"
    # a native kind the maps do not know is still a Google file, not "other"
    assert file_category({"mimeType": apps + "fusiontable"}) == "doc"


def test_file_category_folder_shortcuts_classify_as_their_target():
    to_folder = {"id": "s1", "name": "Team", "mimeType": SHORTCUT_MIME,
                 "shortcutDetails": {"targetId": "t1",
                                     "targetMimeType": FOLDER_MIME}}
    assert file_category(to_folder) == "folder"
    to_pdf = {"id": "s2", "name": "Doc", "mimeType": SHORTCUT_MIME,
              "shortcutDetails": {"targetId": "t2",
                                  "targetMimeType": "application/pdf"}}
    assert file_category(to_pdf) == "pdf"
    # an unresolved shortcut (no target details yet) stays visible
    assert file_category({"mimeType": SHORTCUT_MIME}) == "shortcut"


def test_owner_name_prefers_display_name():
    assert owner_name({"owners": [{"displayName": "Ada",
                                   "emailAddress": "ada@x.com"}]}) == "Ada"
    assert owner_name({"owners": [{"emailAddress": "ada@x.com"}]}) == \
        "ada@x.com"


def test_owner_name_degrades_to_empty_string():
    assert owner_name({"owners": []}) == ""
    assert owner_name({}) == ""
    assert owner_name({"owners": None}) == ""
    assert owner_name({"owners": "odd"}) == ""
    assert owner_name({"owners": [None, "odd"]}) == ""
    assert owner_name({"owners": [{"displayName": None}]}) == ""


# ---- breadcrumbs (folder navigation) ---------------------------------

class ChainDrive:
    """Fake Drive whose get() answers from a parent-chain table."""

    def __init__(self, meta_by_id, fail_after=0):
        self.meta = meta_by_id
        self.fail_after = fail_after      # raise on the (n+1)-th get
        self.gets = []

    def get(self, file_id, fields=None):
        self.gets.append(file_id)
        if len(self.gets) > self.fail_after > 0:
            raise DriveError(f"GET files/{file_id} -> 403: denied")
        try:
            return self.meta[file_id]
        except KeyError:
            raise DriveError(f"GET files/{file_id} -> 404: not found")


def test_breadcrumbs_walks_parents_root_first():
    drive = ChainDrive({
        "rootid": {"id": "rootid", "name": "My Drive", "parents": []},
        "research": {"id": "research", "name": "Research",
                     "parents": ["rootid"]},
        "experiments": {"id": "experiments", "name": "Experiments",
                        "parents": ["research"]},
    })
    chain = breadcrumbs(drive, "experiments")
    assert [(c["id"], c["name"]) for c in chain] == [
        ("rootid", "My Drive"), ("research", "Research"),
        ("experiments", "Experiments")]
    assert drive.gets == ["experiments", "research", "rootid"]


def test_breadcrumbs_virtual_roots_never_hit_the_api():
    class Boom:
        def get(self, file_id, fields=None):
            raise AssertionError("virtual roots must not query the API")

    assert breadcrumbs(Boom(), "root") == \
        [{"id": "root", "name": "My Drive"}]
    assert breadcrumbs(Boom(), "shared") == \
        [{"id": "shared", "name": "Shared with me"}]


def test_breadcrumbs_invalid_folder_raises():
    drive = ChainDrive({})
    with pytest.raises(DriveError):
        breadcrumbs(drive, "bogus-folder-id")


def test_breadcrumbs_unreachable_ancestor_truncates():
    """The folder itself resolves but a parent 403s: show the partial
    chain instead of failing the whole listing."""
    drive = ChainDrive({
        "leaf": {"id": "leaf", "name": "Leaf", "parents": ["hidden"]},
    }, fail_after=1)
    chain = breadcrumbs(drive, "leaf")
    assert [c["id"] for c in chain] == ["leaf"]


def test_breadcrumbs_parent_loop_is_capped_not_hung():
    """A crafted parent cycle must not hang the walk: the seen-set breaks
    it, and a chain that would outrun the depth cap is truncated instead."""
    meta = {"a": {"id": "a", "name": "A", "parents": ["b"]},
            "b": {"id": "b", "name": "B", "parents": ["a"]}}
    chain = breadcrumbs(ChainDrive(meta), "a")
    assert [c["id"] for c in chain] == ["b", "a"]   # cycle detected, no hang

    # a >32-deep honest chain is truncated at MAX_BREADCRUMB_DEPTH
    deep = {}
    for i in range(0, 40):
        parent = [] if i == 39 else [f"f{i + 1}"]
        deep[f"f{i}"] = {"id": f"f{i}", "name": f"F{i}", "parents": parent}
    chain = breadcrumbs(ChainDrive(deep), "f0")
    assert len(chain) == 32
    assert chain[-1]["id"] == "f0"


def test_breadcrumbs_stops_at_literal_root_parent():
    drive = ChainDrive({
        "top": {"id": "top", "name": "Top", "parents": ["root"]},
        "leaf": {"id": "leaf", "name": "Leaf", "parents": ["top"]},
    })
    chain = breadcrumbs(drive, "leaf")
    assert [c["id"] for c in chain] == ["top", "leaf"]


# ---- listing fields and API pagination --------------------------------

class PageSession:
    """Queued JSON responses for _request(); last one repeats."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def request(self, *a, **kw):
        self.calls.append(kw)
        body = self.payloads.pop(0) if len(self.payloads) > 1 \
            else self.payloads[0]
        return FakeResp(200, body)


def test_list_children_requests_owners_and_walks_api_pages():
    """Two API pages must be stitched into one listing, and the Owner
    column's fields must be requested (Phase 2.5 metadata)."""
    page1 = ('{"nextPageToken": "t2", "files": [{"id": "a", "name": "A", '
             '"mimeType": "' + FOLDER_MIME + '"}]}')
    page2 = ('{"files": [{"id": "b", "name": "b.txt", "mimeType": '
             '"text/plain", "owners": [{"displayName": "Ada"}]}]}')
    d = Drive(lambda: "tok")
    d._session = PageSession([page1, page2])
    rows = d.list_children("root")
    assert [r["id"] for r in rows] == ["a", "b"]
    assert rows[1]["owners"][0]["displayName"] == "Ada"
    fields = d._session.calls[0]["params"]["fields"]
    assert "owners(displayName,emailAddress)" in fields
    assert "pageToken" not in d._session.calls[0]["params"]
    assert d._session.calls[1]["params"]["pageToken"] == "t2"


def test_list_children_empty_folder_returns_no_rows():
    d = Drive(lambda: "tok")
    d._session = PageSession(['{"files": []}'])
    assert d.list_children("empty-folder") == []


def test_search_requests_owners_too():
    d = Drive(lambda: "tok")
    d._session = PageSession(['{"files": []}'])
    assert d.search("needle") == []
    assert "owners(displayName,emailAddress)" in \
        d._session.calls[0]["params"]["fields"]


# ---- dashboard browser endpoints --------------------------------------

@pytest.fixture
def browser_client(tmp_path, monkeypatch):
    """Flask test client with one account and a chain-aware fake Drive."""
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    from drivesync.db import Store
    from drivesync.server import create_app

    store = Store(tmp_path / "state.db")
    store.add_account("me@example.com", {
        "access_token": "tok", "refresh_token": "r", "expires_at": 9e12})
    app = create_app(store, worker=None)
    c = app.test_client()
    c.environ_base["HTTP_X_DRIVESYNC"] = "1"
    yield c, store
    store.close()


class BrowserDrive:
    """Fake Drive for the browser endpoints: a Research/Experiments tree."""

    meta = {
        "rootid": {"id": "rootid", "name": "My Drive", "parents": []},
        "research": {"id": "research", "name": "Research",
                     "parents": ["rootid"]},
        "empty": {"id": "empty", "name": "Empty", "parents": ["rootid"]},
    }
    children = {
        "root": [{"id": "research", "name": "Research",
                  "mimeType": FOLDER_MIME},
                 {"id": "f2", "name": "report.pdf", "mimeType": "application/pdf",
                  "size": "2048", "modifiedTime": "2026-11-18T10:00:00Z",
                  "owners": [{"displayName": "Ada"}]},
                 {"id": "s1", "name": "Team", "mimeType": SHORTCUT_MIME,
                  "shortcutDetails": {"targetId": "research",
                                      "targetMimeType": FOLDER_MIME}}],
        "research": [{"id": "g1", "name": "Notes",
                      "mimeType": "application/vnd.google-apps.document"}],
        "empty": [],
    }

    def __init__(self, provider, limiter=None):
        pass

    def get(self, file_id, fields=None):
        try:
            return dict(self.meta[file_id])
        except KeyError:
            raise DriveError(f"GET files/{file_id} -> 404: not found")

    def list_children(self, parent):
        return [dict(f) for f in self.children.get(parent, [])]

    def list_shared_folders(self):
        return [{"id": "sh1", "name": "From Other Account",
                 "owners": [{"displayName": "Ada"}]}]

    def search(self, query, limit=50, folders_only=False):
        return [dict(f) for f in self.children["root"]]


def test_children_endpoint_navigation_metadata_and_shortcuts(
        browser_client, monkeypatch):
    c, _ = browser_client
    monkeypatch.setattr("drivesync.server.Drive", BrowserDrive)

    body = c.get("/api/drive/1/children").get_json()
    assert body["ok"] is True and body["current"] is None
    assert [b["id"] for b in body["breadcrumbs"]] == ["root"]
    rows = {i["id"]: i for i in body["items"]}
    assert rows["f2"]["category"] == "pdf"
    assert rows["f2"]["owner"] == "Ada"
    assert rows["research"]["category"] == "folder"
    assert rows["s1"]["category"] == "folder"     # folder shortcut

    sub = c.get("/api/drive/1/children?parent=research").get_json()
    assert sub["current"]["name"] == "Research"
    assert [b["id"] for b in sub["breadcrumbs"]] == ["rootid", "research"]
    assert sub["items"][0]["category"] == "document"
    assert "size" not in sub["items"][0]   # Google docs carry no size


def test_children_endpoint_empty_folder(browser_client, monkeypatch):
    c, _ = browser_client
    monkeypatch.setattr("drivesync.server.Drive", BrowserDrive)
    body = c.get("/api/drive/1/children?parent=empty").get_json()
    assert body["ok"] is True
    assert body["items"] == []
    assert [b["id"] for b in body["breadcrumbs"]] == ["rootid", "empty"]
    # an unknown folder id is a different case: a clean error, no rows
    bad = c.get("/api/drive/1/children?parent=ghost").get_json()
    assert bad["ok"] is False and "404" in bad["error"]


def test_children_endpoint_invalid_folder_is_502(browser_client,
                                                 monkeypatch):
    c, _ = browser_client
    monkeypatch.setattr("drivesync.server.Drive", BrowserDrive)
    resp = c.get("/api/drive/1/children?parent=bogus-id")
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["ok"] is False and "bogus-id" in body["error"]


def test_children_endpoint_permission_error_is_502(browser_client,
                                                   monkeypatch):
    """Inaccessible folders surface Google's message instead of a 500."""
    c, _ = browser_client

    class DeniedDrive(BrowserDrive):
        def list_children(self, parent):
            raise DriveError("GET files/x -> 403: PERMISSION_DENIED "
                             "-> Google refused the Drive call")

    monkeypatch.setattr("drivesync.server.Drive", DeniedDrive)
    resp = c.get("/api/drive/1/children?parent=research")
    assert resp.status_code == 502
    assert "PERMISSION_DENIED" in resp.get_json()["error"]


def test_drive_endpoints_with_unknown_account_are_502(browser_client):
    c, _ = browser_client
    resp = c.get("/api/drive/999/children")
    assert resp.status_code == 502
    assert "account not found" in resp.get_json()["error"]


def test_drive_endpoints_with_broken_credentials_are_502(browser_client):
    """An account whose refresh token is missing (re-auth needed) must be
    a clean 502 JSON error — never a 500 HTML page the browser cannot read."""
    c, store = browser_client
    store.add_account("reauth@x.com", {})
    resp = c.get("/api/drive/2/children")
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["ok"] is False and "re-run authorization" in body["error"]
    assert c.get("/api/drive/2/folders").status_code == 502
    assert c.get("/api/drive/2/search?q=x").status_code == 502


def test_folders_endpoint_breadcrumbs_and_shared_view(browser_client,
                                                      monkeypatch):
    c, _ = browser_client
    monkeypatch.setattr("drivesync.server.Drive", BrowserDrive)

    body = c.get("/api/drive/1/folders?parent=research").get_json()
    assert body["ok"] is True
    assert [b["name"] for b in body["breadcrumbs"]] == ["My Drive",
                                                        "Research"]
    assert body["current"]["id"] == "research"
    # only pickable rows (the shortcut resolves, the pdf is skipped)
    assert [f["id"] for f in body["folders"]] == []

    root = c.get("/api/drive/1/folders").get_json()
    assert root["current"] is None
    assert root["folders"][0] == {"id": "shared", "name": "Shared with me"}

    shared = c.get("/api/drive/1/folders?parent=shared").get_json()
    assert shared["current"] == {"id": "shared", "name": "Shared with me"}
    assert [f["id"] for f in shared["folders"]] == ["sh1"]


def test_search_endpoint_decorates_rows(browser_client, monkeypatch):
    c, _ = browser_client
    monkeypatch.setattr("drivesync.server.Drive", BrowserDrive)
    body = c.get("/api/drive/1/search?q=report").get_json()
    assert body["ok"] is True
    rows = {i["id"]: i for i in body["items"]}
    assert rows["f2"]["category"] == "pdf"
    assert rows["f2"]["owner"] == "Ada"
    assert rows["s1"]["category"] == "folder"
    assert rows["research"]["category"] == "folder"


def test_search_endpoint_without_query_lists_nothing(browser_client):
    c, _ = browser_client
    body = c.get("/api/drive/1/search").get_json()
    assert body == {"ok": True, "items": []}


# ---- desktop browser rows (needs PyGObject) ---------------------------

def test_drive_row_shape_and_owner_column():
    """The desktop browser's 8-column row: owner visible, ids/mime hidden,
    folder shortcuts resolved to their target. Every value must be str so
    the str-only ListStore never raises on append."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    from drivesync import gui as gui_mod

    view = Gtk.ListStore(str, str, str, str, str, str, str, str)
    rows = [
        {"id": "f2", "name": "report.pdf", "mimeType": "application/pdf",
         "size": "2048", "modifiedTime": "2026-11-18T10:00:00Z",
         "owners": [{"displayName": "Ada"}]},
        {"id": "s1", "name": "Team", "mimeType": SHORTCUT_MIME,
         "shortcutDetails": {"targetId": "t9", "targetMimeType": FOLDER_MIME}},
        {"id": "g1", "name": "Notes",
         "mimeType": "application/vnd.google-apps.document"},
    ]
    for item in rows:
        row = gui_mod._drive_row(item)
        assert len(row) == view.get_n_columns()
        assert all(isinstance(v, str) for v in row)
        view.append(row)               # the exact call the browser makes
    assert view[0][4] == "Ada"         # Owner column
    assert view[0][7] == "2048"        # hidden raw size
    assert view[1][5] == "t9"          # shortcut resolves to the target id
    assert view[1][1] == gui_mod.i18n.t("Folder")
    assert view[2][2] == ""            # Google docs have no size


def test_drive_type_and_sort_tables_stay_aligned():
    """The combo tables are index-aligned; a mismatch would silently show
    the wrong rows. Also every category token must have a Kind label."""
    gi = pytest.importorskip("gi")

    from drivesync import gui as gui_mod
    from drivesync.drive import file_category

    assert len(gui_mod._DRIVE_TYPE_KEYS) == len(gui_mod._DRIVE_TYPE_TOKENS)
    assert len(gui_mod._DRIVE_SORT_KEYS) == len(gui_mod._DRIVE_SORT_ORDERS)
    tokens = {"folder", "shortcut", "document", "spreadsheet", "presentation",
              "drawing", "form", "map", "site", "doc", "pdf", "image",
              "video", "audio", "archive", "text", "other"}
    for token in tokens:
        assert token in gui_mod._DRIVE_KIND_KEYS, token
        assert file_category({"mimeType": "x/y"}) is not None  # smoke
    covered = set().union(*(t for t in gui_mod._DRIVE_TYPE_TOKENS if t))
    assert tokens <= covered | {"folder"}   # 'All types' covers the rest
