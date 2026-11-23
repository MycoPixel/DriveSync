"""Google Drive REST v3 wrapper used by the sync engine.

Only the operations the sync engine needs: recursive listing, upload,
download, mkdir, rename/move, trash. Talks to the public REST endpoints
with `requests`; no heavyweight client library required.
"""

import mimetypes
import os
import random
import secrets
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
RETRY_STATUSES = {429, 500, 502, 503, 504}
# A POST that returned 500 may already have created the file (Drive does not
# deduplicate names), so only replay creates on definite gateway/throttle
# failures. Reads and idempotent mutations may replay on 500 too.
POST_NO_REPLAY = {500}


def _retryable(method, status):
    return (status in RETRY_STATUSES and
            not (method == "POST" and status in POST_NO_REPLAY))


LIST_WORKERS = 4              # parallel folder listings on the first scan
RESUMABLE_CHUNK = 8 << 20    # resumable upload chunk (256 KiB multiples)
PART_SUFFIX = ".drivesync.part"   # download progress kept beside the destination
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"


def safe_name(name):
    """One Drive item name that cannot escape the sync root.

    Drive forbids '/' in names, but defensive code still guards against
    crafted or legacy names like '.' and '..': joining those with a local
    root would write outside the pair. Unusable names become underscores.
    """
    name = str(name or "").replace("\x00", "")
    name = name.replace("/", "_")
    if name in ("", ".", ".."):
        return "_" * max(1, len(name))
    return name


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


def owner_name(item):
    """Display name of a Drive row's owner ('' when absent or odd).

    The listing fields include `owners` (displayName preferred, emailAddress
    as fallback); shared-with-me rows and some Workspace items have none, so
    the UIs show a dash rather than pretending there is one. Shared by the
    web dashboard and the desktop Drive browser.
    """
    owners = item.get("owners") or []
    if not isinstance(owners, list):
        return ""
    first = owners[0] if owners and isinstance(owners[0], dict) else None
    if not first:
        return ""
    return str(first.get("displayName") or first.get("emailAddress") or "")


# Shared file-type classification for the Drive browsers. The tokens are
# stable identifiers (never shown to users directly); each UI maps them to
# its own translated label and uses them for the type filter dropdown.
ARCHIVE_MIMES = {"application/zip", "application/x-zip-compressed",
                 "application/x-tar", "application/x-rar-compressed",
                 "application/x-7z-compressed", "application/gzip",
                 "application/x-gzip", "application/x-bzip2"}
NATIVE_KINDS = {
    "document": "document", "spreadsheet": "spreadsheet",
    "presentation": "presentation", "drawing": "drawing",
    "form": "form", "map": "map", "site": "site",
}


def file_category(item):
    """Drive listing row -> one stable category token.

    Folder shortcuts classify as 'folder' (the browsers descend into them);
    unresolved shortcuts (target missing/private) classify as 'shortcut' so
    they are still listed instead of disappearing. Everything else buckets
    by mimeType: google-native kinds by suffix, regular files by prefix.
    """
    mime = (item.get("mimeType") or "").lower()
    details = item.get("shortcutDetails") or {}
    if details.get("targetMimeType"):
        mime = str(details["targetMimeType"]).lower()
    if mime == FOLDER_MIME:
        return "folder"
    if mime == SHORTCUT_MIME:
        return "shortcut"
    if mime.startswith("application/vnd.google-apps."):
        suffix = mime.rsplit("vnd.google-apps.", 1)[-1]
        if suffix in NATIVE_KINDS:
            return NATIVE_KINDS[suffix]
        return "doc"                       # third-party or unknown native kind
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("text/") or mime.endswith("+xml") or \
            mime.endswith("/json"):
        return "text"
    if mime == "application/pdf":
        return "pdf"
    if mime in ARCHIVE_MIMES:
        return "archive"
    return "other"


# Virtual folders the browsers show at the top level; they have no real
# Drive id, so breadcrumbs and "sync this folder" treat them specially.
VIRTUAL_ROOTS = {
    "root": "My Drive",
    "shared": "Shared with me",
    "drives": "Shared drives",
}


def is_virtual_root(folder_id):
    """True for browser-only levels that are not real Drive folders.

    'root' is deliberately excluded: it *is* a valid sync target (My Drive
    itself). 'shared' and 'drives' are lists, not folders.
    """
    return folder_id in VIRTUAL_ROOTS and folder_id != "root"

