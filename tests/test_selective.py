"""Selective sync (per-pair include scopes) + on-demand download tests.

Covers the db plumbing (normalize/parse/storage), the engine scope
filtering for full and delta scans, the on-demand download helpers, the
dashboard API and the CLI flags.
"""

import os

import pytest

from drivesync.db import normalize_include_patterns, parse_include_patterns
from drivesync.engine import OnDemandSkip, download_item, download_tree, \
    included
from drivesync.engine import scan_local


# ---- db plumbing ---------------------------------------------------------
def test_normalize_include_patterns_forms():
    assert normalize_include_patterns(None) == "[]"
    assert normalize_include_patterns("") == "[]"
    assert normalize_include_patterns([]) == "[]"
    # list form: sorted, deduped, slashes trimmed
    assert normalize_include_patterns(["photos", "docs/", "docs"]) == \
        '["docs", "photos"]'
    # comma form for the CLI
    assert normalize_include_patterns("docs, photos/2024") == \
        '["docs", "photos/2024"]'
    # json list string round-trips
    assert normalize_include_patterns('["docs"]') == '["docs"]'
    # junk path segments dropped
    assert normalize_include_patterns(["/docs//", "."]) == '["docs"]'


def test_normalize_include_patterns_rejects_junk():
    with pytest.raises(ValueError):
        normalize_include_patterns(42)
    with pytest.raises(ValueError):
        normalize_include_patterns(["docs", 3])
    with pytest.raises(ValueError):
        normalize_include_patterns('{"a": 1}')   # json but not a list


def test_parse_include_patterns_degrades_to_everything():
    assert parse_include_patterns(None) == []
    assert parse_include_patterns("") == []
    assert parse_include_patterns("not json{") == []     # legacy/broken value
    assert parse_include_patterns('["docs", "photos"]') == ["docs", "photos"]


def test_pair_include_roundtrip(env):
    env.store.update_pair(env.pair["id"], include_patterns=["docs"])
    row = env.store.pair(env.pair["id"])
    assert parse_include_patterns(row["include_patterns"]) == ["docs"]
    env.store.update_pair(env.pair["id"], include_patterns="photos,docs")
    row = env.store.pair(env.pair["id"])
    assert parse_include_patterns(row["include_patterns"]) == \
        ["docs", "photos"]
    with pytest.raises(ValueError):
        env.store.update_pair(env.pair["id"], include_patterns=17)


# ---- scope matching -------------------------------------------------------
def test_included_semantics():
    assert included("anything", [])              # no scope = everything
    assert included("docs", ["docs"])
    assert included("docs/a/b.txt", ["docs"])
    assert included("docs", ["docs/2024"])       # ancestor dir stays walkable
    assert included("docs/2024/x", ["docs/2024"])
    assert not included("photos/a", ["docs"])
    assert not included("docs2/a", ["docs"])     # no accidental prefix match
    # loose files in the pair root always mirror
    assert included("top.txt", ["docs"], is_file=True)
    # ...but root-level directories obey the scope
    assert not included("pics", ["docs"])


# ---- selective sync via the engine ---------------------------------------
def _seed_remote(env):
    env.drive.put_remote_file("docs/a.txt", "alpha")
    env.drive.put_remote_file("docs/nested/b.txt", "beta")
    env.drive.put_remote_file("photos/c.jpg", "gamma")
    env.drive.put_remote_file("top.txt", "root file")


def _set_scope(env, include):
    env.store.update_pair(env.pair["id"], include_patterns=include or [])
    env.pair = env.store.pair(env.pair["id"])


def test_scope_limits_first_download(env):
    _seed_remote(env)
    _set_scope(env, ["docs"])
    env.sync()
    local = scan_local(str(env.local), [])
    assert "docs/a.txt" in local and "docs/nested" in local
    assert "photos" not in local and "photos/c.jpg" not in local
    assert "top.txt" in local                     # loose root file mirrors
    base = env.base()
    assert "photos/c.jpg" not in base             # never tracked


def test_scope_chosen_at_pair_creation_protects_the_first_pass(env):
    """The GUI and dashboard let users pick subfolders before the pair
    exists; the very first pass must already respect that scope (otherwise
    unwanted folders are downloaded before the user can narrow it)."""
    from drivesync import engine

    _seed_remote(env)
    other = env.root / "other"
    other.mkdir()
    pair = env.store.add_pair(1, str(other), env.drive.root_id,
                              include_patterns=["docs"])
    stats, ok = engine.sync_pair(env.store, env.cfg, pair, env.drive)
    assert ok
    assert (other / "docs" / "a.txt").exists()
    assert not (other / "photos").exists()
    assert "top.txt" in scan_local(str(other), [])


