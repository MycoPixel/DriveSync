"""Engine tests: every reconcile case against the mock Drive."""

import os
import time

import pytest

from drivesync.engine import decide, md5_file, scan_local


def wfile(path, content):
    p = path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes) else content.encode())


def rfile(path):
    return path.read_bytes()


# ---------------------------------------------------------- transfer queue
def test_sync_pair_reports_transfer_lifecycle(env):
    from drivesync import engine

    wfile(env.local / "queued.txt", "hello")
    events = []
    stats, ok = engine.sync_pair(
        env.store, env.cfg, env.pair, env.drive,
        transfer_callback=events.append)

    assert ok and stats.pushed == 1
    assert [event["status"] for event in events] == [
        "queued", "uploading", "completed"]
    assert all(event["path"] == "queued.txt" for event in events)
    assert all(event["direction"] == "upload" for event in events)
    assert events[-1]["size"] == 5


# ---------------------------------------------------------- basic transfers
def test_new_local_file_uploads(env):
    wfile(env.local / "a.txt", "alpha")
    stats, ok = env.sync()
    assert ok and stats.pushed == 1
    assert env.drive.read_remote("a.txt") == b"alpha"
    assert env.base()["a.txt"]["kind"] == "file"


def test_new_remote_file_downloads(env):
    env.drive.put_remote_file("b.txt", "bravo")
    stats, ok = env.sync()
    assert ok and stats.pulled == 1
    assert rfile(env.local / "b.txt") == b"bravo"


def test_local_edit_pushes(env):
    wfile(env.local / "a.txt", "v1")
    env.sync()
    wfile(env.local / "a.txt", "v2-changed-longer")
    stats, ok = env.sync()
    assert ok and stats.pushed == 1
    assert env.drive.read_remote("a.txt") == b"v2-changed-longer"


def test_remote_edit_pulls(env):
    env.drive.put_remote_file("b.txt", "v1")
    env.sync()
    fid = env.drive._find_by_rel("b.txt")
    env.drive._fs_path(fid).write_bytes(b"v2-from-drive")
    env.drive._touch(fid)
    stats, ok = env.sync()
    assert ok and stats.pulled == 1
    assert rfile(env.local / "b.txt") == b"v2-from-drive"


def test_no_change_no_ops(env):
    wfile(env.local / "a.txt", "same")
    env.sync()
    stats, ok = env.sync()
    assert ok
    assert (stats.pushed, stats.pulled, stats.deleted, stats.renamed) == (0, 0, 0, 0)


def test_touch_only_is_not_uploaded(env):
    wfile(env.local / "a.txt", "same")
    env.sync()
    old = os.stat(env.local / "a.txt")
    time.sleep(0.01)
    os.utime(env.local / "a.txt", (old.st_atime, old.st_mtime + 100))
    stats, ok = env.sync()
    assert ok and stats.pushed == 0


def test_same_content_both_changed_reconciles(env):
    # different sizes vs the base so change detection does not depend on
    # filesystem mtime granularity (same-size edits rely on the mtime tick)
    wfile(env.local / "a.txt", "v1-original-content")
    env.sync()
    wfile(env.local / "a.txt", "v2-edited-longer")
    fid = env.drive._find_by_rel("a.txt")
    env.drive._fs_path(fid).write_bytes(b"v2-edited-longer")
    stats, ok = env.sync()
    assert ok and (stats.pushed, stats.pulled) == (0, 0)


# ---------------------------------------------------------- conflicts
def _both_changed(env):
    """Sync a file, then change it locally and on the fake Drive."""
    wfile(env.local / "a.txt", "original")
    env.sync()
    wfile(env.local / "a.txt", "local edit")
    st = os.stat(env.local / "a.txt")
    os.utime(env.local / "a.txt", (st.st_atime, 1_000_000_000))  # older
    fid = env.drive._find_by_rel("a.txt")
    env.drive._fs_path(fid).write_bytes(b"remote edit")
    env.drive._touch(fid)  # modifiedTime = now -> newer than local
    return fid


