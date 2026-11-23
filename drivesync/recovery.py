"""Recovery operations: what was protected, and how to get files back.

The engine already keeps every destructive outcome recoverable:
  * local deletions  -> moved to <app dir>/trash/<pair>/<stamp>/<rel>
  * remote deletions -> Drive's own trash (untrash restores them)
  * overwrites       -> previous bytes kept under <app dir>/history/<pair>/<rel>/
  * conflicts        -> copies named <stem>.conflict-<ts><ext> (conflicts.py)

This module is the read/restore layer over those locations plus the
pending-deletion holds the safety valve writes. It never deletes user data:
"restore" copies/moves files back, and purging is deliberately out of scope
(trash dirs are bounded by the user deleting pairs / running cleanup, not by
a recovery action).
"""

import json
import os
import shutil
from datetime import datetime, timezone

from .config import app_dir
from .security import safe_join

# Actions recorded in the recovery table (kept here so callers and tests
# share one vocabulary; db.recovery_log takes them as plain strings).
A_SAFETY_STOP = "safety_stop"
A_ALLOWED = "deletions_allowed"
A_KEPT = "deletions_kept"
A_PENDING_CLEARED = "pending_cleared"
A_TRASH_RESTORED = "trash_restored"
A_DRIVE_TRASH_RESTORED = "drive_trash_restored"
A_VERSION_RESTORED = "version_restored"
A_CONFLICT_RESOLVED = "conflict_resolved"


# ---- pending deletions (mass-deletion protection) ---------------------
def pending_deletions(store, pair_id):
    """The stored hold for one pair, or None. Malformed JSON reads as None
    (a corrupted hold must never wedge a pair)."""
    raw = store.kv_get(f"pending_deletes:{pair_id}")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "count" not in data:
        return None
    data["pair_id"] = pair_id
    return data


def all_pending_deletions(store):
    """Holds for every pair, newest first (for the Recovery page)."""
    out = []
    for pair in store.pairs():
        hold = pending_deletions(store, pair["id"])
        if hold:
            hold["pair_name"] = pair["remote_path"] or pair["local_path"]
            out.append(hold)
    return out


def allow_pending_deletions(store, pair_id):
    """Approve the held deletions for exactly one pass.

    The engine consumes the approval when it next applies the pair (and
    logs `deletions_allowed`), so a stale approval cannot linger.
    """
    store.kv_set(f"deletes_approved:{pair_id}", "once")


def keep_pending_files(store, pair_id):
    """Resolve the hold WITHOUT deleting: switch the pair to deletes-off.

    The engine's ack path then tombstones every held deletion (files stay
    on both sides, state stays stable) and the hold is released. The user
    re-enables deletions from the pair edit dialog when they want them.
    """
    store.update_pair(pair_id, allow_deletes=0)
    store.kv_delete(f"pending_deletes:{pair_id}")
    store.kv_delete(f"deletes_approved:{pair_id}")
    store.log(pair_id, "recovery", "",
              "deletes switched off — every held deletion was kept; the "
              "files stay on both sides", level="conflict")
    store.recovery_log(pair_id, "", A_KEPT,
                       "user chose to keep the files (deletes off)",
                       "the pair will acknowledge deletions instead of "
                       "applying them; re-enable with the pair editor")


# ---- local trash (locally preserved copies) ---------------------------
def local_trash_root(pair_id=None):
    root = str(app_dir() / "trash")
    return os.path.join(root, str(pair_id)) if pair_id is not None else root


