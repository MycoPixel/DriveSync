"""Sync engine: three-way reconciliation of local folder, Drive and base.

For every path we compare three states:
  * L — current local file system (under the pair's local root)
  * R — current remote Drive state (under the pair's remote folder)
  * B — the base snapshot stored in SQLite at the end of the last sync

The classic cases fall out of that comparison:
  L changed, R unchanged  -> push local
  R changed, L unchanged  -> pull remote
  both changed            -> conflict policy (newer/local/remote/both)
  one side deleted        -> propagate delete, unless the other side changed
                             since the base (then the policy decides)

The decide() step is pure (maps in, op list out) so it is fully unit-testable
against a mock Drive; apply() then performs the ops and updates the base.

The pass runs in three stages (Phase 3.1):

  SCANNER  scan_pair_state()  gathers everything input-shaped: local walk,
           Drive state (delta feed or full listing), the base snapshot,
           export folding, case canonicalization, the md5 cache. It
           decides nothing.
  PLANNER  build_plan()       runs the pure decide() over the scan, records
           WHY each op happens (PlannedOp.reason), orders the ops and
           applies the delete-safety gate. A Plan is data: serializable,
           previewable, the executor's whole input.
  EXECUTOR execute_plan()     applies the Plan (pre-ops -> parallel
           transfers -> deletions last), records per-op outcomes in an
           ExecReport, updates the base. One failed op never aborts the
           pass; partial progress always lands.

sync_pair() is the thin orchestration; preview_pair() is scanner+planner
with cleanup=False (strictly read-only).
"""

import fnmatch
import hashlib
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import app_dir
from .db import parse_include_patterns
from .drive import (FOLDER_MIME, PART_SUFFIX,
                    resolve_export_formats, safe_name)
from .security import safe_join

GDOC_PREFIX = "application/vnd.google-apps."
CONFLICT_TAG = "%Y%m%d-%H%M%S"
DESTRUCTIVE_OPS = {"delete_local", "delete_remote", "rmdir_local", "rmdir_remote"}
TRANSFER_OPS = {"upload", "download"}


# ---------------------------------------------------------------- helpers
def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def export_lex(rel, ext):
    """Local rel path for an exported Google-native doc: append ext once.

    A Drive doc named "Report" materializes as "Report.docx"; a doc already
    named "Report.docx" on Drive keeps that name (no double extension).
    """
    return rel if rel.lower().endswith(ext) else rel + ext


def fold_exports(local, remote, base=None, export_map=None):
    """Align exported Google-native docs between the scans.

    The remote rel for a native doc has no extension ("Report") while the
    local exported file does ("Report.docx"). Re-key the local entry under
    the matching rel so decide() compares the right pair, mark it folded
    and return the fold map (rel -> local rel) that apply() uses to resolve
    real on-disk paths. A bare local file with the same name is left alone
    (collision handling decides what happens to it).

    Two alignments:
    1. normal: remote rel "Report" <-> local "Report.docx";
    2. pending rename: the doc was renamed on Drive, so the exported file
       still sits under the old rel+ext and the base row holds the old rel —
       keep the local entry keyed by the old rel so rename detection (which
       matches the base row against the new remote rel by version) can pair
       them and move the file.
    """
    fold = {}
    for rel, r in remote.items():
        if r.get("kind") == "gdoc" and r.get("export"):
            lex = export_lex(rel, r["export"][1])
            if rel in local or lex not in local:
                continue
            b = (base or {}).get(rel)
            # only treat the local file as our export when the base row
            # proves it: kind 'file' with no remote_md5 is the shape written
            # by op_download for a materialized native doc. A user file that
            # merely shares the name must stay visible (unfolded) so decide()
            # emits skip_gdoc_collision instead of overwriting it.
            ours = bool(b and b.get("kind") == "file" and
                        b.get("remote_md5") is None)
            local[rel] = local.pop(lex)
            if ours:
                local[rel]["folded"] = True
            fold[rel] = lex
    if base:
        exts = [ext for _, ext in (export_map or {}).values()]
        for old_rel, b in base.items():
            if old_rel in remote or old_rel in fold or old_rel in local or \
                    b.get("kind") != "file" or b.get("remote_md5") is not None:
                continue
            for ext in exts:
                old_lex = export_lex(old_rel, ext)
                if old_lex in local:
                    local[old_rel] = local.pop(old_lex)
                    local[old_rel]["folded"] = True
                    fold[old_rel] = old_lex
                    break
    return fold