# Breadcrumb walks stop well before the API would: a parent chain longer
# than this is a corrupted tree or a crafted loop, never a real hierarchy.
MAX_BREADCRUMB_DEPTH = 32


def breadcrumbs(drive, folder_id):
    """Root-first chain of {id, name} entries from My Drive to folder_id.

    One metadata call per ancestor (the Files API has no reverse-parents
    listing), so this stays O(depth) — fine for real hierarchies, and the
    depth cap turns a crafted parent loop into a short, honest chain. The
    virtual roots never hit the API. An unreachable ancestor truncates the
    chain instead of failing the whole listing: showing 'My Drive / … /
    This folder' beats showing nothing.
    """
    label = VIRTUAL_ROOTS.get(folder_id)
    if label:
        return [{"id": folder_id, "name": label}]
    chain, seen, current = [], set(), folder_id
    in_shared_drive = False
    while current and current not in seen and len(chain) < MAX_BREADCRUMB_DEPTH:
        seen.add(current)
        try:
            meta = drive.get(current, fields="id,name,parents,driveId")
        except DriveError:
            if chain:
                break              # an ancestor is gone: show what we have
            raise                  # the requested folder itself is broken
        if meta.get("driveId"):
            in_shared_drive = True
        chain.append({"id": meta.get("id", current),
                      "name": meta.get("name") or current})
        parents = [p for p in (meta.get("parents") or [])
                   if p and p != "root"]
        if not parents:
            break                          # reached the actual My Drive root
        current = parents[0]
    chain.reverse()
    if in_shared_drive:
        # keep the virtual 'Shared drives' level in the chain so the
        # breadcrumb can walk back to the drive list
        chain.insert(0, {"id": "drives", "name": VIRTUAL_ROOTS["drives"]})
    return chain


