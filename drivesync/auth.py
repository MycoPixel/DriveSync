"""Google OAuth2 for installed apps: loopback redirect flow + token refresh.

Uses the user's own OAuth desktop client (client_id / client_secret) from
config or the GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET environment variables.
Scopes requested: full drive access (read + write).
"""

import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive"

ACCOUNT_KWARGS = ("access_type=offline", "prompt=consent",
                  "include_granted_scopes=true")


class AuthError(Exception):
    pass


class _CallbackServer(HTTPServer):
    """One-shot HTTP server that captures the ?code= query on the loopback."""

    def __init__(self, port):
        self.result = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                outer.result = {k: v[0] for k, v in q.items()}
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h2>DriveSync</h2><p>Authorization received. "
                                 b"You can close this tab and return to the app.</p>")

            def log_message(self, *args):
                pass

        super().__init__(("127.0.0.1", port), Handler)


def _client_credentials(config):
    cid = config.get("client_id") or __import__("os").environ.get("GOOGLE_CLIENT_ID", "")
    csec = config.get("client_secret") or __import__("os").environ.get("GOOGLE_CLIENT_SECRET", "")
    if not cid or not csec:
        raise AuthError(
            "No OAuth client configured. Create a Google Cloud 'Desktop app' "
            "client and set client_id / client_secret (see README).")
    return cid, csec


def interactive_auth(config):
    """Run the installed-app flow; returns the full token dict.

    Opens the browser when possible; always prints the URL so it can be
    pasted into a browser on a headless machine.
    """
    client_id, client_secret = _client_credentials(config)
    port = int(config.get("auth_port", 53137))
    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://127.0.0.1:{port}"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
    }
    params.update(dict(kv.split("=") for kv in ACCOUNT_KWARGS))
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)

    server = _CallbackServer(port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("\nOpening your browser to authorize DriveSync...")
    print("If it does not open, visit this URL manually:\n")
    print(url + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    deadline = time.time() + 300  # 5 minutes to complete consent
    while time.time() < deadline and "code" not in server.result \
            and "error" not in server.result:
        time.sleep(0.3)
    server.shutdown()
    result = server.result
    if result.get("state") != state:
        raise AuthError("OAuth state mismatch (possible forged callback).")
    if "error" in result:
        raise AuthError(f"Authorization failed: {result['error']}")
    if "code" not in result:
        raise AuthError("Timed out waiting for Google authorization.")

    tokens = exchange_code(client_id, client_secret, result["code"], redirect_uri)
    tokens.update({"client_id": client_id, "client_secret": client_secret})
    return tokens


def exchange_code(client_id, client_secret, code, redirect_uri):
    resp = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=30)
    if resp.status_code != 200:
        raise AuthError(f"Token exchange failed: {resp.text[:300]}")
    return resp.json()


def refresh_tokens(client_id, client_secret, refresh_token):
    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=30)
    if resp.status_code != 200:
        raise AuthError(f"Token refresh failed: {resp.text[:300]}")
    return resp.json()


def access_token(token_json):
    """Return a valid access token, refreshing when expired."""
    if not token_json.get("refresh_token"):
        raise AuthError("No refresh token stored; re-run authorization.")
    expires_at = token_json.get("expires_at", 0)
    if time.time() < expires_at - 60 and token_json.get("access_token"):
        return token_json["access_token"]

    fresh = refresh_tokens(token_json["client_id"], token_json["client_secret"],
                           token_json["refresh_token"])
    token_json["access_token"] = fresh["access_token"]
    token_json["expires_in"] = fresh.get("expires_in", 3600)
    token_json["expires_at"] = time.time() + token_json["expires_in"]
    if fresh.get("refresh_token"):
        token_json["refresh_token"] = fresh["refresh_token"]
    return token_json["access_token"]


def whoami(access_token_value):
    """Fetch the signed-in user's email via the Drive about endpoint."""
    resp = requests.get(
        "https://www.googleapis.com/drive/v3/about",
        params={"fields": "user"},
        headers={"Authorization": f"Bearer {access_token_value}"},
        timeout=30)
    if resp.status_code != 200:
        raise AuthError(f"Could not read account info: {resp.text[:300]}")
    return resp.json().get("user", {})


def token_from_json(raw):
    return json.loads(raw) if isinstance(raw, str) else raw
