"""Phase 3.1 — scanner/planner/executor architecture.

The behavior contract is already covered by the engine suite (sync_pair
must behave exactly as before the split — that suite passing unchanged IS
the parity test). These tests cover the new seams: truthful op reasons,
Plan/PlannedOp structure, ExecReport per-op outcomes, and the upgraded
preview payload.
"""


from drivesync.engine import build_plan, execute_plan, preview_pair, sync_pair


def wfile(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())


def reasons_for(plan, name=None):
    return {p.rel: p.reason for p in plan.ops
            if name is None or p.name == name}


# ---- truthful reasons ---------------------------------------------------

def test_reason_new_local_file(env):
    wfile(env.local / "new.txt", "data")
    plan = build_plan(env.store, env.cfg, env.pair, env.drive)
    assert reasons_for(plan, "upload")["new.txt"] == "new local file"


def test_reason_local_edit_and_remote_edit(env):
    wfile(env.local / "a.txt", "v1")
    env.sync()
    wfile(env.local / "a.txt", "v2-changed-longer")
    plan = build_plan(env.store, env.cfg, env.pair, env.drive)
    assert reasons_for(plan)["a.txt"] == "local changes since last sync"

    wfile(env.local / "a.txt", "v1")
    env.sync()
    fid = env.drive._find_by_rel("a.txt")
    env.drive._fs_path(fid).write_bytes(b"edited-on-drive-2")
    env.drive._touch(fid)
    plan = build_plan(env.store, env.cfg, env.pair, env.drive)
    assert reasons_for(plan)["a.txt"] == "changed on Drive since last sync"


def test_reason_deletion_propagations(env):
    wfile(env.local / "gone.txt", "bye")
    env.sync()
    import os
    os.remove(env.local / "gone.txt")
    plan = build_plan(env.store, env.cfg, env.pair, env.drive)
    assert reasons_for(plan, "delete_remote")["gone.txt"] == \
        "local file deleted — propagating to Drive"

    wfile(env.local / "cloud.txt", "hi")
    env.sync()
    env.drive.trash(env.drive._find_by_rel("cloud.txt"))
    plan = build_plan(env.store, env.cfg, env.pair, env.drive)
    assert reasons_for(plan, "delete_local")["cloud.txt"] == \
        "deleted on Drive — propagating locally"


def test_reason_conflict_policy_newer(env):
    wfile(env.local / "c.txt", "original")
    env.sync()
    wfile(env.local / "c.txt", "local edit longer")
    fid = env.drive._find_by_rel("c.txt")
    env.drive._fs_path(fid).write_bytes(b"remote edit longer")
    env.drive._touch(fid)
    plan = build_plan(env.store, env.cfg, env.pair, env.drive)
    reason = reasons_for(plan)["c.txt"]
    assert reason.startswith("conflict — policy 'newer' keeps the")
    assert reason.endswith("(newer)")


def test_reason_deletes_off_ack(env):
    wfile(env.local / "kept.txt", "data")
    env.sync()
    env.set_deletes(False)
    import os
    os.remove(env.local / "kept.txt")
    plan = build_plan(env.store, env.cfg, env.pair, env.drive)
    assert reasons_for(plan, "ack_local_delete")["kept.txt"] == \
        "local file deleted — deletes are off, Drive copy kept"


def test_reason_rename_and_mkdir(env):
    wfile(env.local / "old-name.txt", "payload")
    env.sync()
    (env.local / "old-name.txt").rename(env.local / "renamed.txt")
    plan = build_plan(env.store, env.cfg, env.pair, env.drive)
    assert reasons_for(plan, "rename_local")["old-name.txt"] == \
        "renamed locally — now renamed.txt"

    (env.local / "folder").mkdir()
    wfile(env.local / "folder" / "x.txt", "x")
    plan = build_plan(env.store, env.cfg, env.pair, env.drive)
    assert reasons_for(plan, "mkdir_remote")["folder"] == "new local folder"


# ---- Plan structure ------------------------------------------------------

def test_plan_ordering_and_metadata(env):
    (env.local / "sub").mkdir()
    wfile(env.local / "sub" / "f.txt", "f")
    wfile(env.local / "top.txt", "t")
    plan = build_plan(env.store, env.cfg, env.pair, env.drive)
    names = [p.name for p in plan.ops]
    assert names.index("mkdir_remote") < names.index("upload")
    assert plan.counts.get("upload") == 2 and plan.counts.get("mkdir_remote") == 1
    assert plan.error is None and plan.blocked == 0
    assert plan.scan["local_n"] == 3            # sub + sub/f.txt + top.txt
    assert plan.direction == "bidirectional"


def test_plan_carries_transfer_sizes(env):
    wfile(env.local / "sized.bin", "12345")
    plan = build_plan(env.store, env.cfg, env.pair, env.drive)
    op = next(p for p in plan.ops if p.name == "upload")
    assert op.size == 5 and op.transfer is True and op.destructive is False


