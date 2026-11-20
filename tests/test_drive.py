"""Drive client error-translation tests: 403 responses get actionable hints."""

import pytest

from drivesync.drive import (Drive, DriveError, FOLDER_MIME, SHORTCUT_MIME,
                             folders_for_parent, resolve_folder_entry,
                             safe_name)

DISABLED = ('{"error": {"code": 403, "message": "Google Drive API has not been used '
            'in project 123 before or it is disabled.", "status": "PERMISSION_DENIED", '
            '"details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", '
            '"reason": "SERVICE_DISABLED", "domain": "googleapis.com", '
            '"metadata": {"service": "drive.googleapis.com", '
            '"consumer": "projects/802449092947"}}]}}')
GENERIC = ('{"error": {"code": 403, "message": "Permission denied on the service.", '
           '"errors": [{"message": "Permission denied on the service.", '
           '"domain": "global", "reason": "forbidden"}], '
           '"status": "PERMISSION_DENIED"}}')


class FakeResp:
    def __init__(self, status_code, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def json(self):
        import json
        return json.loads(self.text)


def test_safe_name_never_escapes_the_sync_root():
    assert safe_name("photo.jpg") == "photo.jpg"
    assert safe_name("..") == "__"
    assert safe_name(".") == "_"
    assert safe_name("") == "_"
    assert safe_name("a/b") == "a_b"
    assert safe_name("nul\x00byte") == "nulbyte"


class FakeSession:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def request(self, *a, **kw):
        self.calls.append(kw)
        return self.resp


def _drive(resp):
    d = Drive(lambda: "tok")
    d._session = FakeSession(resp)
    return d


def test_every_request_carries_authorization():
    """Regression: _request must attach the bearer token to every call."""
    d = _drive(FakeResp(200, '{"files": [], "id": "x"}'))
    d.about()
    d.get("abc")
    d.list_children("root")
    d.mkdir("newfolder", "root")
    d.rename("x", "renamed")
    d.trash("x")
    assert len(d._session.calls) == 6
    for kw in d._session.calls:
        assert kw["headers"]["Authorization"] == "Bearer tok"


def test_upload_rewinds_stream_after_401(tmp_path):
    """The 401 retry must replay the whole file — a half-consumed stream
    would upload a truncated copy and still look successful."""
    from drivesync.drive import Drive

    local = tmp_path / "a.txt"
    local.write_text("hello world")

    class Session:
        def __init__(self):
            self.calls = 0
            self.bodies = []

        def request(self, *a, **kw):
            self.calls += 1
            body = kw.get("data")
            self.bodies.append(b"".join(body) if body is not None else b"")
            if self.calls == 1:
                return FakeResp(401, "unauthorized")
            return FakeResp(200, '{"id": "f1"}')

    d = Drive(lambda: "tok")
    d._session = Session()
    assert d.upload(str(local), "a.txt", "root")["id"] == "f1"
    assert d._session.calls == 2
    assert d._session.bodies[0] == d._session.bodies[1]
    assert b"hello world" in d._session.bodies[1]


def test_throttled_body_rewinds(tmp_path):
    from drivesync.drive import _Throttled

    local = tmp_path / "a.txt"
    local.write_text("hello world")
    with open(local, "rb") as fh:
        body = _Throttled(fh, None)
        assert body.read() == b"hello world"
        body.rewind()
        assert body.read() == b"hello world"


def test_caller_headers_survive_and_auth_added():
    d = _drive(FakeResp(200, '{"ok": true}'))
    d._request("PATCH", "https://example.invalid/x",
               headers={"Content-Type": "text/plain"}, json={})
    sent = d._session.calls[0]["headers"]
    assert sent["Content-Type"] == "text/plain"
    assert sent["Authorization"] == "Bearer tok"


def test_401_retry_re_resolves_token():
    tokens = iter(["t0", "t1"])
    d = Drive(lambda: next(tokens))
    d._session = FakeSession(FakeResp(401, '{"error": {"code": 401}}'))
    with pytest.raises(DriveError):
        d.about()
    assert d._session.calls[0]["headers"]["Authorization"] == "Bearer t0"
    assert d._session.calls[1]["headers"]["Authorization"] == "Bearer t1"


def test_upload_sends_multipart_content_type():
    import tempfile, os
    d = _drive(FakeResp(200, '{"id": "f1"}'))
    fd, path = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b"hello")
        d.upload(path, "hello.txt", "root")
    finally:
        os.remove(path)
    sent = d._session.calls[0]["headers"]
    assert sent["Content-Type"].startswith("multipart/related; boundary=")
    assert sent["Authorization"] == "Bearer tok"