def test_conflict_newer_remote_wins(env):
    _both_changed(env)
    stats, ok = env.sync()
    assert ok and stats.pulled == 1 and stats.pushed == 0
    assert rfile(env.local / "a.txt") == b"remote edit"


def test_conflict_newer_local_wins(env):
    _both_changed(env)
    st = os.stat(env.local / "a.txt")
    os.utime(env.local / "a.txt", (st.st_atime, time.time() + 500))  # newer
    stats, ok = env.sync()
    assert ok and stats.pushed == 1
    assert env.drive.read_remote("a.txt") == b"local edit"


def test_conflict_policy_local(env):
    _both_changed(env)
    env.set_policy("local")
    env.sync()
    assert env.drive.read_remote("a.txt") == b"local edit"


def test_conflict_policy_remote(env):
    _both_changed(env)
    env.set_policy("remote")
    env.sync()
    assert rfile(env.local / "a.txt") == b"remote edit"


def test_conflict_policy_both_keeps_copies(env):
    _both_changed(env)
    env.set_policy("both")
    stats, ok = env.sync()
    assert ok and stats.conflicts == 1
    # remote content landed at the original path
    assert rfile(env.local / "a.txt") == b"remote edit"
    # local edit preserved under a conflict-* name, here and on Drive
    copies = list(env.local.glob("a.conflict-*.txt"))
    assert len(copies) == 1 and copies[0].read_bytes() == b"local edit"
    assert env.drive.read_remote(copies[0].name) == b"local edit"


# ---------------------------------------------------------- deletions
def test_local_delete_propagates(env):
    wfile(env.local / "a.txt", "bye")
    env.sync()
    os.remove(env.local / "a.txt")
    stats, ok = env.sync()
    assert ok and stats.deleted == 1
    assert "a.txt" not in env.base()
    with pytest.raises(KeyError):
        env.drive.read_remote("a.txt")


def test_remote_delete_propagates(env):
    env.drive.put_remote_file("b.txt", "bye")
    env.sync()
    env.drive.trash(env.drive._find_by_rel("b.txt"))
    stats, ok = env.sync()
    assert ok and stats.deleted == 1
    assert not (env.local / "b.txt").exists()


def test_delete_vs_remote_edit_newer_restores(env):
    wfile(env.local / "a.txt", "v1")
    env.sync()
    os.remove(env.local / "a.txt")
    fid = env.drive._find_by_rel("a.txt")
    env.drive._fs_path(fid).write_bytes(b"edited on drive")
    env.drive._touch(fid)
    stats, ok = env.sync()  # default policy 'newer': remote edit beats delete
    assert ok and stats.pulled == 1
    assert rfile(env.local / "a.txt") == b"edited on drive"


def test_delete_vs_remote_edit_policy_local_deletes(env):
    wfile(env.local / "a.txt", "v1")
    env.sync()
    os.remove(env.local / "a.txt")
    fid = env.drive._find_by_rel("a.txt")
    env.drive._fs_path(fid).write_bytes(b"edited on drive")
    env.drive._touch(fid)
    env.set_policy("local")
    stats, ok = env.sync()
    assert ok and stats.deleted == 1
    with pytest.raises(KeyError):
        env.drive.read_remote("a.txt")


def test_remote_delete_vs_local_edit_keeps_local(env):
    env.drive.put_remote_file("b.txt", "v1")
    env.sync()
    env.drive.trash(env.drive._find_by_rel("b.txt"))
    wfile(env.local / "b.txt", "kept local edit")
    stats, ok = env.sync()  # 'newer' keeps the edited side: upload
    assert ok and stats.pushed == 1
    assert env.drive.read_remote("b.txt") == b"kept local edit"


# ---------------------------------------------------------- renames
def test_local_rename_detected(env):
    wfile(env.local / "report.txt", "contents")
    env.sync()
    os.rename(env.local / "report.txt", env.local / "renamed.txt")
    stats, ok = env.sync()
    assert ok and stats.renamed == 1 and stats.pushed == 0
    assert env.drive.read_remote("renamed.txt") == b"contents"
    base = env.base()
    assert "report.txt" not in base and "renamed.txt" in base