def test_scope_excluded_local_files_are_never_uploaded(env):
    _seed_remote(env)
    _set_scope(env, ["docs"])
    env.sync()
    (env.local / "photos").mkdir()
    (env.local / "photos" / "local-only.txt").write_text("secret")
    env.sync()
    with pytest.raises(KeyError):
        env.drive._find_by_rel("photos/local-only.txt")   # not on Drive


def test_narrowing_scope_keeps_both_copies_and_forgets(env):
    _seed_remote(env)
    env.sync()                                    # everything mirrored
    assert (env.local / "photos" / "c.jpg").exists()
    _set_scope(env, ["docs"])
    env.sync()
    base = env.base()
    assert "photos" not in base and "photos/c.jpg" not in base
    assert (env.local / "photos" / "c.jpg").exists()      # kept on disk
    assert env.drive.read_remote("photos/c.jpg") == b"gamma"  # kept on Drive


def test_widening_scope_downloads_on_next_pass(env):
    _seed_remote(env)
    _set_scope(env, ["docs"])
    env.sync()
    assert not (env.local / "photos").exists()
    _set_scope(env, ["docs", "photos"])
    env.sync()
    assert (env.local / "photos" / "c.jpg").read_bytes() == b"gamma"


def test_scope_respected_in_delta_scan(env):
    from drivesync import engine
    _seed_remote(env)
    _set_scope(env, ["docs"])
    env.sync()
    token = env.drive.changes_start_token()
    env.drive.put_remote_file("photos/new.bin", "later")   # out of scope
    env.drive.put_remote_file("docs/new.doc", "in scope")  # in scope
    feed, _tok = env.drive.changes_list(token)
    stats, ok = engine.sync_pair(env.store, env.cfg, env.pair, env.drive,
                                 changes=feed)
    assert ok
    assert (env.local / "docs" / "new.doc").exists()
    assert not (env.local / "photos" / "new.bin").exists()


def test_root_gdoc_inside_scope_still_exports(env):
    env.drive.put_remote_file("docs/report", "body")
    fid = env.drive._find_by_rel("docs/report")
    env.drive.files[fid]["mimeType"] = "application/vnd.google-apps.document"
    _set_scope(env, ["docs"])
    stats, ok = env.sync()
    assert ok
    assert (env.local / "docs" / "report.docx").exists()


# ---- on-demand downloads --------------------------------------------------
GDOC_MAP = {"application/vnd.google-apps.document": (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".docx")}


def _make_gdoc(env, rel="Report"):
    fid = env.drive.put_remote_file(rel, "doc body")
    env.drive.files[fid]["mimeType"] = "application/vnd.google-apps.document"
    return fid


def test_download_item_regular_file(env, tmp_path):
    env.drive.put_remote_file("pics/cat.jpg", "meow")
    fid = env.drive._find_by_rel("pics/cat.jpg")
    meta = env.drive.get(fid)
    dest = tmp_path / "cat.jpg"
    written = download_item(env.drive, meta, str(dest))
    assert written == str(dest)
    assert dest.read_bytes() == b"meow"
    assert os.path.getmtime(dest) > 0             # Drive mtime stamped


def test_download_item_gdoc_exports_with_extension(env, tmp_path):
    _make_gdoc(env, "Report")
    fid = env.drive._find_by_rel("Report")
    meta = env.drive.get(fid)
    dest = tmp_path / "Report"
    written = download_item(env.drive, meta, str(dest), GDOC_MAP)
    assert written == str(tmp_path / "Report.docx")
    assert (tmp_path / "Report.docx").read_bytes() == b"doc body"


def test_download_item_skips_unconvertible_docs(env, tmp_path):
    _make_gdoc(env, "Report")
    fid = env.drive._find_by_rel("Report")
    meta = env.drive.get(fid)
    with pytest.raises(OnDemandSkip):
        download_item(env.drive, meta, str(tmp_path / "Report"), {})


def test_download_item_rejects_folders(env, tmp_path):
    meta = {"id": env.drive.put_remote_folder("docs2"),
            "name": "docs2", "mimeType": "application/vnd.google-apps.folder"}
    with pytest.raises(ValueError):
        download_item(env.drive, meta, str(tmp_path / "x"))


