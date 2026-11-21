"""Phase 3.2 — resumable transfers.

Covers the three seams the phase added:
  * executor retry with backoff, transient-vs-fatal classification
  * resumable uploads: session URI persisted in kv, resumed across passes
    and in-pass retries; stale sessions discarded when the file changed
  * resumable downloads: .part files survive a broken transfer and the
    next attempt continues from them

MockDrive emulates Google's session semantics faithfully (a failure
injection advances the server-side offset before raising, exactly like a
chunk the server received but whose reply was lost).
"""

import json
import os
import time

import pytest
import requests
from drivesync.drive import Drive

from drivesync.drive import PART_SUFFIX, DriveError
from drivesync.engine import build_plan, execute_plan, is_transient_transfer_error


def wfile(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())


@pytest.fixture
def big(env):
    """A pair with a 12 KiB local file and a 1 KiB resumable threshold."""
    env.cfg["resumable_threshold"] = 1024
    env.cfg["transfer_retry_base"] = 0.001
    path = env.local / "big.bin"
    wfile(path, os.urandom(12288))
    return path


# ---- error classification -------------------------------------------------

def test_transient_vs_fatal_classification():
    assert is_transient_transfer_error(
        DriveError("POST x -> 500: backend error"))
    assert is_transient_transfer_error(DriveError("GET x -> 429: rate limit"))
    assert is_transient_transfer_error(
        DriveError("PUT y failed: ConnectionError(timeout)"))
    assert not is_transient_transfer_error(
        DriveError("PATCH z -> 403: forbidden"))
    assert not is_transient_transfer_error(OSError(13, "permission denied"))


# ---- uploads: session persistence + resume --------------------------------

def test_large_upload_uses_resumable_session(env, big):
    stats, ok = env.sync()
    assert ok and stats.pushed == 1
    assert env.drive.calls["upload_resumable"] == 1
    assert env.drive.calls.get("upload", 0) == 0
    assert env.drive.read_remote("big.bin") == big.read_bytes()
    assert env.store.kv_get("upsession:1:big.bin") is None


def test_small_upload_stays_plain(env):
    env.cfg["resumable_threshold"] = 1 << 20
    wfile(env.local / "tiny.txt", "small")
    env.sync()
    assert env.drive.calls.get("upload", 0) == 1
    assert env.drive.calls["upload_resumable"] == 0


def test_failed_upload_persists_session_and_resumes_next_pass(env, big):
    """Pass 1 breaks mid-session (retries off): the kv store keeps the
    session URI, the mock holds the received bytes, pass 2 continues."""
    env.cfg["transfer_retries"] = 0
    env.drive.fail_uploads_after = 4096
    stats, ok = env.sync()
    assert ok is False and stats.errors == 1

    raw = env.store.kv_get("upsession:1:big.bin")
    assert raw
    rec = json.loads(raw)
    assert rec["size"] == 12288
    session = rec["session"]
    assert env.drive.sessions[session]["offset"] == 4096   # server holds 4 KiB

    # pass 2: same three-way state, the upload re-plans and resumes
    stats2, ok2 = env.sync()
    assert ok2 and stats2.pushed == 1
    assert env.drive.read_remote("big.bin") == big.read_bytes()
    assert env.store.kv_get("upsession:1:big.bin") is None


def test_in_pass_retry_completes_and_counts(env, big):
    """With retries on, the same apply attempt recovers after the injected
    500 — the ExecReport counts the retry and the pass succeeds."""
    env.drive.fail_uploads_after = 2048
    plan = build_plan(env.store, env.cfg, env.pair, env.drive)
    report = execute_plan(env.store, env.cfg, env.pair, env.drive, plan)
    assert report.ok and report.retries == 1 and report.failures == []
    assert env.drive.read_remote("big.bin") == big.read_bytes()
    assert env.store.kv_get("upsession:1:big.bin") is None


def test_fatal_error_is_not_retried(env):
    """A 403 at session init must surface as a failure with zero retries."""
    env.cfg["resumable_threshold"] = 1024
    env.cfg["transfer_retries"] = 3
    env.drive.fail_uploads_status = 403
    wfile(env.local / "doc.bin", os.urandom(4096))
    stats, ok = env.sync()
    assert ok is False and stats.errors == 1
    assert stats.pushed == 0


def test_changed_file_discards_stale_session(env, big):
    """A session belongs to the bytes that opened it: after the local file
    changes, the saved URI is ignored and a fresh session completes."""
    env.cfg["transfer_retries"] = 0
    env.drive.fail_uploads_after = 4096
    env.sync()                                  # fails, session saved
    session_old = json.loads(env.store.kv_get("upsession:1:big.bin"))["session"]

    big.write_bytes(os.urandom(9999))           # different size and mtime
    stats, ok = env.sync()
    assert ok and stats.pushed == 1
    assert env.drive.read_remote("big.bin") == big.read_bytes()
    # a fresh session completed the push; the stale one was left untouched
    assert env.drive.sessions[session_old]["offset"] == 4096
    assert env.drive.calls.get("session_resumes", 0) == 0


