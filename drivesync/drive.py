"""Google Drive REST v3 wrapper used by the sync engine.

Only the operations the sync engine needs: recursive listing, upload,
download, mkdir, rename/move, trash. Talks to the public REST endpoints
with `requests`; no heavyweight client library required.
"""

import mimetypes
import os
import tempfile
from datetime import datetime, timezone

import requests

API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"


def resolve_folder_entry(item):
    """Picker-ready {id,name} for a Drive listing row.

    Real folders pass through; shortcuts to folders resolve to their real
    target id (so sync pairs bind to the folder itself, not the shortcut);
    files, docs and shortcut-to-file rows return None. Shared by the web
    dashboard and the desktop GUI folder picker.
    """
    if item.get("mimeType") == FOLDER_MIME:
        return {"id": item["id"], "name": item["name"]}
    if item.get("mimeType") == SHORTCUT_MIME:
        details = item.get("shortcutDetails") or {}
        if details.get("targetMimeType") == FOLDER_MIME:
            return {"id": details["targetId"], "name": item["name"]}
    return None


def folders_for_parent(drive, parent):
    """Subfolders of `parent` for folder pickers.

    parent 'shared' lists the account's 'Shared with me' folders; any
    other value is a folder id ('root' = My Drive). Returns pickable
    rows only (folders and folder shortcuts).
    """
    if parent == "shared":
        return drive.list_shared_folders()
    children = drive.list_children(parent)
    return [f for f in (resolve_folder_entry(c) for c in children) if f]

# Google-native files have no downloadable bytes; Drive converts them on the
# fly via files/{id}/export. Default target per native type: Office formats
# a desktop can open directly.
GDOC_EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx"),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}

# Config shorthand: export_formats = {"document": "pdf", ...}
EXPORT_KINDS = {
    "document": "application/vnd.google-apps.document",
    "spreadsheet": "application/vnd.google-apps.spreadsheet",
    "presentation": "application/vnd.google-apps.presentation",
    "drawing": "application/vnd.google-apps.drawing",
}
EXPORT_ALIASES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "odt": "application/vnd.oasis.opendocument.text",
    "rtf": "application/rtf",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "epub": "application/epub+zip",
    "html": "application/zip",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "png": "image/png",
    "svg": "image/svg+xml",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
}


def resolve_export_formats(cfg):
    """Config -> {native mime: (export mime, file extension)}.

    Honors convert_google_docs (off -> {}, docs stay record-only) and
    per-kind overrides in export_formats using the short names above;
    unknown names fall back to the Office default for that kind.
    """
    if not cfg.get("convert_google_docs", True):
        return {}
    overrides = cfg.get("export_formats") or {}
    if not isinstance(overrides, dict):
        overrides = {}
    out = {}
    for kind, native in EXPORT_KINDS.items():
        short = str(overrides.get(kind) or "").strip().lstrip(".").lower()
        mime = EXPORT_ALIASES.get(short)
        if mime:
            out[native] = (mime, f".{short}")
        else:
            out[native] = GDOC_EXPORTS[native]
    return out


class DriveError(Exception):
    pass


def _explain(resp):
    """Append an actionable hint to Google's often-cryptic API error text.

    Google wraps the decisive info in error.details (ErrorInfo: reason plus
    metadata with the service and the project that must have it enabled),
    which the 300-char body preview cuts off — so parse it out here.
    """
    if resp.status_code == 401:
        return ("  -> the stored access credential was rejected even after an "
                "automatic refresh attempt. Run 'drivesync auth' to re-authorize — "
                "typically needed after deleting or replacing the OAuth client in "
                "the Google Cloud console, or when a testing-mode app's refresh "
                "token has expired.")
    if resp.status_code != 403:
        return ""
    reasons, consumer, service = [], "", ""
    try:
        err = resp.json().get("error", {})
        reasons += [e["reason"] for e in err.get("errors", []) if e.get("reason")]
        for d in err.get("details", []):
            if d.get("reason"):
                reasons.append(d["reason"])
            md = d.get("metadata") or {}
            consumer = md.get("consumer") or consumer
            service = md.get("service") or service
    except Exception:
        pass
    project = consumer.rsplit("/", 1)[-1] if consumer.startswith("projects/") else ""
    url = (f"https://console.developers.google.com/apis/api/"
           f"{service or 'drive.googleapis.com'}/overview"
           f"?project={project or '<PROJECT_NUMBER>'}")
    if "SERVICE_DISABLED" in reasons:
        return ("  -> the Google Drive API is NOT enabled in project "
                f"{project or '<PROJECT_NUMBER>'} — the one that issued your OAuth "
                "client (its number is the leading number of your client ID, before "
                f"the dash). Enable it here: {url} then wait a few minutes and retry; "
                "enabling it in any other project does not count.")
    return ("  -> Google refused the Drive call (PERMISSION_DENIED). In order: (1) "
            "the Drive API must be enabled in the project that issued your OAuth "
            f"client — check {url} (enabling can take a few minutes to propagate); "
            "(2) your Google account must be a test user on the OAuth consent "
            "screen; (3) Workspace/school accounts can be blocked by admin policy "
            "— try a personal @gmail.com account to isolate.")