def test_download_tree_mirrors_structure_and_counts(env, tmp_path):
    env.drive.put_remote_file("docs/a.txt", "alpha")
    env.drive.put_remote_file("docs/nested/b.txt", "beta")
    env.drive.put_remote_file("docs/empty/c.txt", "cee")
    env.drive.put_remote_folder("docs/void")      # truly empty folder
    _make_gdoc(env, "docs/mystery")               # no export map -> skipped
    dest = tmp_path / "out"
    stats = download_tree(env.drive, env.drive.root_id, str(dest),
                          export_map={})
    assert stats["downloaded"] == 3 and stats["skipped"] == 1
    assert stats["errors"] == 0 and stats["bytes"] > 0
    assert (dest / "docs" / "a.txt").read_bytes() == b"alpha"
    assert (dest / "docs" / "nested" / "b.txt").read_bytes() == b"beta"
    assert (dest / "docs" / "void").is_dir()      # empty folders land too
    assert not (dest / "docs" / "mystery").exists()


def test_download_tree_serializes_notes(env, tmp_path):
    """log() receives the notes after the parallel phase — one row per file."""
    env.drive.put_remote_file("docs/a.txt", "alpha")
    env.drive.put_remote_file("docs/b.txt", "beta")
    seen = []
    download_tree(env.drive, env.drive.root_id, str(tmp_path / "out"),
                  concurrency=4,
                  log=lambda rel, msg, level="info": seen.append((rel, level)))
    assert len(seen) == 2 and all(lvl == "info" for _rel, lvl in seen)


def test_download_tree_progress_streams_per_file(env, tmp_path):
    """progress() fires once per completed file with live counts (0-based
    done, total known upfront) — this is what the GUI strip renders."""
    env.drive.put_remote_file("docs/a.txt", "alpha")
    env.drive.put_remote_file("docs/b.txt", "beta")
    env.drive.put_remote_file("docs/c.txt", "cee")
    _make_gdoc(env, "docs/mystery")               # skip kind, still reported
    events = []
    stats = download_tree(env.drive, env.drive.root_id, str(tmp_path / "out"),
                          concurrency=4,
                          progress=lambda done, total, rel, kind:
                          events.append((done, total, rel, kind)))
    assert [done for done, _t, _r, _k in events] == [1, 2, 3, 4]
    assert all(total == 4 for _d, total, _r, _k in events)
    kinds = {rel: kind for _d, _t, rel, kind in events}
    assert kinds["docs/a.txt"] == "ok" and kinds["docs/mystery"] == "skip"
    assert stats["downloaded"] == 3 and stats["skipped"] == 1


def test_download_tree_progress_single_worker(env, tmp_path):
    """The serial path (concurrency=1) reports progress the same way."""
    env.drive.put_remote_file("docs/a.txt", "alpha")
    env.drive.put_remote_file("docs/b.txt", "beta")
    events = []
    download_tree(env.drive, env.drive.root_id, str(tmp_path / "out"),
                  concurrency=1,
                  progress=lambda done, total, rel, kind:
                  events.append((done, total)))
    assert events == [(1, 2), (2, 2)]


# ---- dashboard API --------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    """Same fixture as test_server.py (fixtures are module-local)."""
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    from drivesync.db import Store
    from drivesync.server import create_app
    store = Store(tmp_path / "state.db")
    app = create_app(store, worker=None)
    c = app.test_client()
    c.environ_base["HTTP_X_DRIVESYNC"] = "1"
    yield c, store
    store.close()