def parse_remote_mtime(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0.0


def kind_of(mime):
    if mime == "application/vnd.google-apps.folder":
        return "dir"
    if mime.startswith(GDOC_PREFIX):
        return "gdoc"
    return "file"


def ignored(rel, patterns):
    """True if rel matches any ignore pattern (name or path glob)."""
    if not patterns:
        return False
    name = rel.rsplit("/", 1)[-1]
    for pat in patterns:
        if not pat:
            continue
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


def included(rel, include, is_file=False):
    """True if rel is inside the pair's selective-sync scope.

    include is a list of subfolder path prefixes ("docs", "photos/2024");
    empty list = everything is in scope. A rel matches when it IS a chosen
    prefix, lies under one, or is a directory an included path lives in
    (the ancestor clause keeps the local walk and Drive listing from
    pruning a parent that excluded children still need). Loose files in
    the pair root (rel without '/') always mirror — the scope limits
    subfolders, which is what the pickers promise. is_file must reflect
    the item kind so the rule stays identical across full and delta scans.
    """
    if not include:
        return True
    if "/" not in rel and is_file:
        return True
    for p in include:
        if rel == p or rel.startswith(p + "/") or p.startswith(rel + "/"):
            return True
    return False


def scan_local(root, patterns, include=None, symlink_policy="skip"):
    """Walk the local root -> {rel: {'kind','size','mtime','md5'}}.

    md5 starts as None and is computed on demand via md5_of(rel).
    Paths outside the pair's include scope (selective sync) are skipped —
    excluded subfolders stay on disk untouched and invisible to decide().

    Symlinks follow the pair's symlink_policy. "skip" (the default) leaves
    every link out of the snapshot: a link to a file outside the tree would
    otherwise be uploaded as if it were the file, and a linked folder would
    look empty to decide(), which reads as "the children were deleted".
    "follow" records links to real files and descends into linked folders,
    guarded by a (st_dev, st_ino) set so a link cycle cannot make the walk
    diverge.
    """
    root = str(root)
    items = {}
    if not os.path.isdir(root):
        return items
    followed = symlink_policy == "follow"
    seen_dirs = set()          # (st_dev, st_ino) of descended dirs — loop guard

    def target_id(path):
        try:
            st = os.stat(path)      # follows the link: identity of the target
            return (st.st_dev, st.st_ino)
        except OSError:
            return None

    seen_dirs.add(target_id(root))
    for dirpath, dirnames, filenames in os.walk(root, followlinks=followed):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        # drop ignored/out-of-scope directories from the walk entirely
        keep = []
        for d in sorted(dirnames):
            full = os.path.join(dirpath, d)
            rel = f"{rel_dir}/{d}" if rel_dir else d
            link = os.path.islink(full)
            if link and not followed:
                continue       # a link is not the folder it points at
            if ignored(rel, patterns) or not included(rel, include):
                continue
            if link:
                ident = target_id(full)
                if ident is None or ident in seen_dirs:
                    continue   # broken link, or a symlink loop: do not descend
                seen_dirs.add(ident)
            keep.append(d)
            items[rel] = {"kind": "dir", "size": None, "mtime": None, "md5": None}
        dirnames[:] = keep
        for f in sorted(filenames):
            full = os.path.join(dirpath, f)
            rel = f"{rel_dir}/{f}" if rel_dir else f
            if f.endswith(PART_SUFFIX):
                continue       # a half-downloaded transfer in progress
            if ignored(rel, patterns) or not included(rel, include,
                                                      is_file=True):
                continue
            if os.path.islink(full) and not followed:
                continue       # never sync link bytes as the target's content
            try:
                st = os.stat(full)     # follows symlinks when following
            except OSError:
                continue
            items[rel] = {"kind": "file", "size": st.st_size,
                          "mtime": st.st_mtime, "md5": None}
    return items


def canonicalize_case(local, remote, base, notify=None):
    """Case-insensitive mode: re-key the three scans to one spelling per path.

    On FAT/exFAT/NTFS mounts, "Report.docx" and "report.docx" are the same
    file; leaving the maps keyed by different spellings makes every case-only
    rename look like a delete plus a create, and churns on every pass. Paths
    are matched by their lower-cased form, and each is re-keyed to the first
    spelling claimed for it — local on-disk names first (the spelling the
    filesystem will actually use), then base rows, then remote names. Parent
    paths are canonicalized before children so the tree stays consistent.

    Returns (local, remote, base, shadowed): shadowed lists base rows that
    duplicate another row by case, which the caller deletes so stale
    spellings cannot accumulate in state.db. notify(rel, source) hears about
    same-case collisions inside one scan — Drive really can hold "A.txt" and
    "a.txt" side by side, and only one of them can exist on a FAT volume.
    """
    spelling = {}

    def claim(rel):
        if "/" in rel:
            parent, _, name = rel.rpartition("/")
            rel = f"{claim(parent)}/{name}"
        key = rel.lower()
        spelling.setdefault(key, rel)
        return spelling[key]

    for rel in sorted(local):
        claim(rel)
    for rel in sorted(base):
        claim(rel)
    for rel in sorted(remote):
        claim(rel)

    shadowed = []

    def rekey(mapping, source=None, track=None):
        out = {}
        for rel, val in sorted(mapping.items()):
            key = claim(rel)
            if key in out:
                if track is not None:
                    track.append(rel)
                if notify:
                    notify(rel, source)
                continue
            out[key] = val
        return out

    new_local = rekey(local, "local")
    new_base = rekey(base, "base", track=shadowed)
    new_remote = rekey(remote, "remote")
    return new_local, new_remote, new_base, shadowed


def scan_remote(drive, root_id, patterns, export_map=None, include=None):
    """Drive list_tree -> {rel: {'kind','id','md5','version','mtime','size'}}.

    Google-native docs that have a convertible export format also carry
    "export": (export_mime, file_extension). Paths outside the include
    scope are left out so selective-sync pairs never track them.
    """
    raw = drive.list_tree(root_id)
    items = {}
    for rel, f in raw.items():
        k = kind_of(f.get("mimeType", ""))
        if rel and (ignored(rel, patterns) or
                    not included(rel, include, is_file=(k != "dir"))):
            continue
        item = {
            "kind": k,
            "id": f.get("id"),
            # Google-native docs have no content checksum on the real API —
            # normalize to None so change detection stays on `version`
            "md5": None if k == "gdoc" else f.get("md5Checksum"),
            "version": str(f.get("version", "")),
            "mtime": parse_remote_mtime(f.get("modifiedTime")),
            "size": int(f["size"]) if f.get("size") else None,
            "mime": f.get("mimeType", ""),
        }
        if k == "gdoc" and export_map:
            exp = export_map.get(f.get("mimeType", ""))
            if exp:
                item["export"] = exp
        items[rel] = item
    # drop remote items nested under ignored dirs
    for rel in [r for r in items if r and ignored(r, patterns)]:
        prefix = rel + "/"
        items = {k: v for k, v in items.items()
                 if k == rel or not k.startswith(prefix)}
    return items


def base_map(rows):
    return {r["rel_path"]: dict(r) for r in rows}


# ---------------------------------------------------------------- delta
class DeltaUnsupported(Exception):
    """The changes feed cannot express this delta — a full scan is needed."""


def remote_item_from_base(b, export_map):
    """Rebuild what scan_remote would have returned for an unchanged entry.

    Delta scans assume 'not in the feed' means 'unchanged since the last
    pass' — true because the feed covers everything since the token and the
    base snapshot was written exactly at that point. The item is synthesized
    from the remote metadata stored alongside the base row.
    """
    mime = b.get("remote_mime") or ""
    kind = kind_of(mime) if mime else b.get("kind")
    item = {
        "kind": kind,
        "id": b.get("remote_id"),
        "md5": None if kind == "gdoc" else b.get("remote_md5"),
        "version": str(b.get("remote_version") or ""),
        "mtime": float(b.get("remote_mtime") or 0.0),
        "size": b.get("remote_size"),
        "mime": mime,
    }
    if kind == "gdoc" and export_map:
        exp = export_map.get(mime)
        if exp:
            item["export"] = exp
    return item


def scan_remote_delta(pair, base_rows, export_map, patterns, changes,
                      include=None):
    """Remote map for one pair: base snapshot + the account's changes feed.

    File-level changes (create / edit / delete / rename / move) are applied
    directly — the common cases, and exactly what makes a one-file edit in a
    large pair fast. Structural changes — tracked dirs renamed or moved,
    brand-new remote dirs (their unknown subtree could be anything), or base
    rows written by an older version without remote metadata — raise
    DeltaUnsupported so the caller falls back to a full scan.
    """
    rows = [dict(r) for r in base_rows]
    if not all((r.get("remote_mime") or "") for r in rows):
        raise DeltaUnsupported("base rows lack remote metadata (older layout)")
    index = {r["remote_id"]: r["rel_path"] for r in rows if r["remote_id"]}
    root_id = pair["remote_id"]
    if root_id in index:
        raise DeltaUnsupported("root id tracked as a child")
    index[root_id] = ""                       # the pair root itself

    remote = {}
    for r in rows:
        if r.get("deleted_remote"):
            continue                          # acknowledged deletion
        if r["rel_path"] and not included(
                r["rel_path"], include, is_file=(r["kind"] != "dir")):
            continue   # out-of-scope base row (scope shrank): decide forgets it
        remote[r["rel_path"]] = remote_item_from_base(r, export_map)

    def drop(rel):
        remote.pop(rel, None)
        for k in [k for k in remote if k.startswith(rel + "/")]:
            remote.pop(k, None)

    def feed_item(entry):
        mime = entry.get("mimeType", "")
        item = {
            "kind": kind_of(mime),
            "id": entry.get("id"),
            "md5": None if mime.startswith(GDOC_PREFIX)
                   else entry.get("md5Checksum"),
            "version": str(entry.get("version", "")),
            "mtime": parse_remote_mtime(entry.get("modifiedTime")),
            "size": int(entry["size"]) if entry.get("size") else None,
            "mime": mime,
        }
        if item["kind"] == "gdoc" and export_map:
            exp = export_map.get(mime)
            if exp:
                item["export"] = exp
        return item

    for ch in changes:
        fid = ch.get("fileId")
        if not fid or fid == root_id:
            continue                          # root itself: nothing to sync
        entry = ch.get("file") or {}
        if bool(ch.get("removed")) or bool(entry.get("trashed")):
            if fid in index:
                drop(index.pop(fid))
            continue
        if not entry.get("parents"):
            continue
        parent_rel = None
        for pid in entry["parents"]:
            if pid in index:
                parent_rel = index[pid]
                break
        if parent_rel is None:
            if fid in index:
                drop(index.pop(fid))          # moved outside the pair tree
            continue                          # change outside this pair
        name = safe_name(entry.get("name", ""))
        new_rel = f"{parent_rel}/{name}" if parent_rel else name
        if fid in index:
            old_rel = index[fid]
            if new_rel != old_rel:
                is_dir = (remote.get(old_rel) or {}).get("kind") == "dir" or \
                    any(r["rel_path"].startswith(old_rel + "/") for r in rows)
                if is_dir:
                    raise DeltaUnsupported(f"tracked dir changed rel: {old_rel}")
                drop(old_rel)
            if ignored(new_rel, patterns) or \
                    not included(new_rel, include, is_file=True):
                # renamed/edited out of scope: full scans answer the same way
                # (base row is forgotten by decide, local copy follows the
                # delete policy) — do not keep tracking it here
                index.pop(fid, None)
                continue
            remote[new_rel] = feed_item(entry)
            index[fid] = new_rel
        else:
            if kind_of(entry.get("mimeType", "")) == "dir":
                raise DeltaUnsupported(
                    f"new remote dir under tracked parent: {new_rel}")
            if ignored(new_rel, patterns) or \
                    not included(new_rel, include, is_file=True):
                continue
            remote[new_rel] = feed_item(entry)
            index[fid] = new_rel
    return remote


# ---------------------------------------------------------------- decide
def decide(local, remote, base, direction="bidirectional", policy="newer",
           md5_of=None, force=False, allow_deletes=True, reasons=None):
    """Return the ordered op list. Pure function of the three maps.

    force=True re-checks content hashes even when size and mtime look
    unchanged (catches same-size/same-mtime edits).
    allow_deletes=False replaces destructive ops with ack_* acknowledgements:
    files are kept on both sides and the base is tombstoned so the state is
    stable — no resurrection churn on later passes.
    reasons: optional dict; every emitted op records rel -> a short human
    explanation (what changed and which policy branch fired). The planner
    passes one so previews can show WHY each op happens.
    """
    md5_of = md5_of or (lambda rel: None)
    ops = []
    handled = set()          # rels decided by rename detection

    local = dict(local)
    remote = dict(remote)
    base = dict(base)

    can_push = direction in ("bidirectional", "upload")
    can_pull = direction in ("bidirectional", "download")

    # ---- pass 1: rename detection (files only) ---------------------
    ops.extend(_detect_renames(local, remote, base, can_push, can_pull,
                               md5_of, handled, reasons))

    # ---- pass 2: files ---------------------------------------------
    for rel in sorted(set(local) | set(remote) | set(base)):
        if rel in handled or rel == "":
            continue
        l, r, b = local.get(rel), remote.get(rel), base.get(rel)
        if not allow_deletes and b:
            if not l and r and b.get("deleted_local"):
                continue  # local deletion already acknowledged
            if l and not r and b.get("deleted_remote"):
                continue  # remote deletion already acknowledged
        if l and l["kind"] != "file":
            continue  # dirs handled in pass 3
        if r and r["kind"] == "dir" and not l:
            continue  # stale base/remote dirs handled in pass 3
        op = _decide_file(rel, l, r, b, can_push, can_pull, policy, md5_of,
                          force, reasons)
        if op:
            ops.extend(op)

    # ---- pass 3: directories ---------------------------------------
    ops.extend(_decide_dirs(local, remote, base, can_push, can_pull, handled,
                            allow_deletes, reasons))
    ops = _filter_deletes(ops, allow_deletes, reasons)

    # order: mkdirs first, renames, then files ops, deletions last
    def rank(op):
        name = op[0]
        if name in ("mkdir_local", "mkdir_remote"):
            return 0
        if name.startswith("rename"):
            return 1
        if name in ("delete_local", "delete_remote", "rmdir_local", "rmdir_remote"):
            return 3
        return 2  # upload / download / conflict ops

    return sorted(ops, key=rank)


def _decide_file(rel, l, r, b, can_push, can_pull, policy, md5_of,
                 force=False, reasons=None):
    """Ops for one file path. Returns a list (possibly empty)."""
    l_exists, r_exists, b_exists = bool(l), bool(r), bool(b)

    # base md5 may need (re)computing when size/mtime changed
    l_md5 = None
    def local_md5():
        nonlocal l_md5
        if l_md5 is None and l_exists:
            l_md5 = md5_of(rel)
        return l_md5

    def local_changed():
        if not b_exists:
            return True
        if not l_exists:
            return True
        if force or l["size"] != b.get("local_size") or \
                _mtime_neq(l["mtime"], b.get("local_mtime")):
            return local_md5() != b.get("local_md5") or \
                (local_md5() is None and b.get("local_md5") is None)
        return False

    def remote_changed():
        if not b_exists:
            return True
        if not r_exists:
            return True
        return r["md5"] != b.get("remote_md5") or \
            (r["md5"] is None and b.get("remote_md5") is None and
             r["version"] != b.get("remote_version"))

    # ---- deleted on both sides --------------------------------------
    if not l_exists and not r_exists:
        if b_exists:
            _note_reason(reasons, rel, "deleted on both sides")
            return [("forget", rel)]          # just drop the base row
        return []

    # ---- new on one side --------------------------------------------
    if l_exists and not r_exists and not b_exists:
        _note_reason(reasons, rel, "new local file")
        return [("upload", rel)] if can_push else []
    if not l_exists and r_exists and not b_exists:
        if r["kind"] == "gdoc":
            if r.get("export"):
                _note_reason(reasons, rel, "new Google doc on Drive (materializing the export)")
                return [("download", rel)] if can_pull else []
            _note_reason(reasons, rel, "Google-native doc: record only (no export format)")
            return [("record_gdoc", rel)]
        _note_reason(reasons, rel, "new file on Drive")
        return [("download", rel)] if can_pull else []

    # ---- deletions (exists in base, gone on one side) ----------------
    if not l_exists and r_exists:                       # local deleted
        if r["kind"] == "gdoc":
            if r.get("export") and b.get("kind") == "gdoc":
                # legacy record-only row (older version): materialize now
                return [("download", rel)] if can_pull else []
            if not (r.get("export") and b.get("kind") == "file"):
                return []          # unconvertible doc: nothing to sync
            # materialized export: behave like a real file below
        if not remote_changed():
            _note_reason(reasons, rel, "local file deleted — propagating to Drive")
            return [("delete_remote", rel)] if can_push else []
        # remote changed since base while local was deleted
        return _conflict_delete_local(rel, policy, can_pull, reasons)
    if l_exists and not r_exists:                       # remote deleted
        if not local_changed():
            if can_pull:
                _note_reason(reasons, rel, "deleted on Drive — propagating locally")
                return [("delete_local", rel)]
            _note_reason(reasons, rel, "deleted on Drive; pull is off — re-uploading the local copy")
            return [("upload", rel)]
        # local changed since base while remote was deleted
        return _conflict_delete_remote(rel, policy, can_push, can_pull, reasons)

    # ---- present on both sides ---------------------------------------
    if r["kind"] == "gdoc":
        if not r.get("export"):
            _note_reason(reasons, rel, "local file sits where a Google doc would export")
            return [("skip_gdoc_collision", rel)]   # never touches native docs
        if not l.get("folded"):
            # a user-made file sits where the export would go — never
            # overwrite it, even when an old base row exists
            _note_reason(reasons, rel, "a local file occupies the Google doc's export path")
            return [("skip_gdoc_collision", rel)]
        if not b_exists or b.get("kind") != "file":
            # tracked but not yet materialized: export (the fold is proof
            # this local file is ours)
            _note_reason(reasons, rel, "materializing the Google doc export")
            return [("download", rel)] if can_pull else []
        # materialized export: falls through to the normal three-way compare

    lc, rc = local_changed(), remote_changed()
    if not lc and not rc:
        return []
    if lc and not rc:
        _note_reason(reasons, rel, "local changes since last sync")
        return [("upload", rel)] if can_push else []
    if rc and not lc:
        _note_reason(reasons, rel, "changed on Drive since last sync")
        return [("download", rel)] if can_pull else []

    # both changed
    if can_push and can_pull and r["md5"] and local_md5() == r["md5"]:
        _note_reason(reasons, rel, "identical change on both sides")
        return [("reconcile", rel)]          # same content, just refresh base
    if not (can_push and can_pull):
        _note_reason(reasons, rel, "changed on both sides; the direction is one-way")
        return [("upload", rel)] if can_push else [("download", rel)]
    return _conflict_both_changed(rel, l, r, policy, local_md5(), reasons)


# Worth retrying: 5xx/429 replies, throttles, and transport breaks.
# Auth (401/403), not-found, quota-exceeded and local OS errors are fatal —
# retrying the same op cannot help (401 refreshes are handled inside the
# Drive client already).
_TRANSIENT_RE = re.compile(
    r"->\s*5\d\d|->\s*429|failed:\s*(Connection|Timeout|Read)|"
    r"(ChunkedEncoding|ConnectionError|ReadTimeout|ProtocolError|"
    r"SSLError)|(rate limit|backend error|internal error|bad gateway|"
    r"service unavailable)", re.I)

# Exception type names (requests / urllib3) that are transport-shaped.
_TRANSIENT_EXC = {"ChunkedEncodingError", "ConnectionError", "Timeout",
                  "ReadTimeout", "ProtocolError", "SSLError"}


def is_transient_transfer_error(exc):
    """True when retrying this transfer op is worth one more attempt."""
    if isinstance(exc, OSError):
        return False
    if type(exc).__name__ in _TRANSIENT_EXC:
        return True
    return bool(_TRANSIENT_RE.search(str(exc)))


def _note_reason(reasons, rel, text):
    """Record why the planner emitted an op for rel (None dict = no-op)."""
    if reasons is not None and rel not in reasons:
        reasons[rel] = text


def _mtime_neq(a, b):
    if a is None or b is None:
        return True
    return abs(float(a) - float(b)) > 1e-6


def _conflict_both_changed(rel, l, r, policy, local_md5, reasons=None):
    if policy == "local":
        _note_reason(reasons, rel, "conflict — policy 'local' keeps the local version")
        return [("upload", rel)]
    if policy == "remote":
        _note_reason(reasons, rel, "conflict — policy 'remote' keeps the Drive version")
        return [("download", rel)]
    if policy == "newer":
        newer_local = float(l["mtime"] or 0) >= float(r["mtime"] or 0)
        _note_reason(reasons, rel,
                     "conflict — policy 'newer' keeps the " +
                     ("local" if newer_local else "Drive") + " version (newer)")
        return [("upload" if newer_local else "download", rel)]
    _note_reason(reasons, rel, "conflict — both versions kept (policy 'both')")
    return [("conflict_both", rel)]


def _conflict_delete_local(rel, policy, can_pull, reasons=None):
    """Local file deleted, remote edited since base."""
    if policy == "local":
        _note_reason(reasons, rel, "local deleted, remote edited — policy 'local' deletes on Drive")
        return [("delete_remote", rel)]
    if policy == "both":
        _note_reason(reasons, rel, "local deleted, remote edited — policy 'both' restores the Drive copy")
        return [("restore_local_conflict", rel)]
    if can_pull:
        _note_reason(reasons, rel, "local deleted, remote edited — keeping the edited Drive copy")
        return [("download", rel)]
    _note_reason(reasons, rel, "local deleted, remote edited; pull off — deleting on Drive")
    return [("delete_remote", rel)]


def _conflict_delete_remote(rel, policy, can_push, can_pull, reasons=None):
    """Remote file deleted, local edited since base."""
    if policy == "remote":
        _note_reason(reasons, rel, "deleted on Drive, local edited — policy 'remote' deletes locally")
        return [("delete_local", rel)]
    if policy == "both":
        _note_reason(reasons, rel, "deleted on Drive, local edited — policy 'both' keeps both versions")
        return [("upload_conflict_copy", rel)]
    # 'local' and 'newer' both keep the edited local copy (no data loss)
    if can_push:
        _note_reason(reasons, rel, "deleted on Drive, local edited — keeping the edited local copy")
        return [("upload", rel)]
    _note_reason(reasons, rel, "deleted on Drive, local edited; push off — deleting the local copy")
    return [("delete_local", rel)]


def _detect_renames(local, remote, base, can_push, can_pull, md5_of, handled,
                    reasons=None):
    """Detect file renames on either side; emits rename ops."""
    ops = []
    used_new = set()

    # local renames: base entry missing locally + new local file with same md5
    gone_local = {rel: b for rel, b in base.items()
                  if b["kind"] == "file" and rel not in local and rel in remote}
    candidates = {rel: l for rel, l in local.items()
                  if l["kind"] == "file" and rel not in base}
    for old_rel, b in sorted(gone_local.items()):
        match = None
        for new_rel, l in sorted(candidates.items()):
            if new_rel in used_new or l["size"] != b.get("local_size"):
                continue
            if md5_of(new_rel) == b.get("local_md5"):
                match = new_rel
                break
        if match and can_push:
            _note_reason(reasons, old_rel, f"renamed locally — now {match}")
            ops.append(("rename_local", old_rel, match))
            used_new.add(match)
            handled.add(old_rel)
            handled.add(match)

    # remote renames: base entry missing remotely + new remote file same md5.
    # Google-native docs are excluded on purpose: any Drive change (rename
    # included) bumps the file version, so a stable-version match never
    # holds; renames of exports surface as a fresh export under the new name
    gone_remote = {rel: b for rel, b in base.items()
                   if b["kind"] == "file" and rel not in remote and rel in local}
    new_remote = {rel: r for rel, r in remote.items()
                  if r["kind"] == "file" and rel not in base}
    for old_rel, b in sorted(gone_remote.items()):
        match = None
        for new_rel, r in sorted(new_remote.items()):
            if new_rel in used_new or r["md5"] != b.get("remote_md5") or \
                    not r["md5"]:
                continue
            match = new_rel
            break
        if match and can_pull:
            _note_reason(reasons, old_rel, f"renamed on Drive — now {match}")
            ops.append(("rename_remote", old_rel, match))
            used_new.add(match)
            handled.add(old_rel)
            handled.add(match)
    return ops


def _decide_dirs(local, remote, base, can_push, can_pull, handled,
                 allow_deletes=True, reasons=None):
    ops = []
    for rel in sorted(set(local) | set(remote) | set(base)):
        if rel in handled or rel == "":
            continue
        l, r, b = local.get(rel), remote.get(rel), base.get(rel)
        l_dir = l and l["kind"] == "dir"
        r_dir = r and r["kind"] == "dir"
        b_dir = b and b.get("kind") == "dir"
        if not allow_deletes and b:
            if not l and r_dir and b.get("deleted_local"):
                continue  # local dir deletion already acknowledged
            if not r and l_dir and b.get("deleted_remote"):
                continue  # remote dir deletion already acknowledged
        if b and not b_dir and (l_dir or r_dir):
            _note_reason(reasons, rel, "path changed between file and folder")
            ops.append(("forget", rel))  # file replaced by a folder; drop stale row
            continue
        if l_dir and not r_dir and not b_dir and can_push:
            _note_reason(reasons, rel, "new local folder")
            ops.append(("mkdir_remote", rel))
        elif r_dir and not l_dir and not b_dir and can_pull:
            _note_reason(reasons, rel, "new folder on Drive")
            ops.append(("mkdir_local", rel))
        elif not l and b_dir and r_dir:
            # local dir removed: propagate when pushing, else keep base
            if can_push:
                _note_reason(reasons, rel, "local folder removed — propagating to Drive")
                ops.append(("rmdir_remote", rel))
            else:
                _note_reason(reasons, rel, "local folder removed; push off — leaving the Drive copy untracked")
                ops.append(("forget", rel))
        elif not r and b_dir and l_dir:
            if can_pull:
                _note_reason(reasons, rel, "folder removed on Drive — propagating locally")
                ops.append(("rmdir_local", rel))
            else:
                _note_reason(reasons, rel, "folder removed on Drive; pull off — leaving the local copy untracked")
                ops.append(("forget", rel))
        elif not l and not r and b_dir:
            _note_reason(reasons, rel, "folder removed on both sides")
            ops.append(("forget", rel))
    return ops


def _filter_deletes(ops, allow_deletes, reasons=None):
    """Deletes-off mode: swap destructive ops for acknowledgements.

    The file the user still has stays exactly where it is; the absence on
    the other side is recorded as a tombstone so the decision does not
    repeat on every pass.
    """
    if allow_deletes:
        return ops
    out = []
    for op in ops:
        if op[0] in ("delete_remote", "rmdir_remote"):
            out.append(("ack_local_delete", op[1]))   # local gone, keep remote
            if reasons is not None:
                reasons[op[1]] = (
                    "local file deleted — deletes are off, Drive copy kept")
        elif op[0] in ("delete_local", "rmdir_local"):
            out.append(("ack_remote_delete", op[1]))  # remote gone, keep local
            if reasons is not None:
                reasons[op[1]] = (
                    "deleted on Drive — deletes are off, local copy kept")
        else:
            out.append(op)
    return out


# ---------------------------------------------------------------- apply
class _AtomicInt:
    """Counter that survives concurrent `+= 1` from transfer worker threads.

    Behaves like an int for comparisons, arithmetic and f-strings so the
    rest of the engine (and the tests) keep working unchanged.
    """

    __slots__ = ("_v", "_lock")

    def __init__(self, v=0):
        self._v = v
        self._lock = threading.Lock()

    def __iadd__(self, n):
        with self._lock:
            self._v += n
        return self

    def __int__(self):
        return self._v

    def __eq__(self, other):
        return int(self) == (other if isinstance(other, int) else int(other))

    def __lt__(self, other):
        return int(self) < int(other)

    def __le__(self, other):
        return int(self) <= int(other)

    def __gt__(self, other):
        return int(self) > int(other)

    def __ge__(self, other):
        return int(self) >= int(other)

    def __hash__(self):
        return hash(int(self))

    def __str__(self):
        return str(self._v)

    def __repr__(self):
        return str(self._v)


class SyncStats:
    COUNTERS = ("pushed", "pulled", "deleted", "renamed", "conflicts", "errors")

    def __init__(self):
        self.pushed = _AtomicInt()
        self.pulled = _AtomicInt()
        self.deleted = _AtomicInt()
        self.renamed = _AtomicInt()
        self.conflicts = _AtomicInt()
        self.errors = _AtomicInt()

    def merge(self, other):
        """Accumulate another pass's counters into this one (worker totals)."""
        for name in self.COUNTERS:
            counter = getattr(self, name)
            counter += int(getattr(other, name))
        return self


# ================================================================ scanner
@dataclass
class ScanResult:
    """What one pair's scan gathered — the planner's entire input.

    error is None on success; on a fatal scan problem it carries the
    message and the maps stay empty. stats accumulates scan-phase error
    counts so sync_pair can report them exactly as before.
    """

    local: dict = field(default_factory=dict)
    remote: dict = field(default_factory=dict)
    base: dict = field(default_factory=dict)
    fold: dict = field(default_factory=dict)
    local_root: str = ""
    md5_of: object = None
    delta_used: bool = False
    stats: "SyncStats" = field(default_factory=SyncStats)
    error: str = None


def scan_pair_state(store, cfg, pair, drive, force_full=False, changes=None,
                    cleanup=True):
    """Scan one pair: local tree, Drive state (delta feed or full), base.

    The scanner owns everything input-shaped: path checks, the empty-base
    refusal, delta-vs-full choice, ignore/include filtering, symlink
    policy, case canonicalization, Google-doc export folding and the pass-
    local md5 cache. It decides nothing — that is build_plan()'s job.
    cleanup=False (preview) never writes the base table and never logs.
    """
    log = store.log if cleanup else (lambda *a, **k: None)
    stats = SyncStats()
    local_root = str(pair["local_path"])
    if not os.path.isdir(local_root):
        stats.errors += 1
        return ScanResult(local_root=local_root, stats=stats,
                          error=f"local path missing: {local_root}")

    patterns = list(cfg.get("ignore_patterns", []))
    include = parse_include_patterns(pair["include_patterns"])
    export_map = resolve_export_formats(cfg)
    base_rows = store.base_entries(pair["id"])
    # A lost/emptied base snapshot makes both sides look brand-new: the
    # three-way compare would then "resolve" every path as a conflict (and
    # with policy 'newer' silently copy one tree over the other). Refuse
    # unless the user explicitly asks for a rebuild with --full.
    marker = f"pair_synced:{pair['id']}"
    previous = store.kv_get(marker)
    try:
        previous_count = int(previous) if previous else 0
    except (TypeError, ValueError):
        previous_count = 0
    if not base_rows and previous_count > 0 and not force_full:
        stats.errors += 1
        return ScanResult(local_root=local_root, stats=stats, error=(
            "base snapshot is empty but this pair synced before — "
            "refusing to reconcile from scratch (run "
            "'drivesync sync --full --pair "
            f"{pair['id']}' once to rebuild it)"))

    remote = None
    delta_used = False
    if changes is not None and not force_full:
        try:
            remote = scan_remote_delta(pair, base_rows, export_map, patterns,
                                       changes, include=include)
            delta_used = True
        except DeltaUnsupported:
            remote = None
        except Exception as exc:
            log(pair["id"], "info", "",
                f"delta scan failed ({exc}); doing a full scan")
            remote = None
    if remote is None:
        try:
            remote = scan_remote(drive, pair["remote_id"], patterns, export_map,
                                 include=include)
        except Exception as exc:
            stats.errors += 1
            return ScanResult(local_root=local_root, stats=stats,
                              error=f"Drive listing failed: {exc}")

    local = scan_local(local_root, patterns, include=include,
                       symlink_policy=pair["symlink_policy"] or "skip")
    base = base_map(base_rows)
    if pair["case_insensitive"]:
        def _case_notify(rel, source):
            log(pair["id"], "case-collision", rel,
                f"two {'Drive files' if source == 'remote' else 'paths'} "
                "differ only in letter case; this pair runs in "
                "case-insensitive mode and can track only one of them — "
                "the other was left out of this pass", level="error")
        local, remote, base, shadowed = canonicalize_case(
            local, remote, base, notify=_case_notify)
        if cleanup:
            for rel in shadowed:
                store.base_delete(pair["id"], rel)
                store.log(pair["id"], "case-cleanup", rel,
                          "stale duplicate base row (same path, different "
                          "case) removed")

    # exported Google-native docs live under rel+ext locally: align the maps
    fold = fold_exports(local, remote, base, export_map)

    md5_cache = {}

    def md5_of(rel):
        # rename detection and change detection can ask for the same file
        # several times in one pass; a large rename set was O(n²) hashes
        if rel in md5_cache:
            return md5_cache[rel]
        path = os.path.join(local_root, fold.get(rel, rel))
        digest = None
        if os.path.isfile(path):
            try:
                digest = md5_file(path)
            except OSError:
                digest = None
        md5_cache[rel] = digest
        return digest

    return ScanResult(local=local, remote=remote, base=base, fold=fold,
                      local_root=local_root, md5_of=md5_of,
                      delta_used=delta_used, stats=stats, error=None)


# ================================================================ planner
@dataclass
class PlannedOp:
    """One planned operation with its explanation.

    The plan is data, not code: serializable, previewable, and the
    executor's whole input. reason is the truthful "why" recorded by
    decide(); size is the transfer payload when known.
    """

    name: str
    rel: str
    target: str = None          # rename destination
    reason: str = ""
    size: int = None
    destructive: bool = False
    transfer: bool = False

    def row(self):
        """[name, rel, target] — the positional form previews render."""
        return [self.name, self.rel] + ([self.target] if self.target else [])


@dataclass
class Plan:
    """The full work program for one sync pass."""

    pair_id: int
    ops: list                    # [PlannedOp], apply-ordered
    counts: dict                 # op name -> n
    blocked: int                 # destructive ops the safety gate removed
    error: str = None            # fatal scan error (ops empty when set)
    scan: dict = field(default_factory=dict)   # local_n, remote_n, base_n, delta
    policy: str = ""
    direction: str = ""
    local_root: str = ""
    local_map: dict = field(default_factory=dict)   # executor inputs
    remote_map: dict = field(default_factory=dict)
    fold: dict = field(default_factory=dict)
    scan_stats: "SyncStats" = field(default_factory=SyncStats)


def _planned_op(op, reasons, local, remote, fold):
    """PlannedOp from a decided op tuple, with reason and transfer size."""
    name, rel = op[0], op[1]
    target = op[2] if len(op) > 2 else None
    destructive = name in DESTRUCTIVE_OPS
    transfer = name in TRANSFER_OPS
    size = None
    if transfer:
        source = local.get(rel) if name == "upload" else remote.get(rel)
        size = (source or {}).get("size")
    return PlannedOp(name=name, rel=rel, target=target,
                     reason=reasons.get(rel, ""),
                     size=size, destructive=destructive, transfer=transfer)


def build_plan(store, cfg, pair, drive, force_full=False, changes=None,
               cleanup=True):
    """Scan the pair and plan the pass: decide() + ordering + safety gate.

    cleanup=True (real passes) writes the safety-valve holds and the
    recovery history; cleanup=False (preview) is strictly read-only and
    only reports `blocked`.
    """
    scan = scan_pair_state(store, cfg, pair, drive, force_full=force_full,
                           changes=changes, cleanup=cleanup)
    plan = Plan(pair_id=pair["id"], ops=[], counts={}, blocked=0,
                error=scan.error, policy=pair["conflict_policy"]
                or cfg.get("conflict_policy", "newer"),
                direction=pair["direction"], local_root=scan.local_root,
                local_map=scan.local, remote_map=scan.remote, fold=scan.fold,
                scan_stats=scan.stats,
                scan={"local_n": len(scan.local), "remote_n": len(scan.remote),
                      "base_n": len(scan.base), "delta_used": scan.delta_used})
    if scan.error:
        return plan

    reasons = {}
    ops = decide(scan.local, scan.remote, scan.base, pair["direction"],
                 plan.policy, scan.md5_of, force=force_full,
                 allow_deletes=bool(pair["allow_deletes"]), reasons=reasons)

    # safety gate: refuse to wipe out more files than allowed in one pass.
    # A tripped gate is not just logged — the held deletions are stored (kv)
    # so the Recovery page can ask the user: allow them once, or keep every
    # file (deletes off). A later pass back under the limit releases the
    # hold. Preview runs read-only: it reports `blocked` but never writes.
    blocked = 0
    limit = int(cfg.get("delete_safety_limit", 10))
    destructive = [op for op in ops if op[0] in DESTRUCTIVE_OPS]
    pending_key = f"pending_deletes:{pair['id']}"
    approved = store.kv_get(f"deletes_approved:{pair['id']}") == "once"
    rlog = store.recovery_log if cleanup else (lambda *a, **k: None)
    log = store.log if cleanup else (lambda *a, **k: None)
    if limit and len(destructive) > limit and not approved:
        paths = sorted({op[1] for op in destructive})
        previous = store.kv_get(pending_key)     # None = a brand-new incident
        pending = {
            "count": len(destructive),
            "limit": limit,
            "kinds": {},
            "paths": paths[:200],      # the full plan stays in the preview
            "more": max(0, len(paths) - 200),
        }
        for op in destructive:
            pending["kinds"][op[0]] = pending["kinds"].get(op[0], 0) + 1
        store.kv_set(pending_key, json.dumps(pending))
        log(pair["id"], "safety-stop", "",
            f"{len(destructive)} deletions blocked — exceeds "
            f"delete_safety_limit={limit}; confirm them on the Recovery "
            "page (or raise the limit in Settings) if this is expected",
            level="error")
        if cleanup and not previous:
            rlog(pair["id"], "", "safety_stop",
                 f"delete_safety_limit={limit} exceeded",
                 f"{len(destructive)} deletions held for confirmation: " +
                 ", ".join(paths[:5]) + ("…" if len(paths) > 5 else ""))
        ops = [op for op in ops if op[0] not in DESTRUCTIVE_OPS]
        scan.stats.errors += 1
        blocked = len(destructive)
    elif store.kv_get(pending_key):
        # the risky pass resolved itself (files came back, scope changed):
        # release the hold so the Recovery page stops asking
        store.kv_delete(pending_key)
        rlog(pair["id"], "", "pending_cleared",
             f"destructive ops back under delete_safety_limit={limit}",
             "the held deletions are no longer part of the plan")

    plan.blocked = blocked
    plan.ops = [_planned_op(op, reasons, scan.local, scan.remote, scan.fold)
                for op in ops]
    counts = {}
    for p in plan.ops:
        counts[p.name] = counts.get(p.name, 0) + 1
    plan.counts = counts
    return plan


def preview_pair(store, cfg, pair, drive, force_full=False, changes=None):
    """What the next sync pass WOULD do, without touching anything.

    Runs the scanner + planner (no transfers, no deletions, no base
    writes, no activity-log rows) and returns a dict: {"ops": [...],
    "counts": {...}, "error": None|msg, "blocked": n, "totals": {...}} —
    plus the Phase 3.1 explanations: "reasons" (per op: why it is planned),
    "sizes" (transfer payloads when known) and "destructive" flags, each
    aligned with ops, and "scan" metadata (file counts, delta vs full).
    Ops arrive already ordered (mkdirs, renames, transfers, deletions
    last) — exactly the order a real pass would apply them in.
    """
    plan = build_plan(store, cfg, pair, drive, force_full=force_full,
                      changes=changes, cleanup=False)
    counts = dict(plan.counts)
    return {
        "pair_id": pair["id"], "error": plan.error,
        "ops": [p.row() for p in plan.ops],
        "reasons": [p.reason for p in plan.ops],
        "sizes": [p.size for p in plan.ops],
        "destructive": [p.destructive for p in plan.ops],
        "counts": counts, "blocked": plan.blocked,
        "scan": plan.scan,
        "totals": {
            "uploads": counts.get("upload", 0),
            "downloads": counts.get("download", 0),
            "deletions": sum(n for k, n in counts.items()
                             if k in DESTRUCTIVE_OPS),
            "renames": counts.get("rename_local", 0)
                       + counts.get("rename_remote", 0),
            "other": sum(n for k, n in counts.items()
                         if k not in TRANSFER_OPS and k not in DESTRUCTIVE_OPS
                         and not k.startswith("rename")),
        },
    }


# ================================================================ executor
@dataclass
class ExecReport:
    """Per-op outcomes of applying a Plan — 3.2 resumability reads this."""

    stats: SyncStats
    ok: bool
    failures: list               # [(op_name, rel, error)] for failed ops
    blocked: int                 # destructive ops the planner held back
    retries: int = 0             # transient failures retried in-pass


def execute_plan(store, cfg, pair, drive, plan, transfer_callback=None):
    """Apply a Plan (pre-ops -> parallel transfers -> deletions last).

    One commit per op: a failed op is recorded (report.failures + error
    event + failed transfer telemetry) and the pass continues — partial
    progress always lands. Updates the pair-synced marker and spends a
    one-time deletion approval when the pass ran with one.
    """
    stats = SyncStats()
    failures = []
    remote, local, fold = plan.remote_map, plan.local_map, plan.fold
    local_root = plan.local_root

    ctx = _ApplyCtx(store, cfg, pair, drive, local_root, remote, local, stats,
                    fold)

    def transfer_event(op, state, error=None):
        if transfer_callback is None:
            return
        name, rel = op[0], op[1]
        source = local.get(rel) if name == "upload" else remote.get(rel)
        item = {
            "id": f"{pair['id']}:{name}:{rel}",
            "pair_id": pair["id"],
            "direction": name,
            "path": rel,
            "size": (source or {}).get("size"),
            "status": state,
            "error": str(error) if error else None,
            "updated_at": datetime.now(timezone.utc).timestamp(),
        }
        try:
            transfer_callback(item)
        except Exception:
            pass  # UI telemetry must never interrupt a sync operation

    retries = [0]

    def apply_one(op):
        is_transfer = op[0] in TRANSFER_OPS
        if is_transfer:
            transfer_event(op, "uploading" if op[0] == "upload"
                           else "downloading")
        # transient failures (5xx, throttles, transport breaks) get another
        # attempt with a short backoff; resumable sessions and .part files
        # make every retry cheap — only the missing bytes move
        attempts = 1 + (transfer_retries if is_transfer else 0)
        for attempt in range(attempts):
            try:
                # one commit per op (base row + marker/activity rows) instead
                # of one per write: a first sync writes thousands of rows
                with store.batch():
                    result = ctx.apply(op)
                if is_transfer:
                    if result is False:
                        rows = store.recent_events(limit=1, level="error",
                                                   pair_id=pair["id"])
                        detail = (rows[0]["message"] if rows
                                  else "transfer failed")
                        transfer_event(op, "failed", detail)
                    else:
                        transfer_event(op, "completed")
                break
            except Exception as exc:
                if is_transfer and attempt < attempts - 1 and \
                        is_transient_transfer_error(exc):
                    retries[0] += 1
                    transfer_event(op, "retrying", exc)
                    store.log(pair["id"], "info",
                              op[1] if len(op) > 1 else "",
                              f"{op[0]} failed ({exc}); retrying")
                    time.sleep(min(30.0, retry_base * (2 ** attempt)))
                    continue
                if is_transfer:
                    transfer_event(op, "failed", exc)
                stats.errors += 1
                failures.append((op[0], op[1] if len(op) > 1 else "",
                                 str(exc)))
                store.log(pair["id"], "error", op[1] if len(op) > 1 else "",
                          f"{op[0]} failed: {exc}", level="error")
                break

    # Same rank order as the sequential loop: mkdirs/renames and per-file
    # bookkeeping first, then file transfers (parallel), deletions last.
    pre, transfers, post = [], [], []
    for p in plan.ops:
        op = p.row()
        if p.transfer:
            transfers.append(op)
        elif p.destructive:
            post.append(op)
        else:
            pre.append(op)
    for op in pre:
        apply_one(op)
    for op in transfers:
        transfer_event(op, "queued")
    workers = max(1, int(cfg.get("transfer_concurrency", 4)))
    try:
        transfer_retries = max(0, int(cfg.get("transfer_retries", 2)))
    except (TypeError, ValueError):
        transfer_retries = 2
    try:
        retry_base = max(0.0, float(cfg.get("transfer_retry_base", 2.0)))
    except (TypeError, ValueError):
        retry_base = 2.0
    if workers == 1 or len(transfers) < 2:
        for op in transfers:
            apply_one(op)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(apply_one, transfers))
    for op in post:
        apply_one(op)
    # remember what a healthy base looked like: an empty snapshot after a
    # non-empty one means state loss, not "everything was deleted"
    marker = f"pair_synced:{pair['id']}"       # same key the scanner read
    store.kv_set(marker, str(len(store.base_entries(pair["id"]))))
    # a one-time approval is spent by the pass that runs it: record what it
    # actually allowed and release the Recovery-page hold
    approved_key = f"deletes_approved:{pair['id']}"
    if store.kv_get(approved_key):
        store.kv_delete(approved_key)
        store.kv_delete(f"pending_deletes:{pair['id']}")
        store.recovery_log(
            pair["id"], "", "deletions_allowed",
            "user confirmed on the Recovery page (one pass)",
            f"{int(stats.deleted)} deletion(s) applied")
    return ExecReport(stats=stats, ok=stats.errors == 0 and plan.blocked == 0,
                      failures=failures, blocked=plan.blocked,
                      retries=retries[0])