def test_remote_rename_detected(env):
    env.drive.put_remote_file("notes.txt", "note body")
    env.sync()
    fid = env.drive._find_by_rel("notes.txt")
    env.drive.rename(fid, "notes-v2.txt")
    stats, ok = env.sync()
    assert ok and stats.renamed == 1 and stats.pulled == 0
    assert (env.local / "notes-v2.txt").exists()
    assert not (env.local / "notes.txt").exists()


def test_local_move_into_subdir_detected(env):
    wfile(env.local / "top.txt", "moved")
    env.sync()
    (env.local / "sub").mkdir()
    os.rename(env.local / "top.txt", env.local / "sub" / "top.txt")
    stats, ok = env.sync()
    assert ok and stats.renamed == 1
    assert env.drive.read_remote("sub/top.txt") == b"moved"


# ---------------------------------------------------------- directories
def test_nested_new_dirs_both_sides(env):
    wfile(env.local / "sub" / "x.txt", "local nested")
    env.drive.put_remote_folder("other")
    env.drive.put_remote_file("other/y.txt", "remote nested")
    stats, ok = env.sync()
    assert ok
    assert rfile(env.local / "other" / "y.txt") == b"remote nested"
    assert env.drive.read_remote("sub/x.txt") == b"local nested"


def test_local_dir_delete_removes_remote_children(env):
    wfile(env.local / "d" / "f.txt", "in dir")
    env.sync()
    import shutil
    shutil.rmtree(env.local / "d")
    stats, ok = env.sync()
    assert ok and stats.deleted >= 2  # file + folder
    with pytest.raises(KeyError):
        env.drive.read_remote("d/f.txt")
    assert "d/f.txt" not in env.base() and "d" not in env.base()


# ---------------------------------------------------------- directions
def test_direction_upload_only(env):
    wfile(env.local / "up.txt", "pushed")
    env.drive.put_remote_file("down.txt", "should stay remote")
    env.set_direction("upload")
    stats, ok = env.sync()
    assert ok and stats.pulled == 0
    assert env.drive.read_remote("up.txt") == b"pushed"
    assert not (env.local / "down.txt").exists()


def test_direction_upload_only_remote_edit_ignored(env):
    wfile(env.local / "a.txt", "mine")
    env.sync()
    env.set_direction("upload")
    fid = env.drive._find_by_rel("a.txt")
    env.drive._fs_path(fid).write_bytes(b"remote version")
    env.drive._touch(fid)
    stats, ok = env.sync()
    assert ok and stats.pulled == 0
    assert rfile(env.local / "a.txt") == b"mine"


def test_direction_download_only(env):
    env.drive.put_remote_file("down.txt", "from cloud")
    wfile(env.local / "up.txt", "local only")
    env.set_direction("download")
    stats, ok = env.sync()
    assert ok and stats.pushed == 0
    assert rfile(env.local / "down.txt") == b"from cloud"
    assert env.drive.read_remote.__self__ is not None
    with pytest.raises(KeyError):
        env.drive.read_remote("up.txt")  # never uploaded


def test_direction_download_only_pulls_remote_delete(env):
    env.drive.put_remote_file("f.txt", "x")
    env.sync()
    env.set_direction("download")
    env.drive.trash(env.drive._find_by_rel("f.txt"))
    stats, ok = env.sync()
    assert ok and stats.deleted == 1
    assert not (env.local / "f.txt").exists()


# ---------------------------------------------------------- ignoring & gdocs
def test_md5_hashed_once_per_path_per_pass(env, monkeypatch):
    """Rename detection and change detection share one hash per file."""
    import drivesync.engine as engine_mod

    wfile(env.local / "a.txt", "same-content")
    assert env.sync()[1]
    (env.local / "a.txt").rename(env.local / "b.txt")

    seen = []
    real = engine_mod.md5_file
    monkeypatch.setattr(engine_mod, "md5_file",
                        lambda path: (seen.append(path), real(path))[1])
    stats, ok = env.sync()
    assert ok
    assert env.drive.read_remote("b.txt") == b"same-content"
    assert len(seen) == len(set(seen))       # never hashed twice
    assert len(seen) <= 2


