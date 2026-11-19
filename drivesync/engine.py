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
"""

import fnmatch
import hashlib
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from .config import app_dir
from .db import parse_include_patterns
from .drive import FOLDER_MIME, resolve_export_formats

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


def scan_local(root, patterns, include=None):
    """Walk the local root -> {rel: {'kind','size','mtime','md5'}}.

    md5 starts as None and is computed on demand via md5_of(rel).
    Paths outside the pair's include scope (selective sync) are skipped —
    excluded subfolders stay on disk untouched and invisible to decide().
    """
    root = str(root)
    items = {}
    if not os.path.isdir(root):
        return items
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        # drop ignored/out-of-scope directories from the walk entirely
        keep = []
        for d in sorted(dirnames):
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if ignored(rel, patterns) or not included(rel, include):
                continue
            keep.append(d)
            items[rel] = {"kind": "dir", "size": None, "mtime": None, "md5": None}
        dirnames[:] = keep
        for f in sorted(filenames):
            rel = f"{rel_dir}/{f}" if rel_dir else f
            if ignored(rel, patterns) or not included(rel, include,
                                                      is_file=True):
                continue
            full = os.path.join(dirpath, f)
            try:
                st = os.stat(full)
            except OSError:
                continue
            items[rel] = {"kind": "file", "size": st.st_size,
                          "mtime": st.st_mtime, "md5": None}
    return items


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
        name = entry.get("name", "")
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
           md5_of=None, force=False, allow_deletes=True):
    """Return the ordered op list. Pure function of the three maps.

    force=True re-checks content hashes even when size and mtime look
    unchanged (catches same-size/same-mtime edits).
    allow_deletes=False replaces destructive ops with ack_* acknowledgements:
    files are kept on both sides and the base is tombstoned so the state is
    stable — no resurrection churn on later passes.
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
                               md5_of, handled))

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
        op = _decide_file(rel, l, r, b, can_push, can_pull, policy, md5_of, force)
        if op:
            ops.extend(op)

    # ---- pass 3: directories ---------------------------------------
    ops.extend(_decide_dirs(local, remote, base, can_push, can_pull, handled,
                            allow_deletes))
    ops = _filter_deletes(ops, allow_deletes)

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


def _decide_file(rel, l, r, b, can_push, can_pull, policy, md5_of, force=False):
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
            return [("forget", rel)]          # just drop the base row
        return []

    # ---- new on one side --------------------------------------------
    if l_exists and not r_exists and not b_exists:
        return [("upload", rel)] if can_push else []
    if not l_exists and r_exists and not b_exists:
        if r["kind"] == "gdoc":
            if r.get("export"):
                return [("download", rel)] if can_pull else []
            return [("record_gdoc", rel)]
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
            return [("delete_remote", rel)] if can_push else []
        # remote changed since base while local was deleted
        return _conflict_delete_local(rel, policy, can_pull)
    if l_exists and not r_exists:                       # remote deleted
        if not local_changed():
            return [("delete_local", rel)] if can_pull else [("upload", rel)]
        # local changed since base while remote was deleted
        return _conflict_delete_remote(rel, policy, can_push, can_pull)

    # ---- present on both sides ---------------------------------------
    if r["kind"] == "gdoc":
        if not r.get("export"):
            return [("skip_gdoc_collision", rel)]   # never touches native docs
        if not l.get("folded"):
            # a user-made file sits where the export would go — never
            # overwrite it, even when an old base row exists
            return [("skip_gdoc_collision", rel)]
        if not b_exists or b.get("kind") != "file":
            # tracked but not yet materialized: export (the fold is proof
            # this local file is ours)
            return [("download", rel)] if can_pull else []
        # materialized export: falls through to the normal three-way compare

    lc, rc = local_changed(), remote_changed()
    if not lc and not rc:
        return []
    if lc and not rc:
        return [("upload", rel)] if can_push else []
    if rc and not lc:
        return [("download", rel)] if can_pull else []

    # both changed
    if can_push and can_pull and r["md5"] and local_md5() == r["md5"]:
        return [("reconcile", rel)]          # same content, just refresh base
    if not (can_push and can_pull):
        return [("upload", rel)] if can_push else [("download", rel)]
    return _conflict_both_changed(rel, l, r, policy, local_md5())