def list_local_trash(store, pair_id=None, limit=500):
    """Everything in the recoverable local trash, newest stamp first.

    Layout: trash/<pair>/<stamp>/<rel> (one stamp per sync pass). Rows carry
    the pair id/name so the UI can group them; missing pairs (deleted while
    their trash remained) still show — the files exist and are restorable to
    a chosen destination, but pair-scoped actions need the pair to exist.
    """
    out = []
    root = local_trash_root()
    if not os.path.isdir(root):
        return out
    pairs = {p["id"]: p for p in store.pairs()}
    for pid_name in sorted(os.listdir(root), reverse=True):
        pid_dir = os.path.join(root, pid_name)
        if not os.path.isdir(pid_dir) or not pid_name.isdigit():
            continue
        pid = int(pid_name)
        row = pairs.get(pid)
        pair = dict(row) if row is not None else None
        for stamp in sorted(os.listdir(pid_dir), reverse=True):
            stamp_dir = os.path.join(pid_dir, stamp)
            if not os.path.isdir(stamp_dir):
                continue
            for dirpath, _dirs, files in os.walk(stamp_dir):
                for name in files:
                    path = os.path.join(dirpath, name)
                    rel = os.path.relpath(path, stamp_dir).replace("\\", "/")
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    out.append({
                        "pair_id": pid,
                        "pair_name": (pair or {}).get("remote_path")
                                     or (pair or {}).get("local_path")
                                     or f"(removed pair #{pid})",
                        "stamp": stamp,
                        "rel": rel,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "path": path,
                    })
                    if len(out) >= limit:
                        return out
    return out


def _safe_stamp(stamp):
    """Reject a trash stamp that is not a single directory name.

    The stamp comes from an API request; allowing ``..`` or a slash here
    would let a caller point the trash root at an arbitrary directory.
    """
    text = str(stamp or "")
    if not text or text in (".", ".."):
        raise ValueError("invalid trash entry")
    if "/" in text or "\\" in text or "\x00" in text:
        raise ValueError("invalid trash entry")
    return text


def restore_from_trash(store, pair_id, stamp, rel):
    """Move one file from the local trash back to its original path.

    The original path wins when it is free; when occupied, the file comes
    back under a `.restored-<stamp>` sibling name instead of overwriting
    anything. Empty stamp directories are cleaned up afterwards. Returns a
    short result dict.
    """
    stamp = _safe_stamp(stamp)
    src = safe_join(os.path.join(local_trash_root(pair_id), stamp), rel)
    if not os.path.isfile(src):
        raise ValueError("that file is no longer in the local trash")
    pair = store.pair(pair_id)
    if not pair:
        raise ValueError("the sync pair for this trash item no longer exists")
    dest_rel = rel.replace("\\", "/")
    root = str(pair["local_path"])
    dest = safe_join(root, dest_rel)
    if os.path.lexists(dest):
        parent, name = os.path.split(dest_rel)
        stem, dot, ext = name.partition(".") if "." in name else (name, "", "")
        dest_rel = (f"{parent}/{stem}.restored-{stamp}{dot}{ext}"
                    if parent else f"{stem}.restored-{stamp}{dot}{ext}")
        dest = safe_join(root, dest_rel)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    shutil.move(src, dest)
    _prune_empty_stamp(local_trash_root(pair_id), stamp)
    store.log(pair_id, "recovery", dest_rel,
              f"restored from local trash (pass {stamp})")
    store.recovery_log(pair_id, dest_rel, A_TRASH_RESTORED,
                       f"restored from the local trash of pass {stamp}",
                       f"now at {dest_rel}" +
                       (" (renamed: the original path was occupied)"
                        if dest_rel != rel else ""))
    return {"pair_id": pair_id, "rel": dest_rel, "path": dest,
            "renamed": dest_rel != rel}


def _prune_empty_stamp(pair_trash_root, stamp):
    """Remove the stamp dir when the restore emptied it, then the pair dir."""
    try:
        stamp = _safe_stamp(stamp)
    except ValueError:
        return
    stamp_dir = os.path.join(pair_trash_root, stamp)
    try:
        if os.path.isdir(stamp_dir) and not os.listdir(stamp_dir):
            os.rmdir(stamp_dir)
    except OSError:
        pass
    try:
        pair_dir = os.path.dirname(stamp_dir)
        if os.path.isdir(pair_dir) and not os.listdir(pair_dir):
            os.rmdir(pair_dir)
    except OSError:
        pass


