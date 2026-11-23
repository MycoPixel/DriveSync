"""Read-only FUSE tree logic (the mount itself needs fusepy + libfuse)."""

import errno
import os
import stat

import pytest

from drivesync.drive import FOLDER_MIME, SHORTCUT_MIME
from drivesync.fuse import DriveFS, FuseUnavailable, mount


class FakeDrive:
    def __init__(self, tree, files=None, exports=None):
        self.tree = tree
        self.files = files or {}
        self.exports = exports or {}
        self.calls = []

    def list_children(self, folder_id):
        self.calls.append(("list", folder_id))
        return self.tree.get(folder_id, [])

    def download(self, file_id, dest):
        self.calls.append(("download", file_id))
        with open(dest, "wb") as fh:
            fh.write(self.files.get(file_id, b""))

    def export(self, file_id, mime, dest):
        self.calls.append(("export", file_id, mime))
        with open(dest, "wb") as fh:
            fh.write(self.exports.get(file_id, b"exported"))


GDOC = "application/vnd.google-apps.document"
DOCX = (("application/vnd.openxmlformats-officedocument.wordprocessingml."
         "document"), ".docx")


def make_fs(tmp_path, **kw):
    tree = {
        "root": [
            {"id": "f1", "name": "Photos", "mimeType": FOLDER_MIME,
             "modifiedTime": "2026-01-02T03:04:05Z"},
            {"id": "a1", "name": "notes.txt", "mimeType": "text/plain",
             "size": "5", "modifiedTime": "2026-01-02T03:04:05Z"},
            {"id": "d1", "name": "Report", "mimeType": GDOC,
             "modifiedTime": "2026-01-02T03:04:05Z"},
            {"id": "s1", "name": "Team", "mimeType": SHORTCUT_MIME,
             "shortcutDetails": {"targetId": "f2",
                                 "targetMimeType": FOLDER_MIME}},
            {"id": "x1", "name": "form", "mimeType":
             "application/vnd.google-apps.form"},
        ],
        "f1": [
            {"id": "a2", "name": "pic.jpg", "mimeType": "image/jpeg",
             "size": "9"},
        ],
        "f2": [],
    }
    drive = FakeDrive(tree, files={"a1": b"hello"},
                      exports={"d1": b"docx-bytes"})
    fs = DriveFS(drive, root_id="root", export_formats={GDOC: DOCX},
                 cache_dir=tmp_path / "cache")
    return fs, drive


def test_resolve_and_getattr(tmp_path):
    fs, _ = make_fs(tmp_path)
    root = fs.getattr("/")
    assert stat.S_ISDIR(root["st_mode"])
    notes = fs.getattr("/notes.txt")
    assert stat.S_ISREG(notes["st_mode"])
    assert notes["st_size"] == 5
    photos = fs.getattr("/Photos")
    assert stat.S_ISDIR(photos["st_mode"])
    # a folder shortcut resolves to its target but keeps the shortcut name
    assert fs.resolve("/Team")["id"] == "f2"
    with pytest.raises(OSError) as err:
        fs.getattr("/missing")
    assert err.value.errno == errno.ENOENT


def test_readdir_skips_unexportable_native_docs(tmp_path):
    fs, _ = make_fs(tmp_path)
    names = fs.readdir("/")
    assert names[:2] == [".", ".."]
    assert "notes.txt" in names
    assert "Photos" in names
    assert "Report.docx" in names          # exported with its extension
    assert "Team" in names
    assert "form" not in names             # Google Form: no export target


def test_duplicate_names_are_made_unique(tmp_path):
    tree = {"root": [
        {"id": "1", "name": "same.txt", "mimeType": "text/plain", "size": "1"},
        {"id": "2", "name": "same.txt", "mimeType": "text/plain", "size": "1"},
        {"id": "3", "name": "same.txt", "mimeType": "text/plain", "size": "1"},
    ]}
    fs = DriveFS(FakeDrive(tree), cache_dir=tmp_path / "cache")
    assert fs.readdir("/")[2:] == ["same.txt", "same (2).txt", "same (3).txt"]
    assert fs.resolve("/same (2).txt")["id"] == "2"


def test_open_read_release_downloads_and_serves_bytes(tmp_path):
    fs, drive = make_fs(tmp_path)
    fh = fs.open("/notes.txt", os.O_RDONLY)
    assert fs.read(fh=fh, path="/notes.txt", size=2, offset=1) == b"el"
    fs.release("/notes.txt", fh)
    assert ("download", "a1") in drive.calls


def test_native_doc_is_exported_on_open(tmp_path):
    fs, drive = make_fs(tmp_path)
    fh = fs.open("/Report.docx", os.O_RDONLY)
    assert fs.read(fh=fh, path="/Report.docx", size=100, offset=0) == \
        b"docx-bytes"
    fs.release("/Report.docx", fh)
    assert ("export", "d1", DOCX[0]) in drive.calls
    assert fs.getattr("/Report.docx")["st_size"] == len(b"docx-bytes")


def test_writes_and_open_for_write_are_refused(tmp_path):
    fs, _ = make_fs(tmp_path)
    for call in (lambda: fs.open("/notes.txt", os.O_WRONLY),
                 lambda: fs.unlink("/notes.txt"),
                 lambda: fs.mkdir("/new", 0o755),
                 lambda: fs.access("/notes.txt", os.W_OK)):
        with pytest.raises(OSError) as err:
            call()
        assert err.value.errno == errno.EROFS


def test_listing_is_cached_between_calls(tmp_path):
    fs, drive = make_fs(tmp_path)
    fs.readdir("/")
    fs.readdir("/")
    assert [c for c in drive.calls if c[0] == "list"] == [("list", "root")]


def test_mount_without_fusepy_is_a_clear_error(tmp_path):
    try:
        import fuse  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("fusepy is installed; the missing-dependency path is n/a")
    _fs, drive = make_fs(tmp_path)
    with pytest.raises(FuseUnavailable, match="fusepy"):
        mount(drive, tmp_path)