def fake_folder_drive(monkeypatch):
    from drivesync.drive import FOLDER_MIME

    class FakeDrive:
        def __init__(self, provider, limiter=None):
            pass

        def get(self, file_id, fields=None):
            return {"id": file_id, "name": file_id, "mimeType": FOLDER_MIME}

    monkeypatch.setattr("drivesync.server.Drive", FakeDrive)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Same fixture as test_cli.py: isolated app data dir."""
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_children_endpoint_lists_files_and_folders(client, monkeypatch):
    from drivesync.drive import FOLDER_MIME

    c, store = client
    store.add_account("me@example.com", {
        "access_token": "tok", "refresh_token": "r", "expires_at": 9e12})

    class FakeDrive:
        def __init__(self, provider):
            pass

        def list_children(self, parent):
            return [{"id": "f1", "name": "Photos", "mimeType": FOLDER_MIME},
                    {"id": "x1", "name": "notes.txt", "mimeType": "text/plain",
                     "size": "12", "modifiedTime": "2026-11-18T10:00:00Z"}]

        def get(self, file_id, fields=None):
            return {"id": file_id, "name": "Sub", "parents": ["root"]}

    monkeypatch.setattr("drivesync.server.Drive", FakeDrive)

    body = c.get("/api/drive/1/children").get_json()
    assert body["ok"] is True and body["current"] is None
    kinds = {i["id"]: i["mimeType"] for i in body["items"]}
    assert kinds == {"f1": FOLDER_MIME, "x1": "text/plain"}
    sub = c.get("/api/drive/1/children?parent=abc").get_json()
    assert sub["current"]["name"] == "Sub"


def test_pair_include_patterns_roundtrip(client, tmp_path, monkeypatch):
    fake_folder_drive(monkeypatch)
    c, store = client
    store.add_account("me@example.com", {})
    docs = tmp_path / "docs"
    docs.mkdir()
    resp = c.post("/api/pairs", json={
        "account_id": 1, "local_path": str(docs), "remote_id": "abc",
        "include_patterns": ["docs", "photos"]})
    pair = resp.get_json()["pair"]
    assert pair["include_patterns"] == ["docs", "photos"]
    upd = c.patch(f"/api/pairs/{pair['id']}",
                  json={"include_patterns": "pics,docs"}).get_json()
    assert upd["pair"]["include_patterns"] == ["docs", "pics"]
    bad = c.patch(f"/api/pairs/{pair['id']}", json={"include_patterns": 5})
    assert bad.status_code == 400


# ---- CLI ------------------------------------------------------------------
def test_cli_pairs_include_flags(home, capsys):
    from drivesync.cli import main
    from drivesync.db import Store, parse_include_patterns

    docs = home.parent / "docs"
    docs.mkdir()
    s = Store(home / "state.db")
    s.add_account("a@example.com", {})
    s.close()
    main(["pairs", "add", str(docs), "--remote-id", "abc123",
          "--include", "docs,photos"])
    out = capsys.readouterr().out
    assert "added pair #1" in out
    s = Store(home / "state.db")
    row = s.pairs()[0]
    assert parse_include_patterns(row["include_patterns"]) == \
        ["docs", "photos"]
    s.close()
    main(["pairs", "list"])
    assert "subfolders: docs, photos" in capsys.readouterr().out
    main(["pairs", "set", "--pair-id", "1", "include_patterns="])
    s = Store(home / "state.db")
    assert parse_include_patterns(s.pairs()[0]["include_patterns"]) == []
    s.close()
    main(["pairs", "list"])
    assert "scope: everything" in capsys.readouterr().out


# ---- GUI helpers (need PyGObject for the module import) -------------------
def test_startup_page_logic(tmp_path, monkeypatch):
    pytest.importorskip("gi")
    from drivesync.gui import PAGE_ACTIVITY, startup_page

    assert startup_page({}, []) == 1                       # Setup
    assert startup_page({"client_id": "x"}, []) == 2       # Accounts
    assert startup_page({"client_id": "x"}, [{"id": 1}]) == 0  # Overview


def test_drive_row_and_bytes_helpers():
    pytest.importorskip("gi")
    from drivesync.gui import _drive_bytes, _drive_row
    from drivesync.drive import FOLDER_MIME

    assert _drive_bytes(0) == "" and _drive_bytes(None) == ""
    assert _drive_bytes(2048) == "2.0 KB"
    folder = _drive_row({"id": "f1", "name": "Docs",
                         "mimeType": FOLDER_MIME,
                         "modifiedTime": "2026-11-18T10:00:00Z"})
    # columns: name, kind, size, modified, owner, id, mime, raw size
    assert folder[:2] == ["Docs", "Folder"]
    assert folder[5] == "f1" and folder[6] == FOLDER_MIME and folder[7] == ""
    doc = _drive_row({"id": "d1", "name": "Report",
                      "mimeType": "application/vnd.google-apps.document"})
    assert doc[1] == "Google Doc" and doc[2] == ""     # no size for docs
    assert doc[7] == ""
    shortcut = _drive_row({"id": "s1", "name": "Team",
                           "mimeType": "application/vnd.google-apps.shortcut",
                           "shortcutDetails": {"targetId": "real",
                            "targetMimeType": FOLDER_MIME}})
    assert shortcut[1] == "Folder" and shortcut[5] == "real"
    file_row = _drive_row({"id": "x1", "name": "notes.txt",
                           "mimeType": "text/plain", "size": "12",
                           "modifiedTime": "2026-11-18T10:00:00Z"})
    assert file_row[1] == "Text file" and file_row[2] == "12 B"