def test_download_retries_429(monkeypatch, tmp_path):
    import drivesync.drive as drive_mod

    monkeypatch.setattr(drive_mod.time, "sleep", lambda s: None)

    class Stream:
        def __init__(self, status, chunks=()):
            self.status_code, self._chunks = status, list(chunks)
            self.text, self.headers = "", {}
            self.closed = False

        def iter_content(self, chunk_size):
            return iter(self._chunks)

        def close(self):
            self.closed = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    class Session:
        def __init__(self):
            self.calls = 0
            self.boundaries = []

        def get(self, url, **kw):
            self.calls += 1
            self.boundaries.append(kw["params"].get("supportsAllDrives"))
            if self.calls == 1:
                return Stream(429)
            return Stream(200, [b"hello ", b"world"])

    d = Drive(lambda: "tok")
    d._session = Session()
    dest = tmp_path / "a.txt"
    d.download("f1", str(dest))
    assert dest.read_bytes() == b"hello world"
    assert d._session.calls == 2
    assert d._session.boundaries == ["true", "true"]


def test_upload_uses_a_random_multipart_boundary():
    from drivesync.drive import _multipart

    class FakeFile:
        def read(self, n=-1):
            return b""

    with_boundaries = [_multipart({"name": "a"}, FakeFile(), "text/plain")[1]
                      for _ in range(2)]
    assert with_boundaries[0] != with_boundaries[1]
    assert all(b.startswith("drivesync") for b in with_boundaries)


def test_403_disabled_api_hint_with_exact_project_url():
    d = _drive(FakeResp(403, DISABLED))
    with pytest.raises(DriveError) as e:
        d.about()
    msg = str(e.value)
    assert "NOT enabled" in msg
    assert ("overview?project=802449092947" in msg)  # consumer from ErrorInfo metadata
    assert "drive.googleapis.com" in msg


def test_403_generic_hint():
    d = _drive(FakeResp(403, GENERIC))
    with pytest.raises(DriveError) as e:
        d.about()
    msg = str(e.value)
    assert "test user" in msg
    assert "@gmail.com" in msg
    assert "overview?project=<PROJECT_NUMBER>" in msg  # no metadata -> placeholder


def test_403_unparsable_body_still_hints():
    d = _drive(FakeResp(403, "<html>something broke</html>"))
    with pytest.raises(DriveError) as e:
        d.about()
    msg = str(e.value)
    assert "test user" in msg
    assert "403" in msg


def test_401_reauth_hint():
    d = _drive(FakeResp(401, '{"error": {"code": 401, "message": "Request is missing '
               'required authentication credential.", "status": "UNAUTHENTICATED"}}'))
    with pytest.raises(DriveError) as e:
        d.about()
    msg = str(e.value)
    assert "drivesync auth" in msg
    assert "OAuth client" in msg


def test_404_has_no_hint():
    d = _drive(FakeResp(404, '{"error": "not found"}'))
    with pytest.raises(DriveError) as e:
        d.about()
    assert "404" in str(e.value)
    assert "PERMISSION_DENIED" not in str(e.value)


RATE_LIMITED = ('{"error": {"code": 403, "message": "Rate limit exceeded", '
                '"errors": [{"reason": "rateLimitExceeded", "domain": "global"}], '
                '"status": "RESOURCE_EXHAUSTED"}}')
QUOTA_EXCEEDED = ('{"error": {"code": 403, "message": "Storage quota exceeded", '
                  '"errors": [{"reason": "storageQuotaExceeded", '
                  '"domain": "global"}]}}')


def test_explain_rate_limit_before_drive_api_hint():
    from drivesync.drive import _explain
    hint = _explain(FakeResp(403, RATE_LIMITED))
    assert "rate-limited" in hint
    assert "transfer_concurrency" in hint
    assert "Drive API is NOT enabled" not in hint


def test_explain_storage_quota():
    from drivesync.drive import _explain
    hint = _explain(FakeResp(403, QUOTA_EXCEEDED))
    assert "storage is full" in hint