# ---- Drive trash (restore files Google still holds) -------------------
def list_drive_trash(pair, drive, limit=100):
    """The account's Drive trash rows, annotated for the UI.

    Drive trash is account-wide, not per pair — the UI says so. Rows are
    marked readable (size/mtime/trashedTime may be absent for foreign
    items) and the UI communicates what a restore will do.
    """
    items = drive.list_trashed(limit=limit)
    now = datetime.now(timezone.utc).timestamp()
    for it in items:
        trashed = it.get("trashedTime")
        it["trashed_ago_days"] = None
        if trashed:
            try:
                ts = datetime.fromisoformat(
                    trashed.replace("Z", "+00:00")).timestamp()
                it["trashed_ago_days"] = max(0, int((now - ts) // 86400))
            except (ValueError, TypeError):
                pass
    return items


def restore_drive_trash(store, pair, drive, file_id):
    """Take one file out of the Drive trash; the next pass reconciles it.

    Google restores the file to its original parents. If the base row for
    the path is gone (a synced deletion), the next pass treats the file as
    new-on-remote and downloads it again — that is the recovery. Returns a
    short result dict; Drive errors propagate to the caller.
    """
    meta = drive.untrash(file_id)
    name = meta.get("name") or file_id
    store.log(pair["id"], "recovery", name,
              "restored from the Drive trash (whole account view)")
    store.recovery_log(pair["id"], name, A_DRIVE_TRASH_RESTORED,
                       "user restored a Drive-trashed file",
                       f"Drive id {file_id} untrashed; the next pass "
                       "re-downloads it if it belongs to this pair")
    return {"pair_id": pair["id"], "file_id": file_id, "name": name}


# ---- version history (previous versions kept before overwrites) -------
def _history_root(pair_id):
    return str(app_dir() / "history" / str(pair_id))


def list_versions(pair_id, rel, limit=100):
    """Kept previous versions of one local path, newest first.

    Layout mirrors engine.keep_old_version(): history/<pair>/<rel>/
    <UTC stamp>-<name>. Returns [] when the pair never overwrote the file.
    """
    rel = str(rel or "").replace("\\", "/")
    if not rel:
        return []
    hist_dir = safe_join(_history_root(pair_id), rel)
    if not os.path.isdir(hist_dir):
        return []
    name = rel.rsplit("/", 1)[-1]
    out = []
    for fname in sorted(os.listdir(hist_dir), reverse=True):
        path = os.path.join(hist_dir, fname)
        if not os.path.isfile(path) or not fname.endswith(f"-{name}"):
            continue
        stamp = fname[:-(len(name) + 1)]
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({"pair_id": pair_id, "rel": rel, "stamp": stamp,
                    "size": st.st_size, "mtime": st.st_mtime,
                    "path": path})
        if len(out) >= limit:
            break
    return out


def restore_version(store, pair_id, rel, stamp):
    """Put a kept previous version back at its original path.

    The current file is first pushed into the history under a fresh stamp
    (so a restore is itself undoable), then the chosen version replaces it.
    The next sync pass uploads the restored content to Drive. Returns a
    short result dict.
    """
    versions = {v["stamp"]: v for v in list_versions(pair_id, rel)}
    version = versions.get(stamp)
    if version is None:
        raise ValueError("that version is no longer in the history")
    pair = store.pair(pair_id)
    if not pair:
        raise ValueError("this sync pair no longer exists")
    dest = safe_join(str(pair["local_path"]), rel)
    if os.path.isfile(dest):
        keep_stamp = datetime.now(timezone.utc).strftime(
            "%Y%m%d-%H%M%S-%f")
        hist_dir = os.path.dirname(version["path"])
        backup = os.path.join(hist_dir, f"{keep_stamp}-"
                              f"{os.path.basename(dest)}")
        shutil.copy2(dest, backup)          # the current bytes stay recoverable
        history_note = f"current copy kept as {os.path.basename(backup)}"
    else:
        history_note = "no local file existed; restored into place"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    shutil.copy2(version["path"], dest)
    store.log(pair_id, "recovery", rel,
              f"previous version {stamp} restored; {history_note}")
    store.recovery_log(pair_id, rel, A_VERSION_RESTORED,
                       f"restored history version {stamp}",
                       history_note + "; the next pass pushes it to Drive")
    return {"pair_id": pair_id, "rel": rel, "stamp": stamp, "path": dest}