def folders_for_parent(drive, parent):
    """Subfolders of `parent` for folder pickers.

    parent 'shared' lists the account's 'Shared with me' folders; any
    other value is a folder id ('root' = My Drive). Returns pickable
    rows only (folders and folder shortcuts).
    """
    if parent == "shared":
        return drive.list_shared_folders()
    if parent == "drives":
        return [{"id": d["id"], "name": d["name"], "mimeType": FOLDER_MIME}
                for d in drive.list_drives()]
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
    joined = " ".join(reasons).lower()
    if "ratelimit" in joined or "rate limit" in joined:
        return ("  -> Google rate-limited this account (per-user or per-project "
                "quota). DriveSync backs off automatically; if it keeps "
                "happening, lower transfer_concurrency or raise the sync "
                "interval.")
    if "storagequota" in joined or "quotaexceeded" in joined:
        return ("  -> the account's Drive storage is full (or its quota is "
                "exhausted). Free space in Drive or Google One and retry.")
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

    def __init__(self, token_provider, page_size=1000, limiter=None):
        """token_provider: callable returning a bearer token string.

        limiter: optional limits.TokenBucket capping transfer speed on
        uploads and downloads combined (None = unlimited). heartbeat: an
        optional callable the transfer chunk loops call as bytes move —
        the systemd watchdog uses it to tell a slow transfer from a hung
        worker.
        """
        self.token_provider = token_provider
        self.page_size = page_size
        self.limiter = limiter
        self.heartbeat = None
        self._session = requests.Session()

    def _ping(self):
        """Liveness hook for the systemd watchdog (never fails a transfer)."""
        hb = self.heartbeat
        if hb:
            try:
                hb()
            except Exception:
                pass

    def _headers(self, extra=None):
        h = {"Authorization": f"Bearer {self.token_provider()}"}
        if extra:
            h.update(extra)
        return h

    @staticmethod
    def _rewind_body(body):
        """Reset a streamed request body so a retry sends the whole content."""
        if body is None:
            return
        rewind = getattr(body, "rewind", None)
        if callable(rewind):
            rewind()
        elif hasattr(body, "seek") and body.seekable():
            body.seek(0)

    @staticmethod
    def _retry_delay(resp, attempt):
        """Exponential backoff with jitter; honour Google's Retry-After."""
        headers = getattr(resp, "headers", None) or {}
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), 30.0)
            except (TypeError, ValueError):
                pass
        return min(0.5 * (2 ** attempt) + random.uniform(0, 0.25), 30.0)

    def _request(self, method, url, ok=(200, 201), **kw):
        # Every call to Google must carry the Authorization header — resolved
        # here centrally so no endpoint can silently go out unauthenticated.
        extra = dict(kw.pop("headers", None) or {})
        extra.pop("Authorization", None)
        params = kw.get("params")
        if "/files" in url:
            # Shared Drives are invisible unless every call opts in — even
            # mutations (create/trash) that pass no query parameters.
            if not isinstance(params, dict):
                params = {}
                kw["params"] = params
            params.setdefault("supportsAllDrives", "true")
            if method == "GET" and params.get("q"):
                params.setdefault("includeItemsFromAllDrives", "true")
        max_tries = 4
        resp = None
        for attempt in range(max_tries):
            kw["headers"] = self._headers(extra)
            try:
                resp = self._session.request(method, url, timeout=120, **kw)
            except (requests.ConnectionError, requests.Timeout) as exc:
                # only replay idempotent reads on transport failures: a
                # create/mutation may already have taken effect server-side
                if attempt >= max_tries - 1 or method not in ("GET", "HEAD"):
                    raise DriveError(f"{method} {url} failed: {exc}") from exc
                time.sleep(self._retry_delay(None, attempt))
                continue
            if resp.status_code == 401 and attempt == 0:
                # token may have just expired mid-flight: refresh once and
                # replay; streamed bodies were consumed, so rewind first
                self._rewind_body(kw.get("data"))
                continue
            if _retryable(method, resp.status_code) and attempt < max_tries - 1:
                time.sleep(self._retry_delay(resp, attempt))
                self._rewind_body(kw.get("data"))
                continue
            break
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
                                   "modifiedTime,parents,trashed,driveId"):
        resp = self._request("GET", f"{API}/files/{file_id}",
                             params={"fields": fields})
        return resp.json()

    # ---- listing --------------------------------------------------
    def list_children(self, folder_id):
        """Direct children of a folder (non-trashed), paged.

        `owners` feeds the Drive browsers' Owner column (engine ignores it).
        """
        out, page_token = [], None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": "nextPageToken, files(id,name,mimeType,md5Checksum,"
                          "version,size,modifiedTime,parents,driveId,"
                          "shortcutDetails,owners(displayName,emailAddress))",
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

    def list_drives(self, limit=100):
        """Shared drives (formerly Team Drives) this account belongs to.

        Each drive is itself a folder root: its id can be used as a parent
        for listing, pair creation and uploads, exactly like 'root'.
        """
        out, page_token = [], None
        while True:
            params = {
                "pageSize": min(100, limit - len(out)),
                "fields": "nextPageToken,drives(id,name)",
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._request("GET", f"{API}/drives", params=params).json()
            out.extend(data.get("drives", []))
            page_token = data.get("nextPageToken")
            if not page_token or len(out) >= limit:
                return out[:limit]

    def list_shared_folders(self):
        """Folders in 'Shared with me' — shared by other accounts, paged."""
        out, page_token = [], None
        while True:
            params = {
                "q": f"sharedWithMe=true and mimeType='{FOLDER_MIME}' "
                     "and trashed=false",
                "fields": "nextPageToken, files(id,name,driveId)",
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

    def search(self, query, limit=50, folders_only=False):
        """Drive-wide search: items whose name contains `query`.

        One API call with `name contains '...'` — how people actually look
        for things, instead of walking the tree folder by folder. Returns
        files.list metadata rows, folders first, capped at `limit`.
        folders_only=True narrows the query to folders (pair pickers).
        """
        term = (query or "").replace("\\", "").replace("'", "").strip()
        if not term:
            return []
        q = f"name contains '{term}' and trashed=false"
        if folders_only:
            q += f" and mimeType='{FOLDER_MIME}'"
        out, page_token = [], None
        while len(out) < limit:
            params = {
                "q": q,
                "fields": "nextPageToken, files(id,name,mimeType,md5Checksum,"
                          "version,size,modifiedTime,driveId,"
                          "shortcutDetails,owners(displayName,emailAddress))",
                "pageSize": min(self.page_size, limit - len(out)),
                "orderBy": "name_natural",
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._request("GET", f"{API}/files", params=params).json()
            out.extend(data.get("files", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        out.sort(key=lambda f: (0 if f.get("mimeType") == FOLDER_MIME else 1,
                                f.get("name", "").lower()))
        return out[:limit]

    def list_tree(self, folder_id):
        """Recursive listing under folder_id.

        Returns {rel_path: item_dict} with rel '' for the root itself.
        Google-native docs are included (kind gdoc via mimeType).
        """
        items = {"": {"id": folder_id, "name": "", "mimeType": FOLDER_MIME}}
        # Breadth-first with a small pool: a first sync of a tree with
        # thousands of folders otherwise spends minutes in serial HTTPS calls.
        pending = [("", folder_id)]
        with ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
            while pending:
                futures = {pool.submit(self.list_children, fid):
                           (prefix, fid) for prefix, fid in pending}
                pending = []
                for future in as_completed(futures):
                    prefix, _ = futures[future]
                    for f in future.result():
                        rel = (f"{prefix}/{safe_name(f['name'])}"
                               if prefix else safe_name(f["name"]))
                        items[rel] = f
                        if f.get("mimeType") == FOLDER_MIME:
                            pending.append((rel, f["id"]))
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
            body, boundary = _multipart(meta, fh, mime, self.limiter,
                                        ping=self._ping)
            resp = self._request(
                "POST", f"{UPLOAD_API}/files",
                params={"uploadType": "multipart",
                        "fields": "id,name,mimeType,version,md5Checksum,"
                                  "size,modifiedTime"},
                headers={"Content-Type":
                         f"multipart/related; boundary={boundary}"},
                data=body)
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
                data=_Throttled(fh, self.limiter, ping=self._ping)
                if self.limiter else _PingBody(fh, self._ping))
        return resp.json()

    # ---- resumable transfers (Phase 3.2) ---------------------------
    def upload_resumable(self, local_path, name, parent_id, session=None,
                         on_session=None, chunk_bytes=None):
        """Create a new file through Google's resumable-upload protocol.

        Instead of one multipart POST, a session URI is initiated with the
        metadata and the body is PUT in chunks; each 308 response reports
        how many bytes the server actually holds, so a broken chunk only
        re-sends what is left.

        session: a URI saved from an earlier attempt — its server-side
        offset is queried first and the transfer continues from there
        (a dead session simply starts over). on_session(uri) fires as
        soon as the session exists so the caller can persist it between
        passes and retries.
        """
        mime = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
        size = os.path.getsize(local_path)
        # an explicit chunk_bytes is honored as given (tests, tuning);
        # otherwise Google's recommended 8 MiB default applies
        chunk = int(chunk_bytes) if chunk_bytes else RESUMABLE_CHUNK
        meta = {"name": name, "parents": [parent_id]}

        offset = 0
        if session:
            kind, val = self._session_status(session, size)
            if kind == "done":
                return val          # crash after the last chunk, before ack
            if kind == "offset":
                offset = val
            else:
                session = None      # server lost the session: start over

        if not session:
            resp = self._request(
                "POST", f"{UPLOAD_API}/files",
                params={"uploadType": "resumable",
                        "fields": "id,name,mimeType,version,md5Checksum,"
                                  "size,modifiedTime"},
                json=meta, headers={"X-Upload-Content-Type": mime})
            session = resp.headers["Location"]
            if on_session:
                try:
                    on_session(session)
                except Exception:
                    pass  # state persistence must not fail the transfer
        return self._send_session_chunks(
            session, local_path, size, offset, chunk)

    def update_content_resumable(self, file_id, local_path, session=None,
                                 on_session=None, chunk_bytes=None):
        """Replace file content through a resumable session (PATCH media)."""
        mime = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
        size = os.path.getsize(local_path)
        chunk = int(chunk_bytes) if chunk_bytes else RESUMABLE_CHUNK

        offset = 0
        if session:
            kind, val = self._session_status(session, size)
            if kind == "done":
                return val
            if kind == "offset":
                offset = val
            else:
                session = None

        if not session:
            resp = self._request(
                "PATCH", f"{UPLOAD_API}/files/{file_id}",
                params={"uploadType": "resumable",
                        "fields": "id,version,md5Checksum,size,modifiedTime"},
                json={}, headers={"X-Upload-Content-Type": mime})
            session = resp.headers["Location"]
            if on_session:
                try:
                    on_session(session)
                except Exception:
                    pass
        return self._send_session_chunks(
            session, local_path, size, offset, chunk)

    def _session_status(self, session, size):
        """Ask the server how much of a session it already holds.

        Returns ("done", metadata_json) when the upload finished (a pass
        can crash between the final chunk and the local acknowledgement),
        ("offset", n) when it is partially complete, ("dead", None) when
        the session is gone and the transfer must start over.
        """
        try:
            resp = self._session.put(
                session, headers=self._headers(
                    {"Content-Range": f"bytes */{size}"}), timeout=120)
        except (requests.ConnectionError, requests.Timeout):
            return ("dead", None)
        if resp.status_code in (200, 201):
            return ("done", resp.json())
        if resp.status_code == 308:
            rng = resp.headers.get("Range", "")
            return ("offset", int(rng.rsplit("-", 1)[1]) + 1 if rng else 0)
        return ("dead", None)

    def _send_session_chunks(self, session, local_path, size, offset, chunk):
        """PUT the remaining bytes of a resumable session, chunk by chunk.

        A failed chunk re-queries the server-side offset and continues —
        at worst one chunk is re-sent, never the whole file. Session PUTs
        are raw requests: the session URI is the credential.
        """
        with open(local_path, "rb") as fh:
            fh.seek(offset)
            tries = 0
            while offset < size:
                body = fh.read(chunk)
                end = offset + len(body)
                cr_last = end >= size
                content_range =                     f"bytes {offset}-{end - 1}/{size}" if cr_last else \
                    f"bytes {offset}-{end - 1}/*"
                if self.limiter:
                    self.limiter.consume(len(body))
                self._ping()
                try:
                    resp = self._session.put(
                        session, data=body,
                        headers=self._headers(
                            {"Content-Range": content_range}), timeout=600)
                except (requests.ConnectionError, requests.Timeout):
                    resp = None
                if resp is not None and resp.status_code in (200, 201):
                    return resp.json()
                if resp is not None and resp.status_code == 308:
                    rng = resp.headers.get("Range", "")
                    offset = int(rng.rsplit("-", 1)[1]) + 1 if rng else end
                    fh.seek(offset)
                    continue
                # transport failure or 5xx: find what the server holds
                tries += 1
                if tries >= 4:
                    raise DriveError(
                        f"PUT {session.split('?')[0]} failed after "
                        f"{offset}/{size} bytes (session kept — the next "
                        "attempt resumes)")
                time.sleep(self._retry_delay(resp, tries - 1))
                kind, val = self._session_status(session, size)
                if kind == "done":
                    return val
                if kind == "offset":
                    offset = val
                else:
                    raise DriveError(
                        f"PUT {session.split('?')[0]} lost its session "
                        f"at {offset}/{size} bytes — restart the transfer")
                fh.seek(offset)
        # offset == size without a final 200: the server has everything but
        # never confirmed — query once more to fetch the metadata
        kind, val = self._session_status(session, size)
        if kind == "done":
            return val
        raise DriveError(f"PUT {session.split('?')[0]} did not confirm "
                         f"the completed upload")

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

    def untrash(self, file_id):
        """Take a file out of the Drive trash (keeps its original parents).

        Drive restores the file where it lived; if that folder was deleted
        too the API may refuse or reparent — callers surface the error.
        """
        return self._request("PATCH", f"{API}/files/{file_id}",
                             json={"trashed": False}).json()

    def list_trashed(self, limit=100):
        """Files currently in the account's Drive trash, newest first.

        Drive trash is account-wide (not per sync pair), so the recovery UI
        labels it that way; `trashedTime` and `parents` are included so the
        UI can show when something was deleted and where it would return.
        """
        out, page_token = [], None
        while True:
            params = {
                "q": "trashed=true",
                "fields": "nextPageToken, files(id,name,mimeType,size,"
                          "modifiedTime,trashedTime,parents)",
                "pageSize": min(1000, max(1, int(limit))),
                "orderBy": "modifiedTime desc",
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._request("GET", f"{API}/files", params=params).json()
            out.extend(data.get("files", []))
            page_token = data.get("nextPageToken")
            if not page_token or len(out) >= limit:
                break
        return out[:limit]

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
        """Stream to dest_path via a .part file + atomic replace, resuming.

        The bytes land in dest_path.part — a deterministic name, so a
        broken transfer leaves its progress on disk. The next attempt
        (same call or a later pass) resumes with a Range request from the
        part size; a server that answers 200 anyway means the part cannot
        be trusted and the transfer starts over. On success the part is
        atomically renamed into place; on failure it stays — that IS the
        resume state.
        """
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        part = f"{dest_path}{PART_SUFFIX}"
        params = dict(params)
        params.setdefault("supportsAllDrives", "true")
        max_tries = 4
        for attempt in range(max_tries):
            offset = os.path.getsize(part) if os.path.exists(part) else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                resp = self._session.get(url, params=params,
                                         headers=self._headers(headers),
                                         stream=True, timeout=600)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= max_tries - 1:
                    raise DriveError(f"GET {url} failed: {exc}") from exc
                time.sleep(self._retry_delay(None, attempt))
                continue          # the part keeps whatever arrived
            if resp.status_code == 401 and attempt == 0:
                # token expired mid-flight: refresh and retry once
                resp.close()
                continue
            if (_retryable("GET", resp.status_code)
                    and attempt < max_tries - 1):
                delay = self._retry_delay(resp, attempt)
                resp.close()
                time.sleep(delay)
                continue
            if resp.status_code == 416:
                # the part already holds every byte (a crash landed between
                # the last write and the rename): rename it and finish
                resp.close()
                os.replace(part, dest_path)
                return
            if resp.status_code >= 400:
                text, hint = resp.text[:300], _explain(resp)
                resp.close()
                raise DriveError(f"GET {url} -> {resp.status_code}: "
                                 f"{text}{hint}")
            if resp.status_code == 200 and offset:
                # server ignored the Range (fresh content, or an endpoint
                # that does not do ranges): the part cannot be trusted
                offset = 0
            try:
                with resp, open(part, "ab" if offset else "wb") as out:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        if self.limiter:
                            self.limiter.consume(len(chunk))
                        self._ping()
                        out.write(chunk)
            except (requests.ConnectionError,
                    requests.exceptions.ChunkedEncodingError) as exc:
                if attempt >= max_tries - 1:
                    raise DriveError(
                        f"GET {url} broke mid-stream: {exc}") from exc
                time.sleep(self._retry_delay(None, attempt))
                continue          # resume from the part on the next attempt
            break
        else:
            raise DriveError(f"GET {url} failed: retries exhausted")
        os.replace(part, dest_path)

    def set_local_mtime(self, dest_path, remote_modified):
        """Best effort: mirror the Drive modifiedTime onto the local file."""
        try:
            dt = datetime.fromisoformat(remote_modified.replace("Z", "+00:00"))
            ts = dt.timestamp()
            os.utime(dest_path, (ts, ts))
        except (ValueError, TypeError, OSError):
            pass


def _multipart(meta, fh, mime, limiter=None, ping=None):
    """Build a multipart/related body (metadata + media) for uploads.

    The boundary is random per call: a fixed one could theoretically appear
    inside the file's bytes and truncate the upload at the wrong place.
    Returns (streaming body, boundary) so the caller can set Content-Type.
    """
    boundary = f"drivesync{secrets.token_hex(16)}"
    meta_bytes = (json_dumps(meta)).encode("utf-8")
    head = (f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            ).encode("utf-8") + meta_bytes + \
           (f"\r\n--{boundary}\r\n"
            f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
    tail = f"\r\n--{boundary}--".encode("utf-8")
    return _GeneratorBody(head, fh, tail, limiter, ping), boundary


class _GeneratorBody:
    """Streaming iterable body so large files are not read fully into RAM."""

    def __init__(self, head, fh, tail, limiter=None, ping=None):
        self.head, self.fh, self.tail = head, fh, tail
        self.limiter = limiter
        self.ping = ping

    def __iter__(self):
        yield self.head
        while True:
            chunk = self.fh.read(1 << 16)
            if not chunk:
                break
            if self.limiter:
                self.limiter.consume(len(chunk))
            if self.ping:
                self.ping()
            yield chunk
        yield self.tail

    def rewind(self):
        """Replay from the start after a failed attempt consumed the file."""
        self.fh.seek(0)


class _Throttled:
    """read()-only wrapper that bills every byte to the limiter, so plain
    file-object upload bodies (requests streams them) throttle too."""

    def __init__(self, fh, limiter, ping=None):
        self.fh = fh
        self.limiter = limiter
        self.ping = ping

    def read(self, n=-1):
        data = self.fh.read(n)
        if data and self.limiter:
            self.limiter.consume(len(data))
        if data and self.ping:
            self.ping()
        return data

    def rewind(self):
        """Replay from the start after a failed attempt consumed the file."""
        self.fh.seek(0)


class _PingBody:
    """read()-only wrapper that pings the watchdog as upload bytes stream
    (used when no bandwidth limiter is configured, so plain file bodies
    report liveness too)."""

    def __init__(self, fh, ping=None):
        self.fh = fh
        self.ping = ping

    def read(self, n=-1):
        data = self.fh.read(n)
        if data and self.ping:
            self.ping()
        return data

    def rewind(self):
        """Replay from the start after a failed attempt consumed the file."""
        self.fh.seek(0)


def json_dumps(obj):
    import json
    return json.dumps(obj)