def test_session_resume_updates_existing_file(env):
    """update_content over a tracked file goes through the resumable
    protocol too, including resume after an injected break."""
    env.cfg["resumable_threshold"] = 1024
    env.cfg["transfer_retries"] = 0
    path = env.local / "tracked.bin"
    wfile(path, os.urandom(8192))
    env.sync()
    assert env.drive.calls["upload_resumable"] == 1

    path.write_bytes(os.urandom(8192))          # same size, new mtime/content
    # guarantee the mtime change is visible even on coarse-timestamp FS
    later = time.time() + 5
    os.utime(path, (later, later))
    env.drive.fail_uploads_after = 2048
    stats, ok = env.sync()
    assert ok is False                          # broke mid-update, session kept
    env.cfg["transfer_retries"] = 2
    stats2, ok2 = env.sync()
    assert ok2
    # the retry pass re-initiated the session protocol once and resumed
    # the stored session instead of restarting from zero
    assert env.drive.calls.get("update_content_resumable", 0) == 2
    assert env.drive.calls.get("session_resumes", 0) == 1
    assert env.drive.read_remote("tracked.bin") == path.read_bytes()


# ---- downloads: .part resume -----------------------------------------------

def test_download_part_resume_two_passes(env):
    """A download that dies mid-stream leaves the .drivesync.part beside
    the destination; the next pass continues from it instead of
    re-downloading from byte zero. The part is invisible to the scanner."""
    env.cfg["transfer_retries"] = 0
    content = os.urandom(10000)
    env.drive.put_remote_file("movie.bin", content)
    env.drive.download_budget = 3000
    stats, ok = env.sync()
    assert ok is False and stats.errors == 1
    part = env.local / f"movie.bin{PART_SUFFIX}"
    assert part.exists() and part.stat().st_size == 3000
    assert not (env.local / "movie.bin").exists()

    stats2, ok2 = env.sync()                     # budget consumed: completes
    assert ok2
    assert (env.local / "movie.bin").read_bytes() == content
    assert not part.exists()                     # cleaned by the atomic rename
    # the part never entered the plan: no upload of a half-file
    assert env.drive.read_remote("movie.bin") == content


def test_complete_part_finalizes_without_more_bytes(env):
    """A crash between the last byte and the atomic rename leaves a
    complete part; the next pass finalizes it without another download."""
    content = os.urandom(5000)
    env.drive.put_remote_file("done.bin", content)
    part = env.local / f"done.bin{PART_SUFFIX}"
    part.write_bytes(content)
    stats, ok = env.sync()
    assert ok and stats.pulled == 1
    assert (env.local / "done.bin").read_bytes() == content
    assert not part.exists()


def test_download_resume_repeated_breaks(env):
    """Three breaks in a row still converge: each pass extends the part."""
    env.cfg["transfer_retries"] = 0
    content = os.urandom(9000)
    env.drive.put_remote_file("z.bin", content)
    for budget in (2000, 2000, 2000):
        env.drive.download_budget = budget
        stats, ok = env.sync()
        assert ok is False
        assert (env.local / f"z.bin{PART_SUFFIX}").stat().st_size >= budget
    env.drive.download_budget = None
    stats, ok = env.sync()
    assert ok
    assert (env.local / "z.bin").read_bytes() == content


# ---- plan/preview interplay -------------------------------------------------

def test_planned_upload_carries_size_for_resume_decision(env, big):
    plan = build_plan(env.store, env.cfg, env.pair, env.drive)
    op = next(p for p in plan.ops if p.name == "upload")
    assert op.size == 12288 and op.transfer


# ---- real client protocol tests (stubbed HTTP) ------------------------------

class FakeResp:
    def __init__(self, status_code, headers=None, text="", body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._body = body

    def json(self):
        return json.loads(self.text) if self.text else {}

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size=1):
        yield self._body