def test_ignore_patterns(env):
    env.cfg["ignore_patterns"] = ["*.tmp"]
    wfile(env.local / "keep.txt", "kept")
    wfile(env.local / "skip.tmp", "ignored")
    stats, ok = env.sync()
    assert ok
    assert env.drive.read_remote("keep.txt") == b"kept"
    with pytest.raises(KeyError):
        env.drive.read_remote("skip.tmp")
    assert "skip.tmp" not in env.base()


def test_ignore_dir_skips_subtree(env):
    env.cfg["ignore_patterns"] = ["build"]
    wfile(env.local / "build" / "o.o", "x")
    wfile(env.local / "src" / "s.py", "y")
    ok = env.sync()[1]
    assert ok
    with pytest.raises(KeyError):
        env.drive.read_remote("build/o.o")
    assert env.drive.read_remote("src/s.py") == b"y"


def test_non_convertible_native_recorded_not_downloaded(env):
    fid = env.drive.put_remote_file("Survey.form", "placeholder")
    env.drive.files[fid]["mimeType"] = "application/vnd.google-apps.form"
    stats, ok = env.sync()
    assert ok
    assert not (env.local / "Survey.form").exists()
    assert env.base()["Survey.form"]["kind"] == "gdoc"
    # steady state: second pass does not re-log or duplicate
    stats2, ok2 = env.sync()
    assert ok2 and stats2.pushed == 0 and stats2.pulled == 0


# --------------------------------------------------- google docs conversion
def _put_remote_doc(env, name, content, mime="application/vnd.google-apps.document"):
    fid = env.drive.put_remote_file(name, content)
    env.drive.files[fid]["mimeType"] = mime
    return fid


def test_google_doc_exports_as_docx(env):
    _put_remote_doc(env, "Report", "doc-bytes-v1")
    stats, ok = env.sync()
    assert ok and stats.pulled == 1
    assert (env.local / "Report.docx").exists()
    assert rfile(env.local / "Report.docx") == b"doc-bytes-v1"
    row = env.base()["Report"]
    assert row["kind"] == "file" and row["remote_id"]
    # steady state: no churn on the next pass
    stats2, ok2 = env.sync()
    assert ok2 and stats2.pushed == 0 and stats2.pulled == 0


def test_google_doc_edit_on_drive_reexports(env):
    fid = _put_remote_doc(env, "Report", "v1")
    env.sync()
    env.drive._fs_path(fid).write_bytes(b"edited-in-browser")
    env.drive._touch(fid)
    stats, ok = env.sync()
    assert ok and stats.pulled == 1
    assert rfile(env.local / "Report.docx") == b"edited-in-browser"


def test_google_doc_local_edit_uploads_converted(env):
    fid = _put_remote_doc(env, "Report", "v1")
    env.sync()
    time.sleep(0.01)
    wfile(env.local / "Report.docx", b"edited in libreoffice")
    stats, ok = env.sync()
    assert ok and stats.pushed == 1
    assert env.drive._fs_path(fid).read_bytes() == b"edited in libreoffice"
    # and the drive-side edit then does not bounce back
    stats2, ok2 = env.sync()
    assert ok2 and stats2.pushed == 0 and stats2.pulled == 0


def test_google_sheet_exports_as_xlsx(env):
    _put_remote_doc(env, "Budget", "sheet-bytes",
                    mime="application/vnd.google-apps.spreadsheet")
    stats, ok = env.sync()
    assert ok and stats.pulled == 1
    assert (env.local / "Budget.xlsx").exists()


