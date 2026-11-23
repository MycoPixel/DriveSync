"""Optional read-only FUSE mount of a Drive folder.

This is an *extra*: the GUI Drive browser already works without it. Install
the optional dependency and mount a folder (My Drive, a Shared Drive, or a
shared folder) as a read-only filesystem::

    pip install fusepy           # also needs libfuse on the system
    drivesync mount ~/Drive --remote-id <folder-id>

Design notes
------------
- **Read-only by construction.** Every mutation returns ``EROFS``; the mount
  can never write to Drive, so it cannot race the sync engine.
- **Listings are cached briefly** (``LIST_TTL``) so ``ls -l``/``find`` do not
  hammer the API, while a re-listing still sees new files.
- **Google-native documents** (Docs/Sheets/Slides) are exported on first
  open using the same ``export_formats`` config as sync. Their reported size
  is 0 until materialized, which is how FUSE generally presents virtual
  files; ``cat``/``cp`` read to EOF and work.
- **Names are sanitized and de-duplicated.** Drive allows two siblings with
  the same name; a POSIX directory does not, so later duplicates get a
  `` (2)`` suffix.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat as statmod
import tempfile
import threading
import time

from .config import app_dir
from .drive import FOLDER_MIME, SHORTCUT_MIME, safe_name
from .engine import parse_remote_mtime

LIST_TTL = 15.0                 # seconds a directory listing stays cached
READ_ONLY_MODE_DIR = 0o555
READ_ONLY_MODE_FILE = 0o444

try:                            # only needed for an actual mount
    from fuse import FuseOSError as _FuseOSError
except ImportError:             # pragma: no cover - exercised via mount()

    class _FuseOSError(OSError):
        def __init__(self, errno_):
            super().__init__(errno_, os.strerror(errno_))


class FuseUnavailable(RuntimeError):
    """Raised when a mount is requested without fusepy/libfuse."""


def _is_native(mime):
    return str(mime or "").startswith("application/vnd.google-apps.")


def _dedupe(name, used):
    """Return a sibling name unique within `used` (case-insensitive)."""
    base, dot, ext = name.rpartition(".")
    stem, suffix = (base, "." + ext) if dot else (name, "")
    candidate = name
    n = 2
    while candidate.casefold() in used:
        candidate = f"{stem} ({n}){suffix}"
        n += 1
    used.add(candidate.casefold())
    return candidate


class DriveFS:
    """Read-only path tree over one Drive folder.

    The class deliberately has no dependency on fusepy: it is unit-tested
    with a fake Drive, and only :func:`mount` imports the FUSE bindings.
    """

    def __init__(self, drive, root_id="root", export_formats=None,
                 cache_dir=None):
        self.drive = drive
        self.root_id = root_id or "root"
        self.export_formats = dict(export_formats or {})
        self.cache_dir = str(cache_dir or
                             os.path.join(app_dir(), "mount-cache"))
        os.makedirs(self.cache_dir, mode=0o700, exist_ok=True)
        self._lock = threading.RLock()
        self._listings = {}         # folder id -> (expires, [entry])
        self._materialized = {}     # entry id -> local path
        self._open = {}             # fh -> (file object, entry)
        self._next_fh = 1

    # ---- entry model ---------------------------------------------------
    def _normalize(self, item, used):
        """Drive row -> entry dict, or None when it is not representable."""
        mime = str(item.get("mimeType") or "")
        details = item.get("shortcutDetails") or {}
        target_mime = str(details.get("targetMimeType") or "")
        item_id = str(item.get("id") or "")
        if not item_id:
            return None

        kind, export = "file", None
        if mime == FOLDER_MIME:
            kind = "dir"
        elif mime == SHORTCUT_MIME:
            if target_mime != FOLDER_MIME:
                return None         # shortcut to a file: not browsable
            item_id = str(details.get("targetId") or "")
            if not item_id:
                return None
            kind = "dir"
        elif _is_native(mime):
            export = self.export_formats.get(mime)
            if not export:
                return None         # native kind we cannot export
            kind = "gdoc"
        elif not mime or mime == "application/vnd.google-apps.shortcut":
            return None

        name = safe_name(item.get("name"))
        if export:
            ext = export[1]
            if not name.lower().endswith(ext.lower()):
                name += ext
        name = _dedupe(name, used)
        return {
            "id": item_id,
            "name": name,
            "kind": kind,
            "mime": mime,
            "size": int(item.get("size") or 0),
            "mtime": parse_remote_mtime(item.get("modifiedTime")),
            "export": export,
        }

    def _children(self, folder_id, force=False):
        now = time.monotonic()
        with self._lock:
            cached = self._listings.get(folder_id)
            if cached and not force and cached[0] > now:
                return cached[1]
        rows = self.drive.list_children(folder_id)
        used, entries = set(), []
        for row in rows:
            entry = self._normalize(row, used)
            if entry is not None:
                entries.append(entry)
        with self._lock:
            self._listings[folder_id] = (now + LIST_TTL, entries)
        return entries

    def _root_entry(self):
        return {"id": self.root_id, "name": "", "kind": "dir", "mime":
                FOLDER_MIME, "size": 0, "mtime": 0.0, "export": None}

    def resolve(self, path):
        """Path -> entry dict, or None when it does not exist."""
        parts = [p for p in str(path or "/").split("/") if p]
        if not parts:
            return self._root_entry()
        current = self._root_entry()
        for part in parts:
            if current["kind"] != "dir":
                return None
            match = None
            for child in self._children(current["id"]):
                if child["name"] == part:
                    match = child
                    break
            if match is None:
                return None
            current = match
        return current

    # ---- materialization ----------------------------------------------
    def _local_path(self, entry):
        with self._lock:
            cached = self._materialized.get(entry["id"])
        if cached and os.path.exists(cached):
            return cached
        ext = entry["export"][1] if entry.get("export") else ""
        fd, tmp = tempfile.mkstemp(prefix="ds-", suffix=ext,
                                   dir=self.cache_dir)
        os.close(fd)
        try:
            if entry["kind"] == "gdoc":
                self.drive.export(entry["id"], entry["export"][0], tmp)
            else:
                self.drive.download(entry["id"], tmp)
            os.chmod(tmp, 0o600)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        with self._lock:
            self._materialized[entry["id"]] = tmp
        return tmp

    def _file_size(self, entry):
        if entry["kind"] == "file":
            return entry["size"]
        with self._lock:
            cached = self._materialized.get(entry["id"])
        if cached and os.path.exists(cached):
            try:
                return os.path.getsize(cached)
            except OSError:
                pass
        return 0

    # ---- fusepy operations --------------------------------------------
    def getattr(self, path, fh=None):
        entry = self.resolve(path)
        if entry is None:
            raise _FuseOSError(errno.ENOENT)
        is_dir = entry["kind"] == "dir"
        return {
            "st_mode": (statmod.S_IFDIR | READ_ONLY_MODE_DIR) if is_dir
                        else (statmod.S_IFREG | READ_ONLY_MODE_FILE),
            "st_nlink": 2 if is_dir else 1,
            "st_size": 0 if is_dir else self._file_size(entry),
            "st_mtime": entry["mtime"] or 0,
            "st_ctime": entry["mtime"] or 0,
            "st_atime": entry["mtime"] or 0,
            "st_uid": os.getuid(),
            "st_gid": os.getgid(),
        }

    def readdir(self, path, fh=None):
        entry = self.resolve(path)
        if entry is None or entry["kind"] != "dir":
            raise _FuseOSError(errno.ENOTDIR if entry else errno.ENOENT)
        return [".", ".."] + [e["name"] for e in self._children(entry["id"])]

    def open(self, path, flags):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC |
                    os.O_APPEND):
            raise _FuseOSError(errno.EROFS)
        entry = self.resolve(path)
        if entry is None:
            raise _FuseOSError(errno.ENOENT)
        if entry["kind"] == "dir":
            raise _FuseOSError(errno.EISDIR)
        local = self._local_path(entry)
        fh = os.open(local, os.O_RDONLY)
        with self._lock:
            self._open[fh] = entry
        return fh

    def read(self, path, size, offset, fh):
        with self._lock:
            if fh not in self._open:
                raise _FuseOSError(errno.EBADF)
        try:
            os.lseek(fh, offset, os.SEEK_SET)
            return os.read(fh, size)
        except OSError:
            raise _FuseOSError(errno.EIO)

    def release(self, path, fh):
        with self._lock:
            self._open.pop(fh, None)
        try:
            os.close(fh)
        except OSError:
            pass
        return 0

    def access(self, path, mode):
        if mode & os.W_OK:
            raise _FuseOSError(errno.EROFS)
        if self.resolve(path) is None:
            raise _FuseOSError(errno.ENOENT)
        return 0

    def statfs(self, path):
        try:
            usage = shutil.disk_usage(self.cache_dir)
            free = usage.free
        except OSError:
            free = 0
        block = 4096
        return {"f_bsize": block, "f_frsize": block,
                "f_blocks": max(1, free // block), "f_bfree": free // block,
                "f_bavail": free // block, "f_files": 0, "f_ffree": 0,
                "f_favail": 0, "f_namemax": 255}

    # Writes are refused, not silently ignored.
    def _read_only(self, *a, **kw):
        raise _FuseOSError(errno.EROFS)

    mkdir = rmdir = unlink = rename = truncate = chmod = chown = \
        create = write = symlink = link = _read_only


def mount(drive, mountpoint, root_id="root", export_formats=None,
          foreground=True, allow_other=False, debug=False):
    """Mount `root_id` at `mountpoint` (read-only). Blocks while mounted.

    Raises :class:`FuseUnavailable` with installation instructions when the
    optional ``fusepy`` bindings (or libfuse) are missing.
    """
    try:
        from fuse import FUSE
    except ImportError as exc:      # pragma: no cover - depends on host
        raise FuseUnavailable(
            "FUSE support needs the optional 'fusepy' package and libfuse.\n"
            "  Debian/Ubuntu: sudo apt install fuse libfuse2 && "
            "pip install fusepy\n"
            "  Fedora:        sudo dnf install fuse fuse-libs && "
            "pip install fusepy\n"
            "The GUI Drive browser works without it.") from exc
    mountpoint = os.path.abspath(os.path.expanduser(str(mountpoint)))
    if not os.path.isdir(mountpoint):
        raise ValueError(f"mount point does not exist: {mountpoint}")
    fs = DriveFS(drive, root_id=root_id, export_formats=export_formats)
    FUSE(fs, mountpoint, foreground=foreground, ro=True,
         allow_other=allow_other, debug=debug)
    return fs