def _mtime_neq(a, b):
    if a is None or b is None:
        return True
    return abs(float(a) - float(b)) > 1e-6


def _conflict_both_changed(rel, l, r, policy, local_md5):
    if policy == "local":
        return [("upload", rel)]
    if policy == "remote":
        return [("download", rel)]
    if policy == "newer":
        newer_local = float(l["mtime"] or 0) >= float(r["mtime"] or 0)
        return [("upload" if newer_local else "download", rel)]
    return [("conflict_both", rel)]


def _conflict_delete_local(rel, policy, can_pull):
    """Local file deleted, remote edited since base."""
    if policy == "local":
        return [("delete_remote", rel)]
    if policy == "both":
        return [("restore_local_conflict", rel)]
    return [("download", rel)] if can_pull else [("delete_remote", rel)]


def _conflict_delete_remote(rel, policy, can_push, can_pull):
    """Remote file deleted, local edited since base."""
    if policy == "remote":
        return [("delete_local", rel)]
    if policy == "both":
        return [("upload_conflict_copy", rel)]
    # 'local' and 'newer' both keep the edited local copy (no data loss)
    return [("upload", rel)] if can_push else [("delete_local", rel)]


def _detect_renames(local, remote, base, can_push, can_pull, md5_of, handled):
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
            ops.append(("rename_remote", old_rel, match))
            used_new.add(match)
            handled.add(old_rel)
            handled.add(match)
    return ops


def _decide_dirs(local, remote, base, can_push, can_pull, handled,
                 allow_deletes=True):
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
            ops.append(("forget", rel))  # file replaced by a folder; drop stale row
            continue
        if l_dir and not r_dir and not b_dir and can_push:
            ops.append(("mkdir_remote", rel))
        elif r_dir and not l_dir and not b_dir and can_pull:
            ops.append(("mkdir_local", rel))
        elif not l and b_dir and r_dir:
            # local dir removed: propagate when pushing, else keep base
            ops.append(("rmdir_remote", rel)) if can_push else ops.append(("forget", rel))
        elif not r and b_dir and l_dir:
            ops.append(("rmdir_local", rel)) if can_pull else ops.append(("forget", rel))
        elif not l and not r and b_dir:
            ops.append(("forget", rel))
    return ops


def _filter_deletes(ops, allow_deletes):
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
        elif op[0] in ("delete_local", "rmdir_local"):
            out.append(("ack_remote_delete", op[1]))  # remote gone, keep local
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