def test_google_doc_conversion_disabled(env):
    env.cfg["convert_google_docs"] = False
    _put_remote_doc(env, "Report", "doc-bytes")
    stats, ok = env.sync()
    assert ok
    assert not (env.local / "Report.docx").exists()
    assert env.base()["Report"]["kind"] == "gdoc"


def test_legacy_gdoc_base_migrates_to_export(env):
    # a record-only row written by an older version materializes on upgrade
    _put_remote_doc(env, "Report", "doc-bytes")
    env.cfg["convert_google_docs"] = False
    env.sync()                                   # records as gdoc
    assert env.base()["Report"]["kind"] == "gdoc"
    env.cfg["convert_google_docs"] = True
    stats, ok = env.sync()
    assert ok and stats.pulled == 1
    assert (env.local / "Report.docx").exists()
    assert env.base()["Report"]["kind"] == "file"


def test_google_doc_local_rename_renames_on_drive(env):
    fid = _put_remote_doc(env, "Report", "v1")
    env.sync()
    os.rename(env.local / "Report.docx", env.local / "Annual.docx")
    stats, ok = env.sync()
    assert ok and stats.renamed == 1 and stats.pushed == 0
    assert env.drive.files[fid]["name"] == "Annual"
    assert (env.local / "Annual.docx").exists()
    assert "Annual" in env.base() and "Report" not in env.base()
    stats2, ok2 = env.sync()
    assert ok2 and stats2.pushed == 0 and stats2.pulled == 0


def test_google_doc_remote_rename_reexports(env):
    # any Drive-side change (a rename included) bumps the file version, so a
    # rename surfaces as: old export removed, fresh export under the new name
    fid = _put_remote_doc(env, "Report", "v1")
    env.sync()
    env.drive.rename(fid, "Q3 Report")
    stats, ok = env.sync()
    assert ok
    assert (env.local / "Q3 Report.docx").exists()
    assert not (env.local / "Report.docx").exists()
    assert env.base()["Q3 Report"]["kind"] == "file"
    stats2, ok2 = env.sync()
    assert ok2 and stats2.pushed == 0 and stats2.pulled == 0


def test_google_doc_collision_bare_name_left_alone(env):
    # a local file with the doc's exact name (no ext) is not overwritten
    _put_remote_doc(env, "Report", "doc-bytes")
    wfile(env.local / "Report", b"user file")
    stats, ok = env.sync()
    assert ok or stats.errors == 1               # collision surfaced
    assert rfile(env.local / "Report") == b"user file"


def test_google_doc_collision_extension_file_not_overwritten(env):
    """A user file named like the export (Report.docx) must not be replaced.

    Regression: fold_exports assumed any same-named local file was our
    export, so the first pass exported the doc straight over it."""
    _put_remote_doc(env, "Report", "drive-bytes")
    wfile(env.local / "Report.docx", b"user docx")
    stats, ok = env.sync()
    assert not ok and stats.errors == 1
    assert rfile(env.local / "Report.docx") == b"user docx"
    assert "Report" not in env.base()          # nothing downloaded
    stats2, ok2 = env.sync()                   # stable, no churn/overwrite
    assert not ok2 and rfile(env.local / "Report.docx") == b"user docx"


def test_local_delete_of_exported_doc_propagates(env):
    fid = _put_remote_doc(env, "Report", "v1")
    env.sync()
    os.remove(env.local / "Report.docx")
    stats, ok = env.sync()
    assert ok and stats.deleted == 1
    assert "Report" not in env.base()
    assert env.drive.files[fid]["trashed"]


def test_empty_base_after_previous_sync_is_refused(env):
    """A lost base snapshot must not silently re-decide every path."""
    wfile(env.local / "a.txt", "v1")
    stats, ok = env.sync()
    assert ok
    assert env.store.kv_get(f"pair_synced:{env.pair['id']}") == "1"
    env.store._exec("DELETE FROM entries WHERE pair_id=?", (env.pair["id"],))

    stats, ok = env.sync()
    assert not ok and stats.errors == 1
    assert rfile(env.local / "a.txt") == b"v1"
    # nothing was pushed or pulled during the refused pass
    assert stats.pushed == 0 and stats.pulled == 0
    # the explicit recovery path is still available
    stats, ok = env.sync(force_full=True)
    assert ok