class SeqSession:
    """Returns queued responses, repeating the last one once exhausted."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.kw = []

    def request(self, *a, **kw):
        self.calls += 1
        self.kw.append(kw)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def test_request_retries_429_with_backoff(monkeypatch):
    import drivesync.drive as drive_mod

    delays = []
    monkeypatch.setattr(drive_mod.time, "sleep", delays.append)
    d = Drive(lambda: "tok")
    d._session = SeqSession([
        FakeResp(429, RATE_LIMITED),
        FakeResp(429, RATE_LIMITED),
        FakeResp(200, '{"user": {"emailAddress": "me@x.com"}}'),
    ])
    assert d.about()["user"]["emailAddress"] == "me@x.com"
    assert d._session.calls == 3
    assert len(delays) == 2 and all(d >= 0 for d in delays)


def test_request_honours_retry_after_header(monkeypatch):
    import drivesync.drive as drive_mod

    delays = []
    monkeypatch.setattr(drive_mod.time, "sleep", delays.append)
    d = Drive(lambda: "tok")
    d._session = SeqSession([
        FakeResp(503, "", {"Retry-After": "2"}),
        FakeResp(200, '{"user": {}}'),
    ])
    assert d.about() == {"user": {}}
    assert delays == [2.0]


def test_request_gives_up_after_four_tries(monkeypatch):
    import drivesync.drive as drive_mod

    monkeypatch.setattr(drive_mod.time, "sleep", lambda s: None)
    d = Drive(lambda: "tok")
    d._session = SeqSession([FakeResp(429, RATE_LIMITED)])
    with pytest.raises(DriveError) as exc:
        d.about()
    assert "429" in str(exc.value)
    assert d._session.calls == 4


def test_post_500_is_not_replayed(monkeypatch):
    """A 500 on create is ambiguous: the file may exist, so do not retry."""
    import drivesync.drive as drive_mod

    monkeypatch.setattr(drive_mod.time, "sleep", lambda s: None)
    d = Drive(lambda: "tok")
    d._session = SeqSession([FakeResp(500, "{}")])
    with pytest.raises(DriveError):
        d.mkdir("new", "root")
    assert d._session.calls == 1


def test_file_calls_opt_in_to_shared_drives():
    d = _drive(FakeResp(200, '{"files": []}'))
    d.list_children("root")
    params = d._session.calls[0]["params"]
    assert params["supportsAllDrives"] == "true"
    assert params["includeItemsFromAllDrives"] == "true"

    d = _drive(FakeResp(200, '{"user": {}}'))
    d.about()                      # not a /files endpoint
    assert "supportsAllDrives" not in d._session.calls[0]["params"]


def test_list_tree_lists_nested_folders():
    listings = {
        "root": [{"id": "a", "name": "A", "mimeType": FOLDER_MIME},
                 {"id": "f", "name": "f.txt", "mimeType": "text/plain"}],
        "a": [{"id": "b", "name": "B", "mimeType": FOLDER_MIME}],
        "b": [{"id": "c", "name": "c.txt", "mimeType": "text/plain"}],
    }
    d = Drive(lambda: "tok")
    d.list_children = lambda folder_id: listings.get(folder_id, [])
    items = d.list_tree("root")
    assert set(items) == {"", "A", "A/B", "A/B/c.txt", "f.txt"}
    assert items["A/B"]["id"] == "b"
    assert items["A/B/c.txt"]["id"] == "c"


def test_200_no_hint():
    d = _drive(FakeResp(200, '{"user": {"emailAddress": "me@x.com"}}'))
    assert d.about()["user"]["emailAddress"] == "me@x.com"


class _FakeListing:
    """Minimal listing surface for the picker helper tests."""

    def __init__(self, children=None, shared=None):
        self.children = children or {}
        self.shared = shared or []

    def list_children(self, folder_id):
        return self.children.get(folder_id, [])

    def list_shared_folders(self):
        return self.shared


def test_resolve_folder_entry_variants():
    assert resolve_folder_entry({"id": "f1", "name": "Photos",
                                 "mimeType": FOLDER_MIME}) == \
        {"id": "f1", "name": "Photos"}
    # shortcut to a folder binds to the real target
    assert resolve_folder_entry(
        {"id": "s1", "name": "Team", "mimeType": SHORTCUT_MIME,
         "shortcutDetails": {"targetId": "t9",
                             "targetMimeType": FOLDER_MIME}}) == \
        {"id": "t9", "name": "Team"}
    # shortcut to a file and plain docs are not pickable
    assert resolve_folder_entry(
        {"id": "s2", "name": "Doc", "mimeType": SHORTCUT_MIME,
         "shortcutDetails": {"targetId": "x",
                             "targetMimeType": "application/pdf"}}) is None
    assert resolve_folder_entry(
        {"id": "d1", "name": "Notes",
         "mimeType": "application/vnd.google-apps.document"}) is None


def test_folders_for_parent_root_and_shared():
    drive = _FakeListing(
        children={
            "root": [
                {"id": "f1", "name": "Photos", "mimeType": FOLDER_MIME},
                {"id": "s1", "name": "Team", "mimeType": SHORTCUT_MIME,
                 "shortcutDetails": {"targetId": "t9",
                                     "targetMimeType": FOLDER_MIME}},
                {"id": "d1", "name": "Notes",
                 "mimeType": "application/vnd.google-apps.document"},
            ],
            "f1": [{"id": "f2", "name": "2026", "mimeType": FOLDER_MIME}],
        },
        shared=[{"id": "sh1", "name": "SharedFolder"}])
    assert [f["id"] for f in folders_for_parent(drive, "root")] == ["f1", "t9"]
    assert [f["id"] for f in folders_for_parent(drive, "f1")] == ["f2"]
    assert folders_for_parent(drive, "shared") == \
        [{"id": "sh1", "name": "SharedFolder"}]
    assert folders_for_parent(drive, "empty") == []