class ScriptedSession:
    """Returns queued responses in order; records every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def put(self, url, data=None, headers=None, timeout=None, **kw):
        self.calls.append({"method": "PUT", "url": url, "data": data,
                           "headers": headers})
        if not self.responses:
            raise requests.ConnectionError("script exhausted")
        return self.responses.pop(0)

    def get(self, url, params=None, headers=None, stream=False, timeout=None,
            **kw):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        return self.responses.pop(0)

    def request(self, method, url, timeout=None, **kw):
        # metadata calls (e.g. the resumable init POST) go through _request
        self.calls.append({"method": method, "url": url,
                           "headers": kw.get("headers")})
        return self.responses.pop(0)


def test_session_status_reads_the_server_offset():
    d = Drive(lambda: "tok")
    d._session = ScriptedSession([
        FakeResp(308, headers={"Range": "bytes=0-4095"}),
        FakeResp(308),
        FakeResp(200, text='{"id": "x"}'),
        FakeResp(404, text="gone"),
    ])
    assert d._session_status("http://s/1", 10000) == ("offset", 4096)
    assert d._session_status("http://s/1", 10000) == ("offset", 0)
    assert d._session_status("http://s/1", 10000) == ("done", {"id": "x"})
    assert d._session_status("http://s/1", 10000) == ("dead", None)


def test_resumable_chunks_send_content_ranges(monkeypatch, tmp_path):
    import drivesync.drive as drv
    monkeypatch.setattr(drv.time, "sleep", lambda s: None)
    f = tmp_path / "f.bin"
    f.write_bytes(b"A" * 1000)
    d = Drive(lambda: "tok")
    d._session = ScriptedSession([
        FakeResp(200, headers={"Location": "http://sess/1"}),   # init
        FakeResp(308, headers={"Range": "bytes=0-599"}),        # chunk 1 ack
        FakeResp(200, text='{"id": "f1", "size": "1000"}'),     # final chunk
    ])
    meta = d.upload_resumable(str(f), "f.bin", "parent", chunk_bytes=600)
    assert meta["id"] == "f1"
    puts = [c for c in d._session.calls if c["method"] == "PUT"]
    assert puts[0]["headers"]["Content-Range"] == "bytes 0-599/*"
    assert puts[0]["data"] == b"A" * 600
    # the 308 said the server holds 600: only the remainder is sent
    assert puts[1]["headers"]["Content-Range"] == "bytes 600-999/1000"
    assert puts[1]["data"] == b"A" * 400


def test_resumable_recovers_from_broken_chunk(monkeypatch, tmp_path):
    import drivesync.drive as drv
    monkeypatch.setattr(drv.time, "sleep", lambda s: None)
    f = tmp_path / "f.bin"
    f.write_bytes(b"B" * 1000)
    d = Drive(lambda: "tok")
    session = ScriptedSession([
        FakeResp(200, headers={"Location": "http://sess/2"}),   # init
        FakeResp(308, headers={"Range": "bytes=0-599"}),        # status query
        FakeResp(200, text='{"id": "f2"}'),                     # final chunk
    ])
    d._session = session

    def put(url, data=None, headers=None, timeout=None, **kw):
        session.calls.append({"method": "PUT", "headers": headers,
                              "data": data})
        if len(session.calls) == 1:             # first chunk PUT dies mid-air
            raise requests.ConnectionError("connection reset")
        return session.responses.pop(0)

    session.put = put
    meta = d.upload_resumable(str(f), "f.bin", "parent", chunk_bytes=600)
    assert meta["id"] == "f2"
    data_puts = [c for c in session.calls if c.get("data") is not None]
    assert data_puts[-1]["data"] == b"B" * 400   # only the missing tail


def test_stream_to_resumes_from_part_and_finalizes(tmp_path):
    dest = tmp_path / "x.bin"
    part = tmp_path / f"x.bin{PART_SUFFIX}"
    part.write_bytes(b"HEAD")
    d = Drive(lambda: "tok")
    d._session = ScriptedSession([FakeResp(206, body=b"TAIL")])
    d._stream_to("http://x/files/f", {"alt": "media"}, str(dest))
    assert dest.read_bytes() == b"HEADTAIL"
    assert not part.exists()
    assert d._session.calls[0]["headers"]["Range"] == "bytes=4-"


def test_stream_to_restarts_when_server_ignores_range(tmp_path):
    dest = tmp_path / "y.bin"
    part = tmp_path / f"y.bin{PART_SUFFIX}"
    part.write_bytes(b"GARBAGE")
    d = Drive(lambda: "tok")
    d._session = ScriptedSession([FakeResp(200, body=b"FULL")])
    d._stream_to("http://x/files/f", {"alt": "media"}, str(dest))
    assert dest.read_bytes() == b"FULL"          # untrusted part discarded
    assert not part.exists()


def test_stream_to_finalizes_complete_part_on_416(tmp_path):
    """A crash landed between the last byte and the rename: the server says
    416 for the out-of-range resume and the client finalizes the part."""
    dest = tmp_path / "z.bin"
    part = tmp_path / f"z.bin{PART_SUFFIX}"
    part.write_bytes(b"ALLBYTES")
    d = Drive(lambda: "tok")
    d._session = ScriptedSession([FakeResp(416)])
    d._stream_to("http://x/files/f", {"alt": "media"}, str(dest))
    assert dest.read_bytes() == b"ALLBYTES"
    assert not part.exists()