def test_empty_pair_base_is_not_mistaken_for_data_loss(env):
    """A legitimately empty pair (no files) keeps syncing after the marker
    records zero rows."""
    stats, ok = env.sync()
    assert ok
    assert env.store.kv_get(f"pair_synced:{env.pair['id']}") == "0"
    stats, ok = env.sync()
    assert ok and stats.errors == 0


def test_restore_conflict_runs_once(env):
    # local deleted + remote edited, policy 'both': restore once, then stop
    wfile(env.local / "a.txt", "v1")
    env.sync()
    os.remove(env.local / "a.txt")
    fid = env.drive._find_by_rel("a.txt")
    env.drive._fs_path(fid).write_bytes(b"edited on drive")
    env.drive._touch(fid)
    env.set_policy("both")
    stats, ok = env.sync()
    assert ok and stats.conflicts == 1
    copies = list(env.local.glob("a.conflict-*.txt"))
    assert len(copies) == 1 and copies[0].read_bytes() == b"edited on drive"
    stats2, ok2 = env.sync()
    assert ok2 and stats2.conflicts == 0         # no restore loop


# ---------------------------------------------------------- decide() unit checks
def test_decide_pure_function_mapping():
    md5 = lambda rel: "x" * 32
    local = {"a": {"kind": "file", "size": 1, "mtime": 1, "md5": None}}
    remote = {}
    base = {"a": {"kind": "file", "local_md5": "x" * 32, "local_size": 1,
                  "local_mtime": 1, "remote_md5": "y", "remote_version": "1",
                  "remote_id": "f1"}}
    ops = decide(local, remote, base, md5_of=md5)
    assert ("delete_local", "a") in ops

    remote2 = {"a": {"kind": "file", "id": "f1", "md5": "z" * 32,
                     "version": "2", "mtime": 5, "size": 3}}
    ops2 = decide(local, remote2, base, md5_of=md5)
    assert ("download", "a") in ops2


def test_scan_local_walk_and_ignore(tmp_path):
    wfile(tmp_path / "a.txt", "a")
    wfile(tmp_path / "sub" / "b.txt", "b")
    wfile(tmp_path / "sub" / "c.tmp", "c")
    items = scan_local(tmp_path, ["*.tmp"])
    assert "a.txt" in items and "sub/b.txt" in items
    assert "sub/c.tmp" not in items


