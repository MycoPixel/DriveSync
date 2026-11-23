"""Review and resolve conflict copies left behind by sync passes.

The engine never loses data on a conflict: per policy it keeps one side at
the original path and preserves the other as a copy named
``<stem>.conflict-<UTC timestamp><ext>`` (uploaded too, so the copy exists on
both sides). Those copies are safe but noisy, so this module lists them and
lets the user keep the copy, keep the original, or leave both untouched.

Resolution is deliberately explicit: it never guesses. "keep_copy" replaces
the original with the copy's content and removes the copy; "keep_original"
discards the copy; "keep_both" changes nothing.
"""

import hashlib
import os
import re
import shutil

from .recovery import A_CONFLICT_RESOLVED
from .security import safe_join

# Mirrors engine.Engine.conflict_name(): the first dot separates the stem
# from the extension, and hidden files (".foo") produce ".conflict-<ts>.foo".
CONFLICT_RE = re.compile(
    r"^(?P<stem>[^.]*)\.conflict-(?P<tag>\d{8}-\d{6})(?P<ext>\..+)?$")

CHOICES = ("keep_copy", "keep_original", "keep_both")


def parse_conflict_name(rel):
    """Return ``(copy_rel, original_rel)`` for a conflict copy, else None."""
    rel = (rel or "").replace("\\", "/").strip("/")
    if not rel:
        return None
    parent, _, name = rel.rpartition("/")
    match = CONFLICT_RE.match(name)
    if not match:
        return None
    original = match.group("stem") + (match.group("ext") or "")
    return rel, (f"{parent}/{original}" if parent else original)


def _local(root, rel):
    return safe_join(root, rel)


def _md5(path):
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remote_parent_id(store, pair, rel):
    """Drive folder id holding rel, using the base snapshot's dir rows."""
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    if not parent:
        return pair["remote_id"]
    row = store.base_get(pair["id"], parent)
    if row and row["remote_id"]:
        return row["remote_id"]
    raise ValueError(
        f"the Drive folder for {parent!r} is not tracked yet; run a sync first")


def list_conflicts(store, pair):
    """Conflict copies of one pair, newest paths last.

    Only base rows are consulted (cheap, and every copy the engine creates
    has one), so this never walks a large tree. A row whose local file was
    removed by hand is still listed so it can be cleaned up on Drive.
    """
    root = pair["local_path"]
    out = []
    for row in store.base_entries(pair["id"]):
        parsed = parse_conflict_name(row["rel_path"])
        if not parsed:
            continue
        copy_rel, original_rel = parsed
        copy_path = _local(root, copy_rel)
        size = None
        try:
            size = os.path.getsize(copy_path)
        except OSError:
            pass
        out.append({
            "pair_id": pair["id"],
            "path": copy_rel,
            "original": original_rel,
            "size": size,
            "remote_id": row["remote_id"],
            "local_exists": os.path.isfile(copy_path),
            "original_local_exists": os.path.isfile(
                _local(root, original_rel)),
        })
    out.sort(key=lambda item: item["path"])
    return out


def resolve_conflict(store, pair, drive, copy_rel, choice):
    """Resolve one conflict copy; returns a short result dict.

    choice is one of CHOICES. Raises ValueError for unknown paths/choices
    and lets Drive errors propagate so the UI can report them.
    """
    if choice not in CHOICES:
        raise ValueError(f"choice must be one of {CHOICES}")
    parsed = parse_conflict_name(copy_rel)
    if not parsed:
        raise ValueError("not a conflict copy")
    copy_rel, original_rel = parsed
    row = store.base_get(pair["id"], copy_rel)
    if not row:
        raise ValueError("this conflict copy is already resolved")

    result = {"pair_id": pair["id"], "path": copy_rel,
              "original": original_rel, "choice": choice}
    if choice == "keep_both":
        return result
    root = pair["local_path"]
    copy_path = _local(root, copy_rel)
    original_path = _local(root, original_rel)
    remote_copy_id = row["remote_id"]

    if choice == "keep_original":
        # remove the copy from Drive first: if that fails the local copy is
        # still there and the user can retry without losing track of it
        if remote_copy_id:
            drive.trash(remote_copy_id)
        if os.path.lexists(copy_path):
            os.remove(copy_path)
        store.base_delete(pair["id"], copy_rel)
        store.log(pair["id"], "conflict-resolved", copy_rel,
                  f"kept original {original_rel}; discarded the conflict copy")
        store.recovery_log(pair["id"], copy_rel, A_CONFLICT_RESOLVED,
                           f"kept the original {original_rel}",
                           "the conflict copy was discarded on both sides")
        return result

    # keep_copy: the copy wins. Put its content at the original path, make
    # Drive agree, then drop the copy from both sides and record the new
    # base state so the next pass sees a clean, unchanged file.
    if not os.path.isfile(copy_path):
        raise ValueError("the conflict copy is missing on disk")
    os.makedirs(os.path.dirname(original_path) or ".", exist_ok=True)
    shutil.copy2(copy_path, original_path)

    original_row = store.base_get(pair["id"], original_rel)
    remote_id = original_row["remote_id"] if original_row else None
    meta = None
    if remote_id:
        try:
            meta = drive.update_content(remote_id, original_path)
        except Exception:
            # the remote original may have been deleted/trashed; fall back
            # to creating it again under the same name
            meta = None
    if meta is None:
        parent_id = _remote_parent_id(store, pair, original_rel)
        meta = drive.upload(original_path, os.path.basename(original_rel),
                            parent_id)
        remote_id = meta.get("id")

    if remote_copy_id:
        drive.trash(remote_copy_id)
    os.remove(copy_path)
    store.base_delete(pair["id"], copy_rel)

    size = os.path.getsize(original_path)
    store.base_put(
        pair["id"], original_rel, "file", remote_id=remote_id,
        local_md5=_md5(original_path), local_size=size,
        local_mtime=os.path.getmtime(original_path),
        remote_md5=meta.get("md5Checksum"),
        remote_version=str(meta.get("version", "")),
        remote_size=int(meta["size"]) if meta.get("size") else None)
    store.log(pair["id"], "conflict-resolved", copy_rel,
              f"kept {copy_rel} as {original_rel}; removed the other copy")
    store.recovery_log(pair["id"], original_rel, A_CONFLICT_RESOLVED,
                       f"kept the conflict copy over {original_rel}",
                       f"{copy_rel} won and was removed afterwards")
    return result