class Drive:
    """A Drive client bound to one account's access token."""

    def __init__(self, token_provider, page_size=1000):
        """token_provider: callable returning a bearer token string."""
        self.token_provider = token_provider
        self.page_size = page_size
        self._session = requests.Session()

    def _headers(self, extra=None):
        h = {"Authorization": f"Bearer {self.token_provider()}"}
        if extra:
            h.update(extra)
        return h

    def _request(self, method, url, ok=(200, 201), **kw):
        # Every call to Google must carry the Authorization header — resolved
        # here centrally so no endpoint can silently go out unauthenticated.
        extra = dict(kw.pop("headers", None) or {})
        extra.pop("Authorization", None)
        kw["headers"] = self._headers(extra)
        resp = self._session.request(method, url, timeout=120, **kw)
        if resp.status_code == 401:
            # token may have just expired mid-flight: refresh once and retry
            kw["headers"] = self._headers(extra)
            resp = self._session.request(method, url, timeout=120, **kw)
        if resp.status_code not in ok:
            raise DriveError(f"{method} {url} -> {resp.status_code}: "
                             f"{resp.text[:300]}{_explain(resp)}")
        return resp

    # ---- metadata -------------------------------------------------
    def about(self):
        resp = self._request("GET", f"{API}/about",
                             params={"fields": "user,storageQuota"})
        return resp.json()

    def get(self, file_id, fields="id,name,mimeType,version,md5Checksum,size,"
                                   "modifiedTime,parents,trashed"):
        resp = self._request("GET", f"{API}/files/{file_id}",
                             params={"fields": fields})
        return resp.json()

    # ---- listing --------------------------------------------------
    def list_children(self, folder_id):
        """Direct children of a folder (non-trashed), paged."""
        out, page_token = [], None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": "nextPageToken, files(id,name,mimeType,md5Checksum,"
                          "version,size,modifiedTime,parents,shortcutDetails)",
                "pageSize": self.page_size,
                "orderBy": "name_natural",
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._request("GET", f"{API}/files", params=params).json()
            out.extend(data.get("files", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return out

    def list_shared_folders(self):
        """Folders in 'Shared with me' — shared by other accounts, paged."""
        out, page_token = [], None
        while True:
            params = {
                "q": f"sharedWithMe=true and mimeType='{FOLDER_MIME}' "
                     "and trashed=false",
                "fields": "nextPageToken, files(id,name)",
                "pageSize": self.page_size,
                "orderBy": "name_natural",
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._request("GET", f"{API}/files", params=params).json()
            out.extend(data.get("files", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return out

    def list_tree(self, folder_id):
        """Recursive listing under folder_id.

        Returns {rel_path: item_dict} with rel '' for the root itself.
        Google-native docs are included (kind gdoc via mimeType).
        """
        items = {"": {"id": folder_id, "name": "", "mimeType": FOLDER_MIME}}

        def walk(folder_id, prefix):
            for f in self.list_children(folder_id):
                rel = f"{prefix}/{f['name']}" if prefix else f["name"]
                mime = f.get("mimeType", "")
                if mime == FOLDER_MIME:
                    items[rel] = f
                    walk(f["id"], rel)
                else:
                    items[rel] = f

        walk(folder_id, "")
        return items

    # ---- changes feed (incremental scans) --------------------------
    def changes_start_token(self):
        """Current changes-feed token — the 'now' marker for later passes."""
        resp = self._request("GET", f"{API}/changes/startPageToken")
        return resp.json()["startPageToken"]

    def changes_list(self, page_token):
        """All account-wide changes since page_token.

        Returns (items, new_token). Each item is
        {'fileId': …, 'removed': bool, 'file': {metadata}} — the file dict
        carries the *current* metadata, so deltas never need a follow-up
        metadata call. new_token only arrives once all pages were consumed;
        if the feed turns out incomplete, the OLD token is returned so the
        next pass re-reads (decide() is idempotent on no-op changes).
        """
        items, token = [], page_token
        fields = ("nextPageToken,newStartPageToken,"
                  "changes(fileId,removed,file(id,name,mimeType,md5Checksum,"
                  "version,size,modifiedTime,parents,trashed))")
        while True:
            params = {
                "pageToken": token,
                "pageSize": self.page_size,
                "fields": fields,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            data = self._request("GET", f"{API}/changes", params=params).json()
            items.extend(data.get("changes", []))
            if data.get("newStartPageToken"):
                return items, data["newStartPageToken"]
            token = data.get("nextPageToken")
            if not token:
                return items, page_token  # incomplete: re-read next pass

    # ---- mutations ------------------------------------------------
    def mkdir(self, name, parent_id):
        resp = self._request(
            "POST", f"{API}/files",
            json={"name": name, "mimeType": FOLDER_MIME,
                  "parents": [parent_id]})
        return resp.json()

    def upload(self, local_path, name, parent_id):
        """Create a new file via multipart upload. Returns file metadata."""
        mime = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
        meta = {"name": name, "parents": [parent_id]}
        with open(local_path, "rb") as fh:
            resp = self._request(
                "POST", f"{UPLOAD_API}/files",
                params={"uploadType": "multipart",
                        "fields": "id,name,mimeType,version,md5Checksum,"
                                  "size,modifiedTime"},
                headers={"Content-Type": MULTIPART_CT},
                data=_multipart(meta, fh, mime))
        return resp.json()

    def update_content(self, file_id, local_path):
        """Replace file content (media upload on an existing file id)."""
        mime = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
        with open(local_path, "rb") as fh:
            resp = self._request(
                "PATCH", f"{UPLOAD_API}/files/{file_id}",
                params={"uploadType": "media",
                        "fields": "id,version,md5Checksum,size,modifiedTime"},
                headers={"Content-Type": mime},
                data=fh)
        return resp.json()

    def rename(self, file_id, new_name, add_parent=None, remove_parent=None):
        """Rename and/or reparent a file or folder."""
        meta = {}
        if new_name is not None:
            meta["name"] = new_name
        params = {"fields": "id,name,version,md5Checksum,size,modifiedTime"}
        if add_parent:
            params["addParents"] = add_parent
        if remove_parent:
            params["removeParents"] = remove_parent
        resp = self._request("PATCH", f"{API}/files/{file_id}",
                             params=params, json=meta)
        return resp.json()

    def trash(self, file_id):
        return self._request("PATCH", f"{API}/files/{file_id}",
                             json={"trashed": True}).json()

    def download(self, file_id, dest_path):
        """Stream a regular file to dest_path via a temp file + atomic replace."""
        self._stream_to(f"{API}/files/{file_id}", {"alt": "media"}, dest_path)
        return dest_path

    def export(self, file_id, export_mime, dest_path):
        """Export a Google-native doc (Docs/Sheets/Slides) to dest_path."""
        self._stream_to(f"{API}/files/{file_id}/export",
                        {"mimeType": export_mime}, dest_path)
        return dest_path

    def _stream_to(self, url, params, dest_path):
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest_path) or ".",
                                   suffix=".drivesync")
        os.close(fd)
        try:
            with self._session.get(url, params=params, headers=self._headers(),
                                   stream=True, timeout=600) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as out:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        out.write(chunk)
            os.replace(tmp, dest_path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def set_local_mtime(self, dest_path, remote_modified):
        """Best effort: mirror the Drive modifiedTime onto the local file."""
        try:
            dt = datetime.fromisoformat(remote_modified.replace("Z", "+00:00"))
            ts = dt.timestamp()
            os.utime(dest_path, (ts, ts))
        except (ValueError, TypeError, OSError):
            pass


_MULTIPART_BOUNDARY = "drivesync2938jk"
MULTIPART_CT = f"multipart/related; boundary={_MULTIPART_BOUNDARY}"


def _multipart(meta, fh, mime):
    """Build a multipart/related body (metadata + media) for uploads."""
    boundary = _MULTIPART_BOUNDARY
    meta_bytes = (json_dumps(meta)).encode("utf-8")
    head = (f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            ).encode("utf-8") + meta_bytes + \
           (f"\r\n--{boundary}\r\n"
            f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
    tail = f"\r\n--{boundary}--".encode("utf-8")
    return _GeneratorBody(head, fh, tail)


class _GeneratorBody:
    """Streaming iterable body so large files are not read fully into RAM."""

    def __init__(self, head, fh, tail):
        self.head, self.fh, self.tail = head, fh, tail

    def __iter__(self):
        yield self.head
        while True:
            chunk = self.fh.read(1 << 16)
            if not chunk:
                break
            yield chunk
        yield self.tail


def json_dumps(obj):
    import json
    return json.dumps(obj)
