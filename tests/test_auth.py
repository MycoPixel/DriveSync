"""OAuth token lifecycle: expiry stamping, refresh and rotation."""

import time

import pytest

from drivesync import auth


class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return dict(self._payload)


def test_exchange_code_stamps_expiry(monkeypatch):
    payload = {"access_token": "a", "refresh_token": "r", "expires_in": 1200}
    monkeypatch.setattr(auth.requests, "post",
                        lambda *a, **k: FakeResp(200, payload))
    before = time.time()
    tokens = auth.exchange_code("cid", "csec", "code", "http://127.0.0.1:1")
    assert tokens["access_token"] == "a"
    assert before + 1200 <= tokens["expires_at"] <= time.time() + 1200


def test_exchange_code_rejects_missing_access_token(monkeypatch):
    monkeypatch.setattr(auth.requests, "post",
                        lambda *a, **k: FakeResp(200, {"expires_in": 60}))
    with pytest.raises(auth.AuthError):
        auth.exchange_code("cid", "csec", "code", "http://127.0.0.1:1")


def test_access_token_refreshes_and_updates_dict(monkeypatch):
    calls = {}

    def fake_refresh(cid, csec, rt):
        calls.update(cid=cid, csec=csec, rt=rt)
        return {"access_token": "new", "expires_in": 3600,
                "refresh_token": "rotated"}

    monkeypatch.setattr(auth, "refresh_tokens", fake_refresh)
    token = {"access_token": "old", "expires_at": 0, "refresh_token": "r",
             "client_id": "cid", "client_secret": "csec"}
    assert auth.access_token(token) == "new"
    assert token["access_token"] == "new"
    assert token["refresh_token"] == "rotated"
    assert token["expires_at"] > time.time()
    assert calls == {"cid": "cid", "csec": "csec", "rt": "r"}


def test_access_token_reuses_valid_token(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not refresh")

    monkeypatch.setattr(auth, "refresh_tokens", boom)
    token = {"access_token": "tok", "expires_at": time.time() + 3600,
             "refresh_token": "r"}
    assert auth.access_token(token) == "tok"


def test_access_token_without_client_credentials(monkeypatch):
    token = {"access_token": "old", "expires_at": 0, "refresh_token": "r"}
    with pytest.raises(auth.AuthError) as exc:
        auth.access_token(token)
    assert "client credentials" in str(exc.value)


def test_concurrent_refresh_happens_once(monkeypatch):
    """The worker and a dashboard request must not rotate the token twice."""
    import threading

    calls = []
    start = threading.Barrier(2)
    release = threading.Event()

    def slow_refresh(cid, csec, rt):
        calls.append(rt)
        release.wait(2)
        return {"access_token": "fresh", "expires_in": 3600}

    monkeypatch.setattr(auth, "refresh_tokens", slow_refresh)
    token = {"access_token": "old", "expires_at": 0, "refresh_token": "r",
             "client_id": "cid", "client_secret": "csec"}
    results = []

    def worker():
        start.wait()
        results.append(auth.access_token(token))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    release.set()
    for t in threads:
        t.join(3)
    assert sorted(results) == ["fresh", "fresh"]
    assert calls == ["r"]                  # one refresh, not two


def test_interactive_auth_falls_back_to_ephemeral_port(monkeypatch):
    """A taken auth_port must not break re-authorization."""
    ports = []

    class FakeServer:
        def __init__(self, port):
            ports.append(port)
            if port != 0:
                raise OSError("address already in use")
            self.server_address = ("127.0.0.1", 45678)
            self.result = {"error": "access_denied"}   # leave the wait loop

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(auth, "_CallbackServer", FakeServer)
    monkeypatch.setattr(auth.webbrowser, "open", lambda url: True)
    with pytest.raises(auth.AuthError):
        auth.interactive_auth({"client_id": "cid", "client_secret": "csec",
                               "auth_port": 53137})
    assert ports == [53137, 0]


def test_exchange_code_sends_pkce_verifier(monkeypatch):
    seen = {}

    def fake_post(url, data, timeout):
        seen.update(data)
        return FakeResp(200, {"access_token": "a", "expires_in": 3600})

    monkeypatch.setattr(auth.requests, "post", fake_post)
    auth.exchange_code("cid", "csec", "code", "http://127.0.0.1:1",
                       code_verifier="verifier")
    assert seen["code_verifier"] == "verifier"


def test_interactive_auth_uses_pkce_s256(monkeypatch):
    import urllib.parse

    captured = {}

    class FakeServer:
        def __init__(self, port):
            self.server_address = ("127.0.0.1", 45678)
            self.result = {}

        def serve_forever(self):
            return None

        def shutdown(self):
            return None

        def server_close(self):
            return None

    def fake_open(url):
        captured["params"] = urllib.parse.parse_qs(
            urllib.parse.urlparse(url).query)
        captured["url"] = url
        # Complete the callback after the URL has been captured.
        server = servers[0]
        server.result.update({
            "state": captured["params"]["state"][0],
            "code": "authorization-code",
        })
        return True

    servers = []
    def make_server(port):
        server = FakeServer(port)
        servers.append(server)
        return server

    monkeypatch.setattr(auth, "_CallbackServer", make_server)
    monkeypatch.setattr(auth.webbrowser, "open", fake_open)
    monkeypatch.setattr(auth, "exchange_code",
                        lambda cid, secret, code, redirect_uri,
                               code_verifier=None: {
                                   "access_token": "a",
                                   "refresh_token": "r",
                                   "code_verifier": code_verifier,
                               })
    tokens = auth.interactive_auth({
        "client_id": "cid", "client_secret": "csec", "auth_port": 53137,
    })
    params = captured["params"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"][0]
    assert tokens["code_verifier"]
