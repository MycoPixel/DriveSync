"""A fake Google Drive backed by a temp directory — for unit tests.

Implements the subset of drivesync.drive.Drive the engine uses:
list_tree, list_children, mkdir, upload, update_content, rename, trash,
download, about. Files live on disk in a "cloud" folder; metadata (ids,
versions, mtimes, trashed flags) is kept in memory.
"""

import hashlib
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

FOLDER_MIME = "application/vnd.google-apps.folder"


class MockDrive:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.files = {}          # id -> meta dict
        self.root_id = self._new_folder("My Drive")
        self._version_counter = {}

    # ---- helpers --------------------------------------------------
    def _new_id(self):
        return uuid.uuid4().hex[:28]

    def _now_iso(self):
        return datetime.now(timezone.utc).isoformat(timespec="seconds") \
            .replace("+00:00", "Z")

    def _new_folder(self, name, parent_id=None):
        fid = self._new_id()
        self.files[fid] = {
            "id": fid, "name": name, "mimeType": FOLDER_MIME,
            "parents": [parent_id] if parent_id else [],
            "version": "1", "modifiedTime": self._now_iso(),
            "trashed": False,
        }
        return fid

    def _touch(self, fid):
        self._version_counter[fid] = self._version_counter.get(fid, 1) + 1
        self.files[fid]["version"] = str(self._version_counter[fid])
        self.files[fid]["modifiedTime"] = self._now_iso()

    def _fs_path(self, fid):
        """Physical path for a file id (not folders — those are virtual)."""
        parts = []
        cur = fid
        while cur and self.files.get(cur, {}).get("parents"):
            parts.append(self.files[cur]["name"])
            cur = self.files[cur]["parents"][0]
        # 'My Drive' root folder has no parents; anything left is root
        return self.root / Path(*reversed(parts))

    # ---- Drive API subset -----------------------------------------
    def about(self):
        return {"user": {"displayName": "Mock User", "emailAddress": "mock@example.com"},
                "storageQuota": {"usage": "1000", "limit": "15000000"}}

    def list_children(self, folder_id):
        return [f for f in self.files.values()
                if f["parents"] and f["parents"][0] == folder_id
                and not f["trashed"]]

    def get(self, file_id, fields=None):
        return dict(self.files[file_id])

    def list_tree(self, folder_id):
        items = {"": {"id": folder_id, "name": "", "mimeType": FOLDER_MIME}}
        for f in self.files.values():
            if f["trashed"]:
                continue
            if f["id"] == folder_id:
                continue  # root placeholder already present
            # walk up to folder_id; skip items not under it
            chain, cur, under = [], f, True
            while cur["id"] != folder_id:
                if not cur.get("parents"):
                    under = False
                    break
                chain.append(cur["name"])
                cur = self.files[cur["parents"][0]]
            if not under:
                continue
            rel = "/".join(reversed(chain))
            items[rel] = {
                "id": f["id"], "name": f["name"], "mimeType": f["mimeType"],
                "version": f["version"], "modifiedTime": f["modifiedTime"],
                "parents": f["parents"],
            }
            if f["mimeType"] != FOLDER_MIME:
                path = self._fs_path(f["id"])
                if path.exists():
                    data = path.read_bytes()
                    items[rel]["md5Checksum"] = hashlib.md5(data).hexdigest()
                    items[rel]["size"] = str(len(data))
        return items

    def mkdir(self, name, parent_id):
        return dict(self.files[self._new_folder(name, parent_id)])

    def upload(self, local_path, name, parent_id):
        fid = self._new_id()
        dest_dir = self._dir_path(parent_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        shutil.copy2(local_path, dest)
        data = dest.read_bytes()
        self.files[fid] = {
            "id": fid, "name": name, "mimeType": "application/octet-stream",
            "parents": [parent_id], "version": "1",
            "modifiedTime": self._now_iso(), "trashed": False,
            "md5Checksum": hashlib.md5(data).hexdigest(),
            "size": str(len(data)),
        }
        return dict(self.files[fid])

    def update_content(self, file_id, local_path):
        dest = self._fs_path(file_id)
        shutil.copy2(local_path, dest)
        data = dest.read_bytes()
        self._touch(file_id)
        self.files[file_id]["md5Checksum"] = hashlib.md5(data).hexdigest()
        self.files[file_id]["size"] = str(len(data))
        return dict(self.files[file_id])

    def rename(self, file_id, new_name, add_parent=None, remove_parent=None):
        f = self.files[file_id]
        old_path = self._fs_path(file_id)
        if new_name:
            f["name"] = new_name
        if add_parent:
            # single-parent model: the new parent replaces the old one
            f["parents"] = [add_parent] + \
                [p for p in f["parents"] if p not in (remove_parent, add_parent)]
        elif remove_parent and remove_parent in f["parents"] and len(f["parents"]) > 1:
            f["parents"].remove(remove_parent)
        # keep the physical file in step with the metadata change
        new_path = self._fs_path(file_id)
        if old_path != new_path and old_path.exists() and \
                f["mimeType"] != FOLDER_MIME:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_path, new_path)
        self._touch(file_id)
        return dict(f)

    def trash(self, file_id):
        self.files[file_id]["trashed"] = True
        self._touch(file_id)
        return dict(self.files[file_id])

    def download(self, file_id, dest_path):
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._fs_path(file_id), dest_path)
        return dest_path

    def export(self, file_id, export_mime, dest_path):
        return self.download(file_id, dest_path)

    def set_local_mtime(self, dest_path, remote_modified):
        pass

    # ---- extra test helpers ----------------------------------------
    def _dir_path(self, folder_id):
        if folder_id == self.root_id:
            return self.root
        parts, cur = [], folder_id
        while cur != self.root_id:
            parts.append(self.files[cur]["name"])
            cur = self.files[cur]["parents"][0]
        return self.root.joinpath(*reversed(parts))

    def put_remote_file(self, rel_path, content):
        """Test helper: place a file directly in the fake Drive."""
        rel = Path(rel_path)
        parent_rel = rel.parent
        parent_id = self.root_id
        if str(parent_rel) != ".":
            for part in parent_rel.parts:
                existing = [f for f in self.list_children(parent_id)
                            if f["name"] == part]
                if existing:
                    parent_id = existing[0]["id"]
                else:
                    parent_id = self._new_folder(part, parent_id)
        dest = self._dir_path(parent_id) / rel.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content if isinstance(content, bytes) else content.encode())
        data = dest.read_bytes()
        fid = self._new_id()
        self.files[fid] = {
            "id": fid, "name": rel.name, "mimeType": "text/plain",
            "parents": [parent_id], "version": "1",
            "modifiedTime": self._now_iso(), "trashed": False,
            "md5Checksum": hashlib.md5(data).hexdigest(),
            "size": str(len(data)),
        }
        return fid

    def put_remote_folder(self, rel_path):
        parent_id = self.root_id
        for part in Path(rel_path).parts:
            existing = [f for f in self.list_children(parent_id) if f["name"] == part]
            parent_id = existing[0]["id"] if existing else self._new_folder(part, parent_id)
        return parent_id

    def read_remote(self, rel_path):
        fid = self._find_by_rel(rel_path)
        return self._fs_path(fid).read_bytes()

    def _find_by_rel(self, rel_path):
        for f in self.files.values():
            if f["trashed"] or f["mimeType"] == FOLDER_MIME:
                continue
            path = self._fs_path(f["id"])
            try:
                cur_rel = path.relative_to(self.root).as_posix()
            except ValueError:
                continue
            if cur_rel == rel_path:
                return f["id"]
        raise KeyError(rel_path)
