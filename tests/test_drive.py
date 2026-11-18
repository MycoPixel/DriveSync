"""Drive client error-translation tests: 403 responses get actionable hints."""

import pytest

from drivesync.drive import (Drive, DriveError, FOLDER_MIME, SHORTCUT_MIME,
                             folders_for_parent, resolve_folder_entry)

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
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        import json
        return json.loads(self.text)


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