def sync_pair(store, cfg, pair, drive, force_full=False, changes=None):
    """Run one sync pass for a pair row. Returns (SyncStats, ok).

    changes: a Drive changes feed (list of {'fileId','removed','file'})
    captured since the previous pass. When given, the remote map is rebuilt
    from the base snapshot + the feed instead of re-listing the whole tree —
    one-file edits in large pairs cost 1-2 API calls instead of one per
    folder. Structural feed changes fall back to a full scan automatically.
    """
    local_root = str(pair["local_path"])
    stats = SyncStats()
    if not os.path.isdir(local_root):
        store.log(pair["id"], "error", message=f"local path missing: {local_root}",
                  level="error")
        stats.errors += 1
        return stats, False

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
        store.log(pair["id"], "error", "",
                  "base snapshot is empty but this pair synced before — "
                  "refusing to reconcile from scratch (run "
                  "'drivesync sync --full --pair "
                  f"{pair['id']}' once to rebuild it)", level="error")
        stats.errors += 1
        return stats, False

    remote = None
    if changes is not None and not force_full:
        try:
            remote = scan_remote_delta(pair, base_rows, export_map, patterns,
                                       changes, include=include)
        except DeltaUnsupported:
            remote = None
        except Exception as exc:
            store.log(pair["id"], "info", "",
                      f"delta scan failed ({exc}); doing a full scan")
            remote = None
    if remote is None:
        try:
            remote = scan_remote(drive, pair["remote_id"], patterns, export_map,
                                 include=include)
        except Exception as exc:
            store.log(pair["id"], "error", message=f"Drive listing failed: {exc}",
                      level="error")
            stats.errors += 1
            return stats, False

    local = scan_local(local_root, patterns, include=include)
    base = base_map(base_rows)

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

    ops = decide(local, remote, base, pair["direction"],
                 pair["conflict_policy"] or cfg.get("conflict_policy", "newer"),
                 md5_of, force=force_full,
                 allow_deletes=bool(pair["allow_deletes"]))

    # safety valve: refuse to wipe out more files than allowed in one pass
    limit = int(cfg.get("delete_safety_limit", 10))
    destructive = [op for op in ops if op[0] in DESTRUCTIVE_OPS]
    if limit and len(destructive) > limit:
        store.log(pair["id"], "safety-stop", "",
                  f"{len(destructive)} deletions blocked — exceeds "
                  f"delete_safety_limit={limit}; raise it in Settings if this "
                  "is expected", level="error")
        ops = [op for op in ops if op[0] not in DESTRUCTIVE_OPS]
        stats.errors += 1

    ctx = _ApplyCtx(store, cfg, pair, drive, local_root, remote, local, stats,
                    fold)

    def apply_one(op):
        try:
            # one commit per op (base row + marker/activity rows) instead of
            # one per write: a first sync writes thousands of small rows
            with store.batch():
                ctx.apply(op)
        except Exception as exc:
            stats.errors += 1
            store.log(pair["id"], "error", op[1] if len(op) > 1 else "",
                      f"{op[0]} failed: {exc}", level="error")

    # Same rank order as the sequential loop: mkdirs/renames and per-file
    # bookkeeping first, then file transfers (parallel), deletions last.
    pre, transfers, post = [], [], []
    for op in ops:
        name = op[0]
        if name in TRANSFER_OPS:
            transfers.append(op)
        elif name in DESTRUCTIVE_OPS:
            post.append(op)
        else:
            pre.append(op)
    for op in pre:
        apply_one(op)
    workers = max(1, int(cfg.get("transfer_concurrency", 4)))
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
    store.kv_set(marker, str(len(store.base_entries(pair["id"]))))
    return stats, stats.errors == 0


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
        return os.path.join(self.root, self.fold.get(rel, rel))

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
        fn(op)

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
            meta = self.drive.update_content(rid_known, path)
            rid = rid_known
        elif rel in self.fold:
            # the native doc is no longer live: push the exported file as a
            # regular Drive file and re-key the base row to the local name
            # (rel+ext) so the pair stays aligned from now on
            base_rel = self.fold[rel]
            pid = self.parent_remote_id(base_rel)
            meta = self.drive.upload(path, base_rel.rsplit("/", 1)[-1], pid)
            rid = meta["id"]
            self.store.base_delete(self.pid, rel)
        else:
            pid = self.parent_remote_id(rel)
            meta = self.drive.upload(path, rel.rsplit("/", 1)[-1], pid)
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
        dest = os.path.join(self.root, dest_rel)
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
                return
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

    def _remote_iso(self, rel):
        return None  # retained for API compatibility; mtime set via utime

    def _trash_path(self, rel):
        """Destination inside the recoverable trash dir for one rel path."""
        root = os.path.join(str(app_dir()), "trash", str(self.pid),
                            self.trash_stamp)
        os.makedirs(root, exist_ok=True)
        target = os.path.join(root, rel.replace("\\", "/"))
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
        meta = self.drive.upload(copy_path, name, pid)
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
        meta = self.drive.upload(copy_path, name, pid)
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
        meta = self.drive.upload(copy_path, name, pid)
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