def test_md5_file_roundtrip(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello world")
    assert md5_file(p) == "5eb63bbbe01eeed093cb22bb8f5acdc3"

# ------------------------------------------------------- delete safeguards
def test_deletes_off_keeps_local_when_remote_deleted(env):
    env.drive.put_remote_file("b.txt", "bravo")
    env.sync()                                        # download b.txt
    env.drive.trash(env.drive._find_by_rel("b.txt"))  # other user deletes it
    env.set_deletes(False)
    stats, ok = env.sync()
    assert ok
    assert rfile(env.local / "b.txt") == b"bravo"     # local copy kept
    assert env.base()["b.txt"]["deleted_remote"] == 1
    stats, ok = env.sync()                            # stable: nothing to do
    assert ok and stats.pushed == 0 and stats.pulled == 0 and stats.deleted == 0


def test_deletes_off_no_resurrection(env):
    wfile(env.local / "a.txt", "alpha")
    env.sync()                                        # upload
    env.drive.trash(env.drive._find_by_rel("a.txt"))
    env.set_deletes(False)
    env.sync()                                        # acknowledge, keep local
    env.sync()                                        # must not re-upload
    with pytest.raises(Exception):
        env.drive.read_remote("a.txt")                # still absent remotely


def test_deletes_off_keeps_remote_when_local_deleted(env):
    wfile(env.local / "a.txt", "alpha")
    env.sync()                                        # upload
    (env.local / "a.txt").unlink()                    # user deletes locally
    env.set_deletes(False)
    stats, ok = env.sync()
    assert ok
    assert env.drive.read_remote("a.txt") == b"alpha"  # remote copy kept
    assert env.base()["a.txt"]["deleted_local"] == 1
    stats, ok = env.sync()                             # stable
    assert ok and stats.pushed == 0 and stats.deleted == 0


def test_reenabling_deletes_catches_up(env):
    wfile(env.local / "a.txt", "alpha")
    env.sync()
    env.drive.trash(env.drive._find_by_rel("a.txt"))
    env.set_deletes(False)
    env.sync()                                        # kept + tombstoned
    env.set_deletes(True)
    stats, ok = env.sync()
    assert ok and stats.deleted == 1                  # local copy caught up
    assert not (env.local / "a.txt").exists()


def test_local_deletes_go_to_trash(env, tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    from drivesync.config import app_dir
    wfile(env.local / "a.txt", "alpha")
    env.sync()
    env.drive.trash(env.drive._find_by_rel("a.txt"))
    stats, ok = env.sync()                            # deletes ON: propagate
    assert ok and stats.deleted == 1
    assert not (env.local / "a.txt").exists()
    trash_root = app_dir() / "trash" / str(env.pair["id"])
    found = list(trash_root.rglob("a.txt"))
    assert found and found[0].read_bytes() == b"alpha"


def test_safety_valve_blocks_mass_deletion(env):
    for i in range(12):
        env.drive.put_remote_file(f"f{i:02}.txt", f"content-{i}")
    env.sync()                                        # download 12 files
    for i in range(12):
        env.drive.trash(env.drive._find_by_rel(f"f{i:02}.txt"))
    env.cfg["delete_safety_limit"] = 10
    stats, ok = env.sync()
    assert not ok and stats.deleted == 0              # nothing destroyed
    events = env.store.recent_events(pair_id=env.pair["id"], limit=50)
    assert any(e["action"] == "safety-stop" for e in events)
    assert len(list(env.local.glob("f*.txt"))) == 12  # files still on disk


def test_safety_limit_zero_means_unlimited(env, tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    for i in range(12):
        env.drive.put_remote_file(f"f{i:02}.txt", f"content-{i}")
    env.sync()
    for i in range(12):
        env.drive.trash(env.drive._find_by_rel(f"f{i:02}.txt"))
    env.cfg["delete_safety_limit"] = 0
    stats, ok = env.sync()
    assert ok and stats.deleted == 12

# ---------------------------------------------------- parallel transfers
def test_parallel_downloads_nested_dirs(env):
    env.cfg["transfer_concurrency"] = 8
    dirs = ["a", "b", "c", "a/x", "b/y", "c/z"]
    n = 0
    for d in dirs:
        for i in range(5):
            env.drive.put_remote_file(f"{d}/f{i}.txt", f"content-{d}-{i}")
            n += 1
    stats, ok = env.sync()
    assert ok and stats.pulled == n
    for d in dirs:
        for i in range(5):
            assert rfile(env.local / d / f"f{i}.txt") == f"content-{d}-{i}".encode()


def test_parallel_uploads_nested_dirs(env):
    env.cfg["transfer_concurrency"] = 8
    n = 0
    for d in ["a", "b", "a/x"]:
        for i in range(5):
            wfile(env.local / d / f"f{i}.txt", f"data-{d}-{i}")
            n += 1
    stats, ok = env.sync()
    assert ok and stats.pushed == n + 3  # 15 files + 3 created folders
    for d in ["a", "b", "a/x"]:
        for i in range(5):
            assert env.drive.read_remote(f"{d}/f{i}.txt") == f"data-{d}-{i}".encode()


def test_concurrency_one_still_works(env):
    env.cfg["transfer_concurrency"] = 1
    for i in range(6):
        env.drive.put_remote_file(f"s{i}.txt", f"v{i}")
    stats, ok = env.sync()
    assert ok and stats.pulled == 6