# ============================================================ orchestration
def sync_pair(store, cfg, pair, drive, force_full=False, changes=None,
              transfer_callback=None):
    """Run one sync pass for a pair row: scan -> plan -> execute.

    Returns (SyncStats, ok). changes: a Drive changes feed (list of
    {'fileId','removed','file'}) captured since the previous pass. When
    given, the remote map is rebuilt from the base snapshot + the feed
    instead of re-listing the whole tree — one-file edits in large pairs
    cost 1-2 API calls instead of one per folder. Structural feed changes
    fall back to a full scan automatically.
    """
    plan = build_plan(store, cfg, pair, drive, force_full=force_full,
                      changes=changes)
    if plan.error:
        store.log(pair["id"], "error", message=plan.error, level="error")
        return plan.scan_stats, False

    report = execute_plan(store, cfg, pair, drive, plan,
                          transfer_callback=transfer_callback)
    # the safety gate counts as a pass error, exactly as before the split
    report.stats.errors += 1 if plan.blocked else 0
    return report.stats, report.ok


class _ApplyCtx:
    """Executes decided ops against the local FS, Drive and the base table."""

    def __init__(self, store, cfg, pair, drive, local_root, remote, local,
                 stats, fold=None):
        self.store, self.cfg, self.pair = store, cfg, pair
        self.drive = drive
        self.root = local_root
        self.remote_map = dict(remote)
        self.local_map = local
        self.stats = stats
        self.fold = dict(fold or {})
        self.pid = pair["id"]
        # one trash stamp per pass: trash/<pair_id>/<stamp>/<rel_path>
        self.trash_stamp = datetime.now(timezone.utc).strftime(CONFLICT_TAG)
        # remote id per rel (known dirs/files), updated as we create items
        self.ids = {rel: it["id"] for rel, it in remote.items() if it.get("id")}
        try:
            self.history = max(0, int(cfg.get("history_versions", 10) or 0))
        except (TypeError, ValueError):
            self.history = 10

    # -- version history ---------------------------------------------
    def _history_dir(self, rel):
        root = os.path.join(str(app_dir()), "history", str(self.pid),
                            rel.replace("\\", "/"))
        os.makedirs(root, exist_ok=True)
        return root

    def keep_old_version(self, rel):
        """Copy the current local file into the version history before a pull.

        A pull overwrites the bytes on disk, and Drive keeps no trace of what
        used to be here — this is the one overwrite the sync could not
        undo. Versions live under <app dir>/history/<pair>/<rel>/ named
        "<UTC stamp>-<name>", capped at history_versions per file (oldest
        pruned), 0 disables the feature entirely.
        """
        if not self.history:
            return
        path = self.lpath(rel)
        if not os.path.isfile(path):
            return
        # microsecond stamp: lexicographic order == chronological order, so
        # pruning by name is exact and same-second pulls stay unique
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        hist_dir = self._history_dir(rel)
        name = os.path.basename(rel.replace("\\", "/"))
        target = os.path.join(hist_dir, f"{stamp}-{name}")
        i = 0
        while os.path.exists(target):       # clock went backwards?
            i += 1
            target = os.path.join(hist_dir, f"{stamp}-{i:02d}-{name}")
        try:
            shutil.copy2(path, target)
        except OSError:
            return          # history is best-effort; never fail the sync for it
        self._prune_history(hist_dir)
        self.store.log(self.pid, "history-keep", rel,
                       f"previous version kept as {os.path.basename(target)}")

    def _prune_history(self, hist_dir):
        try:
            versions = sorted(
                (f for f in os.listdir(hist_dir)
                 if os.path.isfile(os.path.join(hist_dir, f))),
                reverse=True)                   # stamp-prefixed names
        except OSError:
            return
        for old in versions[self.history:]:
            try:
                os.remove(os.path.join(hist_dir, old))
            except OSError:
                pass

    # -- path/id helpers -------------------------------------------
    def _base(self, rel):
        """Base row for rel as a plain dict (sqlite3.Row lacks .get)."""
        row = self.store.base_get(self.pid, rel)
        return dict(row) if row else None

    def _remote_meta(self, rel, meta=None, mime_fallback=None):
        """remote_size/mtime/mime kwargs for base_put.

        Prefers the scanned remote item; an API response (meta) overrides
        with fresh values after a mutation. Base rows carry the remote
        metadata the delta scan needs to synthesize unchanged items.
        """
        r = self.remote_map.get(rel) or {}
        size, mtime, mime = r.get("size"), r.get("mtime"), r.get("mime")
        if meta:
            if meta.get("size"):
                try:
                    size = int(meta["size"])
                except (TypeError, ValueError):
                    pass
            if meta.get("modifiedTime"):
                mtime = parse_remote_mtime(meta["modifiedTime"])
            if meta.get("mimeType"):
                mime = meta["mimeType"]
        return {"remote_size": size, "remote_mtime": mtime,
                "remote_mime": mime or mime_fallback}

    def lpath(self, rel):
        """Local path for rel — folded native docs resolve to their rel+ext."""
        return safe_join(self.root, self.fold.get(rel, rel),
                         allow_symlink_escape=True)

    def parent(self, rel):
        return rel.rsplit("/", 1)[0] if "/" in rel else ""

    def parent_remote_id(self, rel):
        parent = self.parent(rel)
        if parent == "":
            return self.pair["remote_id"]
        fid = self.ids.get(parent)
        if fid:
            return fid
        # parent not created yet (nested new dirs): create the chain
        parts = parent.split("/")
        cur = ""
        pid = self.pair["remote_id"]
        for part in parts:
            cur = f"{cur}/{part}" if cur else part
            fid = self.ids.get(cur)
            if not fid:
                created = self.drive.mkdir(part, pid)
                fid = created["id"]
                self.ids[cur] = fid
                self.store.base_put(self.pid, cur, "dir", remote_id=fid,
                                    remote_version=str(created.get("version", "")),
                                    remote_mime=FOLDER_MIME)
                self.store.log(self.pid, "mkdir-remote", cur, "folder created on Drive")
            pid = fid
        return pid

    def now_tag(self):
        return datetime.now(timezone.utc).strftime(CONFLICT_TAG)

    def conflict_name(self, rel):
        d, name = self.parent(rel), rel.rsplit("/", 1)[-1]
        r = self.remote_map.get(rel) or {}
        if r.get("kind") == "gdoc" and r.get("export"):
            ext = r["export"][1]
            if not name.lower().endswith(ext):
                name += ext     # conflict copies are real files: keep the ext
        stem, dot, ext = name.partition(".") if "." in name else (name, "", "")
        new = f"{stem}.conflict-{self.now_tag()}{dot}{ext}" if dot else \
            f"{name}.conflict-{self.now_tag()}"
        return f"{d}/{new}" if d else new

    # ---- resumable push (Phase 3.2) --------------------------------
    def _resumable_min(self):
        """Files at or above this size push through resumable sessions."""
        try:
            return max(0, int(self.cfg.get("resumable_threshold",
                                           8 << 20)))
        except (TypeError, ValueError):
            return 8 << 20

    def _session_key(self, rel):
        return f"upsession:{self.pid}:{rel}"

    def _stored_session(self, rel, path):
        """The saved session URI, but only for this exact file version.

        A session is bound to the bytes that opened it: if the file changed
        since the failed attempt (size or mtime), the session is stale and
        must not be resumed — the caller starts a fresh one.
        """
        raw = self.store.kv_get(self._session_key(rel))
        if not raw:
            return None
        try:
            rec = json.loads(raw)
        except ValueError:
            return None
        try:
            same = (rec.get("size") == os.path.getsize(path) and
                    rec.get("mtime") == os.path.getmtime(path))
        except OSError:
            return None
        return rec.get("session") if same and rec.get("session") else None

    def _push_resumable(self, rel, path, update_id=None, name=None,
                        parent_id=None):
        """Create or update a Drive file, resumably when it is worth it.

        Files below resumable_threshold go through the plain one-shot
        calls as before. Large files use the resumable protocol with the
        session URI persisted in the kv store: a failed attempt (mid-pass
        retry or a later pass re-planning the same upload) continues from
        the server-side offset instead of restarting the transfer. The kv
        entry is deleted on success.
        """
        size = os.path.getsize(path)
        can_create = hasattr(self.drive, "upload_resumable")
        can_update = hasattr(self.drive, "update_content_resumable")
        if size < self._resumable_min() or \
                not (can_update if update_id is not None else can_create):
            if update_id is not None:
                return self.drive.update_content(update_id, path)
            return self.drive.upload(path, name, parent_id)

        key = self._session_key(rel)
        session = self._stored_session(rel, path)
        if session is None:
            self.store.kv_delete(key)
        mtime = os.path.getmtime(path)

        def on_session(uri):
            self.store.kv_set(key, json.dumps(
                {"session": uri, "size": size, "mtime": mtime}))

        if update_id is not None:
            meta = self.drive.update_content_resumable(
                update_id, path, session=session, on_session=on_session)
        else:
            meta = self.drive.upload_resumable(
                path, name, parent_id, session=session,
                on_session=on_session)
        self.store.kv_delete(key)      # completed — nothing left to resume
        return meta

    def _pull_remote(self, rel, dest):
        """Download rel, exporting first for Google-native docs."""
        r = self.remote_map.get(rel) or {}
        if r.get("kind") == "gdoc" and r.get("export"):
            self.drive.export(r["id"], r["export"][0], dest)
        else:
            self.drive.download(r["id"], dest)

    # -- op dispatch ------------------------------------------------
    def apply(self, op):
        fn = {
            "upload": self.op_upload,
            "download": self.op_download,
            "delete_local": self.op_delete_local,
            "delete_remote": self.op_delete_remote,
            "mkdir_local": self.op_mkdir_local,
            "mkdir_remote": self.op_mkdir_remote,
            "rmdir_local": self.op_rmdir_local,
            "rmdir_remote": self.op_rmdir_remote,
            "rename_local": self.op_rename_local,
            "rename_remote": self.op_rename_remote,
            "conflict_both": self.op_conflict_both,
            "restore_local_conflict": self.op_restore_local_conflict,
            "upload_conflict_copy": self.op_upload_conflict_copy,
            "record_gdoc": self.op_record_gdoc,
            "skip_gdoc_collision": self.op_skip_gdoc,
            "reconcile": self.op_reconcile,
            "forget": self.op_forget,
            "ack_local_delete": self.op_ack_local_delete,
            "ack_remote_delete": self.op_ack_remote_delete,
        }[op[0]]
        return fn(op)

    def op_upload(self, op):
        rel = op[1]
        path = self.lpath(rel)
        b = self._base(rel)
        rid_known = b["remote_id"] if b else None
        # only PATCH when the tracked id is still the live remote file;
        # if it was trashed/deleted remotely, create a fresh file instead
        still_live = rid_known and rel in self.remote_map and \
            self.remote_map[rel]["id"] == rid_known
        base_rel = rel
        if still_live:
            meta = self._push_resumable(rel, path, update_id=rid_known)
            rid = rid_known
        elif rel in self.fold:
            # the native doc is no longer live: push the exported file as a
            # regular Drive file and re-key the base row to the local name
            # (rel+ext) so the pair stays aligned from now on
            base_rel = self.fold[rel]
            pid = self.parent_remote_id(base_rel)
            meta = self._push_resumable(rel, path,
                                        name=base_rel.rsplit("/", 1)[-1],
                                        parent_id=pid)
            rid = meta["id"]
            self.store.base_delete(self.pid, rel)
        else:
            pid = self.parent_remote_id(rel)
            meta = self._push_resumable(rel, path,
                                        name=rel.rsplit("/", 1)[-1],
                                        parent_id=pid)
            rid = meta["id"]
        # Google-native docs never expose an md5 for their content — leaving
        # it None keeps remote change detection on the version field
        native_update = still_live and \
            (self.remote_map.get(rel) or {}).get("kind") == "gdoc"
        self.store.base_put(self.pid, base_rel, "file", remote_id=rid,
                            local_md5=md5_file(path),
                            local_size=os.path.getsize(path),
                            local_mtime=os.path.getmtime(path),
                            remote_md5=None if native_update
                            else meta.get("md5Checksum"),
                            remote_version=str(meta.get("version", "")),
                            **self._remote_meta(base_rel, meta))
        self.ids[base_rel] = rid
        if base_rel != rel:
            self.ids.pop(rel, None)
        self.stats.pushed += 1
        self.store.log(self.pid, "upload", base_rel)

    def op_download(self, op):
        rel = op[1]
        r = self.remote_map.get(rel)
        if r["kind"] == "gdoc" and r.get("export"):
            # first materialization has no fold entry yet: derive the local
            # name (rel+ext) from the export format itself
            dest_rel = export_lex(rel, r["export"][1])
            self.fold.setdefault(rel, dest_rel)
            note = f"exported as {dest_rel.rsplit('/', 1)[-1]}"
        else:
            dest_rel = rel
            note = ""
        dest = safe_join(self.root, dest_rel, allow_symlink_escape=True)
        if r.get("kind") == "gdoc" and r.get("export"):
            # never clobber a local file that is not our own export; the
            # base row (kind file + no remote_md5) is that proof
            b = self._base(rel)
            ours = bool(b and b.get("kind") == "file" and
                        b.get("remote_md5") is None)
            if os.path.exists(dest) and not ours:
                self.store.log(
                    self.pid, "error", rel,
                    "refusing to overwrite a local file that is not a "
                    "tracked Drive export", level="error")
                self.stats.errors += 1
                return False
        if os.path.isfile(dest):
            self.keep_old_version(rel)   # the old local bytes are about to go
        self._pull_remote(rel, dest)
        if r.get("mtime"):
            try:
                os.utime(dest, (r["mtime"], r["mtime"]))
            except OSError:
                pass
        self.store.base_put(self.pid, rel, "file", remote_id=r["id"],
                            local_md5=md5_file(dest),
                            local_size=os.path.getsize(dest),
                            local_mtime=os.path.getmtime(dest),
                            remote_md5=r["md5"],
                            remote_version=r["version"],
                            **self._remote_meta(rel))
        self.stats.pulled += 1
        self.store.log(self.pid, "download", rel, note)
        return True

    def _remote_iso(self, rel):
        return None  # retained for API compatibility; mtime set via utime

    def _trash_path(self, rel):
        """Destination inside the recoverable trash dir for one rel path."""
        root = os.path.join(str(app_dir()), "trash", str(self.pid),
                            self.trash_stamp)
        os.makedirs(root, exist_ok=True)
        target = safe_join(root, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        return target

    def op_delete_local(self, op):
        rel = op[1]
        path = self.lpath(rel)
        if os.path.exists(path):
            # safeguard: never hard-delete locally — move to the trash dir
            shutil.move(path, self._trash_path(rel))
        self.store.base_delete(self.pid, rel)
        self.stats.deleted += 1
        self.store.log(self.pid, "delete-local", rel, "moved to local trash")

    def op_delete_remote(self, op):
        rel = op[1]
        row = self._base(rel)
        rid = (row or {}).get("remote_id") or \
            (self.remote_map.get(rel) or {}).get("id")
        if rid:
            self.drive.trash(rid)
        self.store.base_delete(self.pid, rel)
        self.stats.deleted += 1
        self.store.log(self.pid, "delete-remote", rel)

    def op_mkdir_local(self, op):
        rel = op[1]
        os.makedirs(self.lpath(rel), exist_ok=True)
        r = self.remote_map.get(rel) or {}
        self.store.base_put(self.pid, rel, "dir", remote_id=r.get("id"),
                            remote_version=r.get("version"),
                            remote_mime=(r.get("mime") or FOLDER_MIME))
        self.store.log(self.pid, "mkdir-local", rel)

    def op_mkdir_remote(self, op):
        rel = op[1]
        pid = self.parent_remote_id(rel)
        created = self.drive.mkdir(rel.rsplit("/", 1)[-1], pid)
        self.ids[rel] = created["id"]
        self.store.base_put(self.pid, rel, "dir", remote_id=created["id"],
                            remote_version=str(created.get("version", "")),
                            remote_mime=FOLDER_MIME)
        self.stats.pushed += 1
        self.store.log(self.pid, "mkdir-remote", rel)

    def _children_of(self, rel):
        return [r for r in list(self.store.base_entries(self.pid))
                if r["rel_path"].startswith(rel + "/")]

    def op_rmdir_local(self, op):
        rel = op[1]
        # children were already removed by their own ops; clear stragglers
        for row in self._children_of(rel):
            self.store.base_delete(self.pid, row["rel_path"])
        path = self.lpath(rel)
        if os.path.exists(path):
            shutil.move(path, self._trash_path(rel))  # safeguard: recoverable
        self.store.base_delete(self.pid, rel)
        self.stats.deleted += 1
        self.store.log(self.pid, "rmdir-local", rel, "moved to local trash")

    def op_rmdir_remote(self, op):
        rel = op[1]
        for row in self._children_of(rel):
            rid = row["remote_id"]
            if rid:
                self.drive.trash(rid)
            self.store.base_delete(self.pid, row["rel_path"])
        rid = (self.remote_map.get(rel) or {}).get("id") or \
            ((self._base(rel) or {}).get("remote_id"))
        if rid:
            self.drive.trash(rid)
        self.store.base_delete(self.pid, rel)
        self.stats.deleted += 1
        self.store.log(self.pid, "rmdir-remote", rel)

    def op_rename_local(self, op):
        _, old, new = op
        r_old = self.remote_map.get(old) or {}
        is_gdoc = r_old.get("kind") == "gdoc" and r_old.get("export")
        new_name = new.rsplit("/", 1)[-1]
        if is_gdoc:
            # the Drive doc keeps its clean name: "Annual.docx" -> "Annual"
            ext = r_old["export"][1]
            if new_name.lower().endswith(ext) and len(new_name) > len(ext):
                new_name = new_name[:-len(ext)]
        new_rel = f"{self.parent(new)}/{new_name}" if self.parent(new) \
            else new_name
        b = self._base(old)
        rid = (b or {}).get("remote_id") or r_old.get("id")
        if rid:
            add_parent = self.parent_remote_id(new_rel)
            # the file currently lives in its old parent (root for top-level)
            remove_parent = self.ids.get(self.parent(old)) or self.pair["remote_id"]
            if add_parent == remove_parent:
                remove_parent = None  # Drive rejects add==remove in one call
            meta = self.drive.rename(rid, new_name,
                                     add_parent=add_parent,
                                     remove_parent=remove_parent)
        else:
            meta = {}
        self.store.base_delete(self.pid, old)
        self.store.base_put(self.pid, new_rel, "file", remote_id=rid,
                            local_md5=(b or {}).get("local_md5"),
                            local_size=(b or {}).get("local_size"),
                            local_mtime=(self.local_map.get(new) or {}).get("mtime"),
                            remote_md5=meta.get("md5Checksum", (b or {}).get("remote_md5"))
                            if not is_gdoc else None,
                            remote_version=str(meta.get("version", (b or {}).get("remote_version", ""))),
                            **self._remote_meta(old, meta,
                                                mime_fallback=(b or {}).get("remote_mime")))
        self.ids[new_rel] = rid
        self.ids.pop(old, None)
        self.stats.renamed += 1
        self.store.log(self.pid, "rename", old, f"moved to {new_rel}")

    def op_rename_remote(self, op):
        _, old, new = op
        b = self._base(old)
        old_path, new_path = self.lpath(old), self.lpath(new)
        os.makedirs(os.path.dirname(new_path) or ".", exist_ok=True)
        shutil.move(old_path, new_path)
        self.store.base_delete(self.pid, old)
        self.store.base_put(self.pid, new, "file",
                            remote_id=(b or {}).get("remote_id"),
                            local_md5=(b or {}).get("local_md5"),
                            local_size=(b or {}).get("local_size"),
                            local_mtime=os.path.getmtime(new_path),
                            remote_md5=(self.remote_map.get(new) or {}).get("md5"),
                            remote_version=(self.remote_map.get(new) or {}).get("version"),
                            **self._remote_meta(new))
        self.ids[new] = (b or {}).get("remote_id")
        self.ids.pop(old, None)
        self.stats.renamed += 1
        self.store.log(self.pid, "rename", old, f"moved to {new}")

    def op_conflict_both(self, op):
        """Both sides edited: keep remote at rel, upload local as a copy."""
        rel = op[1]
        copy_rel = self.conflict_name(rel)
        path, copy_path = self.lpath(rel), self.lpath(copy_rel)
        shutil.copy2(path, copy_path)
        # push the local copy
        name = copy_rel.rsplit("/", 1)[-1]
        pid = self.parent_remote_id(copy_rel)
        meta = self._push_resumable(copy_rel, copy_path, name=name,
                                    parent_id=pid)
        # pull remote to rel (export for Google-native docs)
        r = self.remote_map[rel]
        self._pull_remote(rel, path)
        self.store.base_put(self.pid, rel, "file", remote_id=r["id"],
                            local_md5=md5_file(path),
                            local_size=os.path.getsize(path),
                            local_mtime=os.path.getmtime(path),
                            remote_md5=r["md5"], remote_version=r["version"],
                            **self._remote_meta(rel))
        self.store.base_put(self.pid, copy_rel, "file", remote_id=meta["id"],
                            local_md5=md5_file(copy_path),
                            local_size=os.path.getsize(copy_path),
                            local_mtime=os.path.getmtime(copy_path),
                            remote_md5=meta.get("md5Checksum"),
                            remote_version=str(meta.get("version", "")),
                            **self._remote_meta(copy_rel, meta))
        self.ids[copy_rel] = meta["id"]
        self.stats.conflicts += 1
        self.store.log(self.pid, "conflict", rel,
                       f"both changed; local copy kept at {copy_rel}",
                       level="conflict")

    def op_restore_local_conflict(self, op):
        """Local deleted, remote edited: keep the remote edit as a copy."""
        rel = op[1]
        r = self.remote_map[rel]
        copy_rel = self.conflict_name(rel)
        copy_path = self.lpath(copy_rel)
        self._pull_remote(rel, copy_path)
        # push the copy so it exists on both sides and survives later passes;
        # the local deletion itself still propagates on the next pass
        name = copy_rel.rsplit("/", 1)[-1]
        pid = self.parent_remote_id(copy_rel)
        meta = self._push_resumable(copy_rel, copy_path, name=name,
                                    parent_id=pid)
        self.store.base_put(self.pid, copy_rel, "file", remote_id=meta["id"],
                            local_md5=md5_file(copy_path),
                            local_size=os.path.getsize(copy_path),
                            local_mtime=os.path.getmtime(copy_path),
                            remote_md5=meta.get("md5Checksum"),
                            remote_version=str(meta.get("version", "")),
                            **self._remote_meta(copy_rel, meta))
        # record the new remote state for rel — the conflict is acknowledged
        self.store.base_put(self.pid, rel, "file", remote_id=r["id"],
                            local_md5=None, local_size=None, local_mtime=None,
                            remote_md5=r["md5"], remote_version=r["version"],
                            **self._remote_meta(rel))
        self.ids[copy_rel] = meta["id"]
        self.stats.conflicts += 1
        self.store.log(self.pid, "conflict", rel,
                       f"remote edit kept at {copy_rel}", level="conflict")

    def op_upload_conflict_copy(self, op):
        """Remote deleted, local edited: keep local, upload under a new name."""
        rel = op[1]
        copy_rel = self.conflict_name(rel)
        copy_path = self.lpath(copy_rel)
        shutil.copy2(self.lpath(rel), copy_path)
        name = copy_rel.rsplit("/", 1)[-1]
        pid = self.parent_remote_id(copy_rel)
        meta = self._push_resumable(copy_rel, copy_path, name=name,
                                    parent_id=pid)
        self.store.base_put(self.pid, copy_rel, "file", remote_id=meta["id"],
                            local_md5=md5_file(copy_path),
                            local_size=os.path.getsize(copy_path),
                            local_mtime=os.path.getmtime(copy_path),
                            remote_md5=meta.get("md5Checksum"),
                            remote_version=str(meta.get("version", "")),
                            **self._remote_meta(copy_rel, meta))
        self.store.base_delete(self.pid, rel)   # remote deletion accepted
        self.ids[copy_rel] = meta["id"]
        self.stats.conflicts += 1
        self.store.log(self.pid, "conflict", rel,
                       f"remote deleted; local kept at {copy_rel}",
                       level="conflict")

    def op_record_gdoc(self, op):
        rel = op[1]
        r = self.remote_map[rel]
        self.store.base_put(self.pid, rel, "gdoc", remote_id=r["id"],
                            remote_md5=None, remote_version=r["version"],
                            **self._remote_meta(rel))
        self.store.log(self.pid, "skip", rel,
                       "Google-native document: not downloaded (no md5)")

    def op_skip_gdoc(self, op):
        rel = op[1]
        self.store.log(self.pid, "error", rel,
                       "local file collides with a Google-native doc; left untouched",
                       level="error")
        self.stats.errors += 1

    def op_reconcile(self, op):
        rel = op[1]
        path = self.lpath(rel)
        r = self.remote_map[rel]
        self.store.base_put(self.pid, rel, "file",
                            remote_id=(self._base(rel) or {}).get("remote_id")
                            or r["id"],
                            local_md5=md5_file(path),
                            local_size=os.path.getsize(path),
                            local_mtime=os.path.getmtime(path),
                            remote_md5=r["md5"], remote_version=r["version"],
                            **self._remote_meta(rel))
        self.store.log(self.pid, "reconcile", rel, "identical changes on both sides")

    def op_forget(self, op):
        self.store.base_delete(self.pid, op[1])

    def op_ack_local_delete(self, op):
        """Deletes-off mode: local file was deleted, keep the remote copy."""
        rel = op[1]
        self.store.base_mark_deleted(self.pid, rel, "local")
        self.store.log(self.pid, "delete-kept", rel,
                       "deletes off — remote copy kept, deletion acknowledged")

    def op_ack_remote_delete(self, op):
        """Deletes-off mode: remote file was deleted, keep the local copy."""
        rel = op[1]
        self.store.base_mark_deleted(self.pid, rel, "remote")
        self.store.log(self.pid, "delete-kept", rel,
                       "deletes off — local copy kept, deletion acknowledged")


# ---------------------------------------------------------------- on-demand
class OnDemandSkip(Exception):
    """A Drive item cannot be fetched locally (unconvertible Google doc)."""


def _set_remote_mtime(dest, iso):
    """Best effort: stamp the fetched file with the Drive modifiedTime."""
    ts = parse_remote_mtime(iso)
    if ts:
        try:
            os.utime(dest, (ts, ts))
        except OSError:
            pass


def download_item(drive, entry, dest_path, export_map=None):
    """Fetch one Drive file to dest_path; returns the written path.

    entry is a files.list style metadata dict (id, name, mimeType,
    modifiedTime). Google-native docs are exported with the configured
    format and the extension is appended unless the name already carries
    it. Raises OnDemandSkip for native docs with no convertible format.
    """
    mime = entry.get("mimeType", "")
    name = entry.get("name", "file")
    if mime == FOLDER_MIME:
        raise ValueError("use download_tree() for folders")
    if mime.startswith(GDOC_PREFIX):
        exp = (export_map or {}).get(mime)
        if not exp:
            raise OnDemandSkip(
                f"{name}: this Google Docs type has no configured export "
                "format (see the convert_google_docs setting)")
        dest_path = export_lex(dest_path, exp[1])
        drive.export(entry["id"], exp[0], dest_path)
    else:
        drive.download(entry["id"], dest_path)
    _set_remote_mtime(dest_path, entry.get("modifiedTime"))
    return dest_path


def _tree_note(stats, notes, rel, outcome):
    """Fold one fetch outcome into stats and the serial log notes."""
    kind, detail = outcome
    if kind == "ok":
        stats["downloaded"] += 1
        stats["bytes"] += detail
        notes.append((rel, "saved", "info"))
    elif kind == "skip":
        stats["skipped"] += 1
        notes.append((rel, detail, "info"))
    else:
        stats["errors"] += 1
        notes.append((rel, detail, "error"))


def download_tree(drive, folder_id, dest_dir, export_map=None, concurrency=4,
                  log=None, progress=None):
    """Fetch a whole Drive folder into dest_dir (contents at the top level).

    Files download in parallel; unconvertible Google docs are skipped and
    counted — never silently dropped. log(rel, message, level) receives
    per-file results when given (replayed serially at the end). progress
    (done, total, rel, kind) is called as each file completes so callers
    can show live progress; kind is 'ok' | 'skip' | 'error'. Returns
    {'downloaded': n, 'skipped': n, 'errors': n, 'bytes': n}.
    """
    dest_dir = str(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    tree = drive.list_tree(folder_id)
    stats = {"downloaded": 0, "skipped": 0, "errors": 0, "bytes": 0}
    notes = []                          # (rel, message, level), logged serially

    def note(rel, message, level="info"):
        notes.append((rel, message, level))

    # materialize the directory structure first so empty folders land too
    for rel in sorted(tree):
        if rel and tree[rel].get("mimeType") == FOLDER_MIME:
            os.makedirs(os.path.join(dest_dir, *rel.split("/")), exist_ok=True)

    def fetch(rel):
        """Download one file; returns ('ok'|'skip'|'error', detail)."""
        meta = tree[rel]
        try:
            dest = download_item(drive, meta, os.path.join(dest_dir, rel),
                                 export_map)
        except OnDemandSkip as exc:
            return "skip", str(exc)
        except Exception as exc:
            return "error", f"download failed: {exc}"
        size = 0
        try:
            size = os.path.getsize(dest)
        except OSError:
            pass
        return "ok", size

    files = [rel for rel in sorted(tree)
             if rel and tree[rel].get("mimeType") != FOLDER_MIME]
    workers = max(1, int(concurrency or 1))
    done = 0
    if workers == 1 or len(files) < 2:
        for rel in files:
            outcome = fetch(rel)
            done += 1
            _tree_note(stats, notes, rel, outcome)
            if progress:
                try:
                    progress(done, len(files), rel, outcome[0])
                except Exception:
                    pass
    else:
        # as_completed (not pool.map) so callers get live per-file progress
        # instead of waiting for the whole folder
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch, rel): rel for rel in files}
            for fut in as_completed(futures):
                rel = futures[fut]
                outcome = fut.result()
                done += 1
                _tree_note(stats, notes, rel, outcome)
                if progress:
                    try:
                        progress(done, len(files), rel, outcome[0])
                    except Exception:
                        pass
    if log:
        for rel, message, level in notes:
            try:
                log(rel, message, level)
            except Exception:
                pass
    return stats