def test_plan_blocked_ops_are_counted_not_listed(env):
    for i in range(8):
        wfile(env.local / f"d{i}.txt", f"v{i}")
    env.sync()
    import os
    for i in range(8):
        os.remove(env.local / f"d{i}.txt")
    env.cfg["delete_safety_limit"] = 3
    plan = build_plan(env.store, env.cfg, env.pair, env.drive)
    assert plan.blocked == 8
    assert all(not p.destructive for p in plan.ops)
    assert plan.counts.get("delete_remote") is None


def test_plan_reports_fatal_scan_error(env):
    import shutil
    shutil.rmtree(env.local)
    plan = build_plan(env.store, env.cfg, env.pair, env.drive)
    assert plan.error and plan.error.startswith("local path missing")
    assert plan.ops == []


# ---- executor report ------------------------------------------------------

def test_execute_plan_records_per_op_failures(env):
    """A failing upload does not abort the pass: the report names the op,
    the other file still lands, and ok is False."""
    from drivesync.drive import DriveError

    class FlakyDrive:
        def __init__(self, real):
            self._real = real
            self.calls = 0

        def __getattr__(self, name):
            attr = getattr(self._real, name)
            if name == "upload":
                # Fail by file name, not call order: uploads run in a
                # thread pool, so "the first call" is not deterministic.
                def flaky(local_path, remote_name, *a, **k):
                    self.calls += 1
                    if remote_name == "bad.txt":
                        raise DriveError("507: simulated storage failure")
                    return attr(local_path, remote_name, *a, **k)
                return flaky
            return attr

    wfile(env.local / "bad.txt", "x")
    wfile(env.local / "good.txt", "y")
    flaky = FlakyDrive(env.drive)
    plan = build_plan(env.store, env.cfg, env.pair, flaky)   # scan passes through
    report = execute_plan(env.store, env.cfg, env.pair, flaky, plan)
    assert report.ok is False
    assert [("upload", "bad.txt")] == [(f[0], f[1]) for f in report.failures]
    assert "507" in report.failures[0][2]
    assert env.drive.read_remote("good.txt") == b"y"    # partial progress landed
    assert report.stats.pushed == 1 and report.stats.errors == 1


def test_sync_pair_matches_plan_then_execute(env):
    """Orchestration parity: scan->plan->execute reaches the same state as
    the old monolith — both files upload, base rows match."""
    wfile(env.local / "one.txt", "1")
    wfile(env.local / "two.txt", "2")
    stats, ok = sync_pair(env.store, env.cfg, env.pair, env.drive)
    assert ok and stats.pushed == 2
    assert env.drive.read_remote("one.txt") == b"1"
    assert env.base()["two.txt"]["kind"] == "file"


def test_sync_pair_reports_blocked_as_failure(env):
    """A tripped safety gate still fails the pass (parity with Phase 2.6)."""
    for i in range(8):
        wfile(env.local / f"m{i}.txt", f"v{i}")
    env.sync()
    import os
    for i in range(8):
        os.remove(env.local / f"m{i}.txt")
    env.cfg["delete_safety_limit"] = 3
    stats, ok = sync_pair(env.store, env.cfg, env.pair, env.drive)
    assert ok is False and stats.errors >= 1


# ---- upgraded preview payload --------------------------------------------

def test_preview_includes_aligned_reasons_and_sizes(env):
    wfile(env.local / "pic.bin", "0123456789")
    plan_reasons = preview_pair(env.store, env.cfg, env.pair, env.drive)
    assert len(plan_reasons["reasons"]) == len(plan_reasons["ops"]) == \
        len(plan_reasons["sizes"]) == len(plan_reasons["destructive"])
    idx = next(i for i, op in enumerate(plan_reasons["ops"])
               if op[0] == "upload")
    assert plan_reasons["reasons"][idx] == "new local file"
    assert plan_reasons["sizes"][idx] == 10
    assert plan_reasons["destructive"][idx] is False
    assert plan_reasons["scan"]["local_n"] == 1   # just the file (root is "")


def test_preview_reasons_for_conflict_copy_policy(env):
    wfile(env.local / "cc.txt", "original")
    env.sync()
    env.set_policy("both")
    wfile(env.local / "cc.txt", "local edit longer")
    fid = env.drive._find_by_rel("cc.txt")
    env.drive._fs_path(fid).write_bytes(b"remote edit longer")
    env.drive._touch(fid)
    res = preview_pair(env.store, env.cfg, env.pair, env.drive)
    idx = next(i for i, op in enumerate(res["ops"])
               if op[0] == "conflict_both")
    assert res["reasons"][idx] == "conflict — both versions kept (policy 'both')"
