"""Local web dashboard: JSON API + static single-page UI.

Bound to 127.0.0.1 by default — this server can read your Drive tokens,
so it must never be exposed to the network.
"""

import ipaddress
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import Flask, jsonify, make_response, request, send_from_directory
from werkzeug.exceptions import BadRequest, HTTPException

from . import __version__
from . import conflicts
from . import engine
from . import recovery
from .auth import (AuthCancelled, AuthError, ReauthRequired,
                   access_token_persisting, interactive_auth, revoke_token,
                   token_from_json, validate_client, whoami)
from .config import db_path, load_config, save_config
from .db import Store, parse_include_patterns, to_local
from .drive import (Drive, DriveError, FOLDER_MIME, breadcrumbs,
                    file_category, owner_name, resolve_folder_entry)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
LOOPBACK = ("127.0.0.1", "localhost", "::1")

# The UI is same-origin only: one script, one stylesheet, local images, and
# fetch() to this server. Nothing else may load, and nothing may frame it
# (clickjacking a "Remove account" / "Allow deletions" button).
CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self'",
    "connect-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
    "object-src 'none'",
    "frame-ancestors 'none'",
))


def _to_int(value, name):
    """int(value) or a 400 that names the field (never a bare 500)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be an integer") from None


def _int_field(data, name):
    """Integer field from a JSON body; missing/empty means 0 (not found)."""
    return _to_int(data.get(name) or 0, name)


def _str_field(data, name, default="", required=False):
    """String field from a JSON body, or a 400 naming the field.

    ``str(123)`` would quietly turn a wrong type into a path or an OAuth
    id; reject it instead of letting a later call fail with a 500.
    """
    value = data.get(name, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise BadRequest(f"{name} must be a string")
    if required and not value.strip():
        raise BadRequest(f"missing field: {name}")
    return value


def is_loopback_host(host):
    """Return True only for an explicit loopback address or localhost.

    The dashboard has no user authentication; binding it to a LAN/public
    interface would expose the user's Google Drive operations to the network.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _public_settings(cfg):
    """Config dict safe to hand to the browser: never expose the secret.

    The dashboard only needs to know whether one is stored (the auth form
    posts a new pair when the user re-authorizes).
    """
    out = dict(cfg)
    secret = out.get("client_secret") or ""
    out["client_secret"] = "***" if secret else ""
    out["client_secret_set"] = bool(secret)
    return out


def _browser_rows(items):
    """Listing rows decorated for the browser UIs: a stable `category`
    token (the type column and filter both use it) and the owner's display
    name ('' when Drive does not report one)."""
    out = []
    for item in items:
        row = dict(item)
        row["category"] = file_category(item)
        row["owner"] = owner_name(item)
        out.append(row)
    return out


def create_app(store: Store, worker=None, api_token=None):
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False
    # A random per-server token replaces the old static X-DriveSync: 1
    # header. The dashboard gets it from a <meta> tag injected into index.html;
    # another local process can still read that page, but a blind script or a
    # stale bookmark can no longer drive the API, and every restart rotates it.
    token = api_token or secrets.token_urlsafe(32)
    app.config["API_TOKEN"] = token

    try:
        store.configure_keyring(load_config().get("keyring_tokens", False))
    except Exception:
        pass

    auth_jobs = {}
    auth_jobs_lock = threading.Lock()
    account_cache = {}
    account_cache_lock = threading.Lock()
    pair_undo = {}
    pair_undo_lock = threading.Lock()

    def _prune_auth_jobs():
        cutoff = time.time() - 900
        with auth_jobs_lock:
            for key in [k for k, j in auth_jobs.items()
                        if j.get("created", 0) < cutoff]:
                auth_jobs.pop(key, None)

    def _prune_pair_undo():
        now = time.time()
        with pair_undo_lock:
            for key in [k for k, (expiry, _snap) in pair_undo.items()
                        if expiry < now]:
                pair_undo.pop(key, None)

    # ---- helpers --------------------------------------------------
    def drive_for(account_id, account=None):
        # Callers that already hold the row (the parallel accounts list)
        # pass it in: sharing one SQLite connection across worker threads
        # makes a second lookup racy for no benefit.
        account = account or store.account(account_id)
        if not account:
            raise DriveError("account not found")
        token = token_from_json(account["token_json"])
        return Drive(lambda: access_token_persisting(store, account["id"],
                                                     token))

    def api_error(exc, code=400):
        return jsonify({"ok": False, "error": str(exc)}), code

    def json_body():
        """The request's JSON object ({} when absent); anything else is a
        400 instead of an AttributeError further down."""
        data = request.get_json(force=True, silent=True)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise BadRequest("expected a JSON object")
        return data

    @app.errorhandler(HTTPException)
    def http_error(exc):
        # the API always answers in JSON; the static UI keeps werkzeug's page
        if request.path.startswith("/api/"):
            return api_error(exc.description or exc.name, exc.code or 500)
        return exc

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        app.logger.exception("unhandled error on %s %s",
                             request.method, request.path)
        if request.path.startswith("/api/"):
            return api_error("internal error (details are in the log)", 500)
        return "Internal Server Error", 500

    @app.after_request
    def security_headers(resp):
        resp.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Permissions-Policy"] = \
            "geolocation=(), camera=(), microphone=(), payment=()"
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.path.startswith("/api/"):
            # responses carry account data and quota: never cache them
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.before_request
    def guard_api():
        """Reject cross-site requests and DNS-rebinding Host headers.

        The API can read Drive tokens and mutate files, so a random web page
        open in the same browser must not be able to POST to localhost. A
        custom header cannot be set cross-origin without a CORS preflight
        (which this server never grants), and the Host/Origin checks stop a
        rebinding DNS name from reaching the app.
        """
        if not request.path.startswith("/api/"):
            return None
        supplied = request.headers.get("X-DriveSync") or ""
        bearer = (request.headers.get("Authorization") or "").removeprefix(
            "Bearer ").strip()
        if not (secrets.compare_digest(supplied, token)
                or (bearer and secrets.compare_digest(bearer, token))):
            return api_error("missing or invalid X-DriveSync token", 403)
        host = urlsplit("//" + (request.host or "")).hostname
        if host not in LOOPBACK:
            return api_error("unexpected Host header", 403)
        origin = request.headers.get("Origin")
        if origin:
            if origin == "null" or urlsplit(origin).hostname not in LOOPBACK:
                return api_error("cross-origin request refused", 403)
        return None

    # ---- static UI -------------------------------------------------
    @app.get("/")
    def index():
        with open(os.path.join(STATIC_DIR, "index.html"),
                  encoding="utf-8") as fh:
            html = fh.read()
        html = html.replace("__DRIVESYNC_API_TOKEN__", token)
        resp = make_response(html)
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        # the page embeds the per-server API token; never let a shared cache
        # or the back/forward cache keep a stale copy
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/<path:filename>")
    def static_files(filename):
        return send_from_directory(STATIC_DIR, filename)

    # ---- status ----------------------------------------------------
    @app.get("/api/status")
    def status():
        cfg = load_config()
        return jsonify({
            "ok": True,
            "version": __version__,
            "worker": dict(worker.status) if worker else None,
            "interval": cfg["sync_interval"],
            "conflict_policy": cfg["conflict_policy"],
            "drive_scope": cfg.get("drive_scope", "full"),
            "readonly": cfg.get("drive_scope", "full") == "readonly",
            "db": str(db_path()),
        })

    # ---- accounts --------------------------------------------------
    @app.get("/api/accounts")
    def accounts_list():
        """Accounts with quota, fetched in parallel and cached briefly.

        Each Drive call costs a network round trip; the old serial loop made
        a 5-account page wait for five. Cache the decorated rows for a minute
        so tab switches and the 4-second dashboard refresh do not hammer
        Drive (``?refresh=1`` bypasses it).
        """
        force = request.args.get("refresh") in ("1", "true")
        now = time.time()

        def fetch(account):
            with account_cache_lock:
                cached = account_cache.get(account["id"])
            if not force and cached and cached[0] > now:
                return cached[1]
            item = {"id": account["id"], "email": account["email"],
                    "created_at": account["created_at"]}
            ttl = 60
            try:
                about = drive_for(account["id"], account=account).about()
                item["quota"] = about.get("storageQuota", {})
                item["name"] = (about.get("user") or {}).get("displayName")
            except Exception as exc:
                item["error"] = str(exc)
                # only a dead sign-in needs the consent flow again; a
                # network error or Drive outage clears up on its own
                item["reauth_required"] = isinstance(exc, ReauthRequired)
                ttl = 15
            with account_cache_lock:
                account_cache[account["id"]] = (now + ttl, item)
            return item

        accounts = store.accounts()
        if len(accounts) > 1:
            with ThreadPoolExecutor(
                    max_workers=min(4, len(accounts))) as pool:
                out = list(pool.map(fetch, accounts))
        else:
            out = [fetch(a) for a in accounts]
        return jsonify({"ok": True, "accounts": out})

    @app.post("/api/accounts")
    def accounts_add():
        """Store OAuth client credentials and start the consent flow.

        The browser redirect can take minutes, so the flow runs in a
        background job and returns immediately. The UI polls
        /api/accounts/auth/<job> and can cancel it.
        """
        _prune_auth_jobs()
        data = json_body()
        current = load_config()
        # the form may omit credentials (re-auth with the saved client):
        # only overwrite when a non-empty value was actually posted
        updates = {}
        for key in ("client_id", "client_secret"):
            value = _str_field(data, key).strip()
            if value:
                updates[key] = value
        # the auth form's scope selector applies to this sign-in and later
        # ones; save_config() validates it.
        scope = _str_field(data, "drive_scope").strip().lower()
        if scope:
            updates["drive_scope"] = scope
        try:
            cfg = save_config({**current, **updates})
        except ValueError as exc:
            return api_error(exc)
        if not cfg.get("client_id") or not cfg.get("client_secret"):
            return api_error("Client ID and client secret are required.")
        job_id = secrets.token_urlsafe(12)
        cancel = threading.Event()
        job = {"status": "pending", "cancel": cancel,
               "created": time.time()}
        with auth_jobs_lock:
            # One consent flow at a time: two browser windows would race the
            # same loopback callback port and the second would overwrite the
            # first account's tokens.
            if any(j.get("status") == "pending"
                   for j in auth_jobs.values()):
                return api_error("a sign-in is already in progress", 409)
            auth_jobs[job_id] = job

        def run_auth():
            try:
                tokens = interactive_auth(cfg, cancel_event=cancel)
                email = whoami(tokens["access_token"]).get(
                    "emailAddress", "unknown")
                row = store.add_account(email, tokens)
                store.log(None, "account-added", "", email)
                with account_cache_lock:
                    account_cache.clear()
                job.update(status="done", account={"id": row["id"],
                                                   "email": email})
            except AuthCancelled as exc:
                job.update(status="cancelled", error=str(exc))
            except AuthError as exc:
                job.update(status="error", error=str(exc))
            except Exception as exc:      # keep the job readable, not a 500
                job.update(status="error", error=str(exc))

        threading.Thread(target=run_auth, daemon=True,
                         name="drivesync-auth").start()
        return jsonify({"ok": True, "job": job_id,
                        "status": "pending"}), 202

    @app.get("/api/accounts/auth/<job_id>")
    def accounts_auth_status(job_id):
        _prune_auth_jobs()
        with auth_jobs_lock:
            job = auth_jobs.get(job_id)
            if job is None:
                return api_error("sign-in job not found", 404)
            out = {"status": job.get("status"), "error": job.get("error"),
                   "account": job.get("account")}
        return jsonify({"ok": True, **out})

    @app.delete("/api/accounts/auth/<job_id>")
    def accounts_auth_cancel(job_id):
        with auth_jobs_lock:
            job = auth_jobs.get(job_id)
            if job is None:
                return api_error("sign-in job not found", 404)
            if job.get("status") == "pending":
                job["cancel"].set()
                job["status"] = "cancelled"
            status = job.get("status")
        return jsonify({"ok": True, "status": status})

    @app.post("/api/oauth/test")
    def oauth_test():
        """Validate an OAuth client without starting the consent flow."""
        data = json_body()
        cfg = load_config()
        client_id = (_str_field(data, "client_id")
                     or cfg.get("client_id") or "").strip()
        client_secret = (_str_field(data, "client_secret")
                         or cfg.get("client_secret") or "").strip()
        ok, message = validate_client(client_id, client_secret)
        if not ok:
            return api_error(message)
        return jsonify({"ok": True, "message": message})

    @app.delete("/api/accounts/<int:account_id>")
    def accounts_delete(account_id):
        data = request.get_json(silent=True) or {}
        revoke = bool(data.get("revoke"))
        account = store.account(account_id)
        if account is None:
            return api_error("account not found", 404)
        token = token_from_json(account["token_json"])
        store.delete_account(account_id)
        with account_cache_lock:
            account_cache.pop(account_id, None)
        revoked = None
        if revoke:
            ok, detail = revoke_token(token)
            revoked = {"ok": ok, "detail": detail}
            store.log(None, "account-revoked" if ok else "revoke-failed",
                      "", f"{account['email']}: {detail}",
                      level="info" if ok else "error")
        return jsonify({"ok": True, "revoked": revoked})

    # ---- drive browsing (folder picker + Drive browser) --------------
    @app.get("/api/drive/<int:account_id>/folders")
    def drive_folders(account_id):
        """List folders (and root info) for the picker UI.

        parent=shared lists 'Shared with me' (folders other accounts shared);
        shortcuts to folders resolve to their real target ids so pairs bind
        to the folder itself, not the shortcut. `breadcrumbs` is the
        root-first chain of ancestor folders for clickable navigation; the
        virtual roots (root, shared) never hit the API for it.
        """
        parent = request.args.get("parent", "root")
        try:
            drive = drive_for(account_id)
            crumbs = breadcrumbs(drive, parent)
            if parent == "shared":
                folders = drive.list_shared_folders()
            elif parent == "drives":
                folders = [{"id": d["id"], "name": d["name"],
                            "mimeType": FOLDER_MIME}
                           for d in drive.list_drives()]
            else:
                children = drive.list_children(parent)
                folders = [f for f in (resolve_folder_entry(c) for c in children)
                           if f]
            current = dict(crumbs[-1]) if crumbs else None
        except (DriveError, AuthError) as exc:
            return api_error(exc, 502)
        if parent == "root":
            current = None          # the top of My Drive has no parent row
            folders = [{"id": "shared", "name": "Shared with me"},
                       {"id": "drives", "name": "Shared drives"}] + folders
        return jsonify({"ok": True, "current": current, "breadcrumbs": crumbs,
                        "folders": folders})
    @app.get("/api/drive/<int:account_id>/children")
    def drive_children(account_id):
        """Full child listing (files AND folders) for the Drive browser.

        parent=root lists My Drive; parent=shared lists 'Shared with me'
        folders. Rows carry the metadata the browser needs (id, name,
        mimeType, size, modifiedTime) plus a stable `category` token for
        the type column/filter and the owner's display name; Google-native
        docs have no size. `breadcrumbs` is the root-first ancestor chain.
        """
        parent = request.args.get("parent", "root")
        try:
            drive = drive_for(account_id)
            crumbs = breadcrumbs(drive, parent)
            if parent == "shared":
                items = drive.list_shared_folders()
            elif parent == "drives":
                items = [{"id": d["id"], "name": d["name"],
                          "mimeType": FOLDER_MIME, "driveId": d["id"]}
                         for d in drive.list_drives()]
            else:
                items = drive.list_children(parent)
                if parent == "root":
                    # virtual browser levels, not real Drive folders
                    items = [{"id": "shared", "name": "Shared with me",
                              "mimeType": FOLDER_MIME},
                             {"id": "drives", "name": "Shared drives",
                              "mimeType": FOLDER_MIME}] + items
        except (DriveError, AuthError) as exc:
            return api_error(exc, 502)
        # root keeps current=None (historical contract for pickers); the
        # browser UIs take the folder row from breadcrumbs[-1] instead
        current = dict(crumbs[-1]) if crumbs and parent != "root" else None
        return jsonify({"ok": True, "current": current,
                        "breadcrumbs": crumbs,
                        "items": _browser_rows(items)})

    @app.get("/api/drive/<int:account_id>/search")
    def drive_search(account_id):
        """Drive-wide name search (files and folders, folders first).

        ?q= the text to find, ?folders_only=1 restricts to folders (the
        pair picker uses that mode). Capped at 50 rows — enough to locate
        anything by name without rendering a tree. Rows carry the same
        `category`/`owner` decorations as the folder listings.
        """
        query = (request.args.get("q") or "").strip()
        folders_only = request.args.get("folders_only") in ("1", "true")
        if not query:
            return jsonify({"ok": True, "items": []})
        try:
            drive = drive_for(account_id)
            items = drive.search(query, folders_only=folders_only)
        except (DriveError, AuthError) as exc:
            return api_error(exc, 502)
        return jsonify({"ok": True, "items": _browser_rows(items)})

    # ---- pairs ------------------------------------------------------
    def pair_json(p):
        last = None
        if worker is not None:
            last = (worker.status.get("pairs") or {}).get(p["id"])
        progress = None
        if worker is not None:
            rows = [t for t in (worker.status.get("transfers") or [])
                    if t.get("pair_id") == p["id"]
                    and t.get("status") in ("queued", "uploading",
                                            "downloading", "retrying")]
            if rows:
                total = sum(int(t.get("size") or 0) for t in rows)
                progress = {"active": len(rows), "bytes": total}
        return {"id": p["id"], "account_id": p["account_id"],
                "local_path": p["local_path"], "remote_id": p["remote_id"],
                "remote_path": p["remote_path"], "direction": p["direction"],
                "conflict_policy": p["conflict_policy"],
                "enabled": bool(p["enabled"]),
                "allow_deletes": bool(p["allow_deletes"]),
                "symlink_policy": p["symlink_policy"] or "skip",
                "case_insensitive": bool(p["case_insensitive"]),
                "paused_until": p["paused_until"] or None,
                "bandwidth_limit_kb": p["bandwidth_limit_kb"],
                "peak_from": p["peak_from"] or None,
                "peak_to": p["peak_to"] or None,
                "bandwidth_limit_kb_peak": p["bandwidth_limit_kb_peak"],
                "include_patterns": parse_include_patterns(
                    p["include_patterns"]),
                "last_sync": last, "progress": progress}

    @app.get("/api/pairs")
    def pairs_list():
        return jsonify({"ok": True,
                        "pairs": [pair_json(p) for p in store.pairs()]})

    @app.post("/api/pairs")
    def pairs_add():
        data = json_body()
        try:
            local = os.path.realpath(
                os.path.expanduser(_str_field(data, "local_path",
                                              required=True)))
            if not os.path.isdir(local):
                return api_error(f"local path is not a directory: {local}")
            account_id = _to_int(data["account_id"], "account_id")
            remote_id = _str_field(data, "remote_id", required=True)
            direction = _str_field(data, "direction") or "bidirectional"
            if load_config().get("drive_scope") == "readonly" \
                    and direction != "download":
                return api_error(
                    "This install is authorized for read-only Drive access. "
                    "Use the 'Download only' direction, or reconnect the "
                    "account with full access in Settings.")
            # bind only to a real folder this account can see; a file id
            # would make the first pass fail in a confusing way
            try:
                meta = drive_for(account_id).get(remote_id,
                                                 fields="id,name,mimeType")
            except (DriveError, AuthError) as exc:
                return api_error(f"Drive folder is not accessible: {exc}", 502)
            if meta.get("mimeType") != FOLDER_MIME:
                return api_error("remote_id is not a Drive folder")
            pair = store.add_pair(
                account_id, local, remote_id,
                remote_path=data.get("remote_path", ""),
                direction=direction,
                conflict_policy=data.get("conflict_policy")
                or load_config()["conflict_policy"],
                enabled=data.get("enabled", 1),
                allow_deletes=data.get("allow_deletes", 1),
                include_patterns=data.get("include_patterns"),
                symlink_policy=data.get("symlink_policy", "skip"),
                case_insensitive=data.get("case_insensitive", 0),
                bandwidth_limit_kb=data.get("bandwidth_limit_kb"),
                peak_from=data.get("peak_from"),
                peak_to=data.get("peak_to"),
                bandwidth_limit_kb_peak=data.get("bandwidth_limit_kb_peak"))
        except KeyError as exc:
            return api_error(f"missing field: {exc}")
        except ValueError as exc:
            return api_error(str(exc))
        store.log(pair["id"], "pair-added", "",
                  f"{pair['local_path']} <-> {pair['remote_path'] or pair['remote_id']}")
        return jsonify({"ok": True, "pair": pair_json(pair)})

    @app.patch("/api/pairs/<int:pair_id>")
    def pairs_update(pair_id):
        data = json_body()
        if load_config().get("drive_scope") == "readonly" \
                and str(data.get("direction") or "download") != "download":
            return api_error(
                "This install is authorized for read-only Drive access; "
                "pairs must stay in the 'Download only' direction.")
        # a JSON number/object for a text field would otherwise reach SQLite
        # or path handling and surface as a 500
        for key in ("direction", "conflict_policy", "local_path",
                    "remote_path", "remote_id", "symlink_policy"):
            if key in data:
                _str_field(data, key)
        try:
            pair = store.update_pair(pair_id, **data)
        except ValueError as exc:
            return api_error(str(exc))
        return jsonify({"ok": True, "pair": pair_json(pair)})

    @app.delete("/api/pairs/<int:pair_id>")
    def pairs_delete(pair_id):
        _prune_pair_undo()
        snapshot = store.snapshot_pair(pair_id)
        if snapshot is None:
            return api_error("pair not found", 404)
        store.delete_pair(pair_id)
        undo = None
        if not snapshot.get("truncated"):
            undo = secrets.token_urlsafe(12)
            with pair_undo_lock:
                pair_undo[undo] = (time.time() + 60, snapshot)
        return jsonify({"ok": True, "undo": undo,
                        "pair": pair_json(snapshot["pair"])})

    @app.post("/api/pairs/undo")
    def pairs_undo():
        """Undo a pair removal made in the last minute."""
        _prune_pair_undo()
        data = json_body()
        undo = str(data.get("undo") or "")
        with pair_undo_lock:
            entry = pair_undo.pop(undo, None)
        if entry is None:
            return api_error("undo window expired", 410)
        pair = store.restore_pair(entry[1])
        if pair is None:
            return api_error("could not restore the pair (id is in use)", 409)
        store.log(pair["id"], "pair-restored", "",
                  f"{pair['local_path']} <-> "
                  f"{pair['remote_path'] or pair['remote_id']}")
        if worker is not None:
            worker.sync_now(pair["id"])
        return jsonify({"ok": True, "pair": pair_json(pair)})

    @app.post("/api/pairs/<int:pair_id>/open-folder")
    def pairs_open_folder(pair_id):
        """Open the pair's local folder with the desktop's file manager."""
        pair = store.pair(pair_id)
        if not pair:
            return api_error("pair not found", 404)
        path = pair["local_path"]
        if not os.path.isdir(path):
            return api_error(f"local folder does not exist: {path}", 404)
        try:
            subprocess.Popen(["xdg-open", path],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except (OSError, ValueError) as exc:
            return api_error(f"could not open the folder: {exc}", 503)
        return jsonify({"ok": True, "path": path})

    @app.post("/api/pairs/<int:pair_id>/sync")
    def pairs_sync(pair_id):
        if worker is None:
            return api_error("worker not available", 503)
        if not store.pair(pair_id):
            return api_error("pair not found", 404)
        threading.Thread(target=worker.run_pass, args=(pair_id,),
                         daemon=True).start()
        return jsonify({"ok": True, "started": True})

    @app.get("/api/pairs/<int:pair_id>/preview")
    def pairs_preview(pair_id):
        """Dry-run the pair's next pass: what WOULD change, nothing applied.

        Blocking request by design (the scan is the work); Flask runs
        threaded so the dashboard stays responsive. The ops list is capped
        — counts carry the full picture for huge first-sync previews.
        """
        pair = store.pair(pair_id)
        if not pair:
            return api_error("pair not found", 404)
        try:
            result = engine.preview_pair(store, load_config(), pair,
                                         drive_for(pair["account_id"]))
        except (DriveError, AuthError) as exc:
            return api_error(f"Drive error: {exc}", 502)
        except Exception as exc:
            return api_error(str(exc))
        result["ops"] = result["ops"][:500]
        for key in ("reasons", "sizes", "destructive"):
            if key in result:
                result[key] = result[key][:500]
        return jsonify({"ok": True, **result})

    # ---- conflict copies (review + resolve) --------------------------
    @app.get("/api/pairs/<int:pair_id>/conflicts")
    def pairs_conflicts(pair_id):
        pair = store.pair(pair_id)
        if not pair:
            return api_error("pair not found", 404)
        try:
            items = conflicts.list_conflicts(store, pair)
        except Exception as exc:
            return api_error(str(exc))
        return jsonify({"ok": True, "conflicts": items})

    @app.post("/api/pairs/<int:pair_id>/conflicts/resolve")
    def conflicts_resolve(pair_id):
        """Keep the conflict copy, keep the original, or leave both.

        resolve_conflict raises ValueError for unknown paths/choices and
        Drive errors propagate as 502 so the UI can show them; the action
        itself is logged to the activity feed by the conflicts module.
        """
        pair = store.pair(pair_id)
        if not pair:
            return api_error("pair not found", 404)
        data = json_body()
        try:
            result = conflicts.resolve_conflict(
                store, pair, drive_for(pair["account_id"]),
                data.get("path") or "", data.get("choice") or "")
        except ValueError as exc:
            return api_error(str(exc))
        except (DriveError, AuthError) as exc:
            return api_error(f"Drive error: {exc}", 502)
        if worker is not None:
            worker.sync_now(pair_id)   # let the next pass settle the base
        return jsonify({"ok": True, "result": result})

    # ---- recovery (Phase 2.6) ----------------------------------------
    def _pair_or_error(pair_id):
        pair = store.pair(pair_id)
        if not pair:
            return None, api_error("pair not found", 404)
        return pair, None

    @app.get("/api/recovery/summary")
    def recovery_summary():
        """One snapshot for the Recovery page: pending holds, trash and
        conflict counts per pair, recent failures and the decision log."""
        pending = recovery.all_pending_deletions(store)
        trash = recovery.list_local_trash(store, limit=1000)
        conflict_counts = []
        for pair in store.pairs():
            try:
                n = len(conflicts.list_conflicts(store, pair))
            except Exception:
                n = None          # a broken pair must not break the page
            if n:
                conflict_counts.append({"pair_id": pair["id"],
                                        "name": pair["remote_path"]
                                        or pair["local_path"],
                                        "count": n})
        failures = store.recent_events(limit=20, level="error")
        history = store.recent_recovery(limit=100)
        return jsonify({"ok": True,
                        "pending": pending,
                        "trash": [{"pair_id": t["pair_id"],
                                   "pair_name": t["pair_name"],
                                   "stamp": t["stamp"], "rel": t["rel"],
                                   "size": t["size"], "mtime": t["mtime"],
                                   "when": to_local(datetime.fromtimestamp(
                                       t["mtime"], timezone.utc)
                                       .strftime("%Y-%m-%dT%H:%M:%SZ"))}
                                  for t in trash[:200]],
                        "trash_total": len(trash),
                        "conflicts": conflict_counts,
                        "failures": [{"id": e["id"],
                                      "ts": to_local(e["ts"]),
                                      "pair_id": e["pair_id"],
                                      "action": e["action"],
                                      "path": e["rel_path"],
                                      "message": e["message"]}
                                     for e in failures],
                        "history": [{"id": h["id"], "ts": to_local(h["ts"]),
                                     "pair_id": h["pair_id"],
                                     "path": h["rel_path"],
                                     "action": h["action"],
                                     "reason": h["reason"],
                                     "detail": h["detail"]}
                                    for h in history]})

    @app.post("/api/recovery/deletions/allow")
    def recovery_allow_deletions():
        """Approve one pass of the held deletions (mass-delete protection)."""
        data = json_body()
        pair, err = _pair_or_error(_int_field(data, "pair_id"))
        if err:
            return err
        hold = recovery.pending_deletions(store, pair["id"])
        if not hold:
            return api_error("no deletions are held for this pair")
        recovery.allow_pending_deletions(store, pair["id"])
        if worker is not None:
            worker.sync_now(pair["id"])    # apply the approved pass promptly
        return jsonify({"ok": True, "approved": hold["count"]})

    @app.post("/api/recovery/deletions/keep")
    def recovery_keep_files():
        """Resolve the hold by keeping every file: switches deletes off."""
        data = json_body()
        pair, err = _pair_or_error(_int_field(data, "pair_id"))
        if err:
            return err
        if not recovery.pending_deletions(store, pair["id"]):
            return api_error("no deletions are held for this pair")
        recovery.keep_pending_files(store, pair["id"])
        return jsonify({"ok": True, "kept": True})

    @app.get("/api/recovery/trash")
    def recovery_trash():
        """Files the engine moved to the recoverable local trash."""
        try:
            limit = int(request.args.get("limit", 200))
        except (TypeError, ValueError):
            limit = 200
        items = recovery.list_local_trash(store, limit=max(1, min(limit, 500)))
        return jsonify({"ok": True, "items": items, "total": len(items)})

    @app.post("/api/recovery/trash/restore")
    def recovery_trash_restore():
        """Move a trashed file back to its original local path."""
        data = json_body()
        pair_id = _int_field(data, "pair_id")
        try:
            result = recovery.restore_from_trash(
                store, pair_id, data.get("stamp") or "",
                data.get("rel") or "")
        except ValueError as exc:
            return api_error(str(exc))
        if worker is not None:
            worker.sync_now(pair_id)       # the restored file joins the pair
        return jsonify({"ok": True, "result": result})

    @app.get("/api/recovery/drive-trash")
    def recovery_drive_trash():
        """The account's Drive trash (Drive-wide, labelled as such)."""
        pair_id = request.args.get("pair_id", type=int)
        pair, err = _pair_or_error(pair_id or 0)
        if err:
            return err
        try:
            items = recovery.list_drive_trash(
                pair, drive_for(pair["account_id"]))
        except (DriveError, AuthError) as exc:
            return api_error(f"Drive error: {exc}", 502)
        return jsonify({"ok": True, "items": items})

    @app.post("/api/recovery/drive-trash/restore")
    def recovery_drive_trash_restore():
        """Untrash a Drive file; the next pass re-syncs it if it belongs
        to one of the pairs."""
        data = json_body()
        pair, err = _pair_or_error(_int_field(data, "pair_id"))
        if err:
            return err
        file_id = data.get("file_id") or ""
        if not file_id:
            return api_error("file_id is required")
        try:
            result = recovery.restore_drive_trash(
                store, pair, drive_for(pair["account_id"]), file_id)
        except (DriveError, AuthError) as exc:
            return api_error(f"Drive error: {exc}", 502)
        if worker is not None:
            worker.sync_now(pair["id"])
        return jsonify({"ok": True, "result": result})

    @app.get("/api/recovery/versions")
    def recovery_versions():
        """Kept previous versions of one path (before overwrite pulls)."""
        pair_id = request.args.get("pair_id", type=int)
        rel = (request.args.get("rel") or "").strip()
        if not rel:
            return api_error("rel is required")
        if pair_id is None:
            return api_error("pair_id is required")
        try:
            items = recovery.list_versions(pair_id, rel)
        except ValueError as exc:
            return api_error(str(exc))
        return jsonify({"ok": True, "items": items})

    @app.post("/api/recovery/versions/restore")
    def recovery_version_restore():
        """Put a kept previous version back at its path (current bytes are
        themselves pushed into the history first)."""
        data = json_body()
        pair_id = _int_field(data, "pair_id")
        try:
            result = recovery.restore_version(
                store, pair_id, data.get("rel") or "",
                data.get("stamp") or "")
        except ValueError as exc:
            return api_error(str(exc))
        if worker is not None:
            worker.sync_now(pair_id)
        return jsonify({"ok": True, "result": result})

    @app.get("/api/recovery/history")
    def recovery_history():
        try:
            limit = int(request.args.get("limit", 100))
        except (TypeError, ValueError):
            limit = 100
        rows = store.recent_recovery(limit=max(1, min(limit, 500)))
        return jsonify({"ok": True, "history": [
            {"id": h["id"], "ts": to_local(h["ts"]),
             "pair_id": h["pair_id"], "path": h["rel_path"],
             "action": h["action"], "reason": h["reason"],
             "detail": h["detail"]} for h in rows]})

    @app.post("/api/sync")
    def sync_all():
        if worker is None:
            return api_error("worker not available", 503)
        threading.Thread(target=worker.run_pass, daemon=True).start()
        return jsonify({"ok": True, "started": True})

    # ---- transfer queue ---------------------------------------------
    @app.get("/api/transfers")
    def transfers():
        """Current and recently completed file transfers from the worker."""
        rows = list((worker.status.get("transfers") or []) if worker else [])
        rows.reverse()  # newest activity first
        return jsonify({"ok": True, "transfers": rows})

    # ---- events -----------------------------------------------------
    @app.get("/api/events")
    def events():
        try:
            limit = int(request.args.get("limit", 200))
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(limit, 1000))
        level = request.args.get("level")
        pair_id = request.args.get("pair_id", type=int)
        rows = store.recent_events(limit=limit, level=level, pair_id=pair_id)
        return jsonify({"ok": True, "events": [
            {"id": e["id"], "ts": to_local(e["ts"]), "pair_id": e["pair_id"],
             "level": e["level"], "action": e["action"],
             "path": e["rel_path"], "message": e["message"]} for e in rows]})

    # ---- settings ----------------------------------------------------
    @app.get("/api/settings")
    def settings_get():
        return jsonify({"ok": True, "settings": _public_settings(load_config())})

    @app.post("/api/settings")
    def settings_set():
        data = json_body()
        # round-tripping a redacted settings object must not turn the real
        # secret into the literal mask
        if data.get("client_secret") == "***":
            data.pop("client_secret")
        data.pop("client_secret_set", None)
        try:
            # merge over the stored config: the form posts only its own
            # fields, and dropping unspecified keys would wipe credentials
            # and settings the UI does not send
            cfg = save_config({**load_config(), **data})
        except ValueError as exc:
            return api_error(str(exc))
        try:
            store.configure_keyring(cfg.get("keyring_tokens", False))
        except Exception:
            pass
        if worker:
            worker.apply_config(cfg)
        return jsonify({"ok": True, "settings": _public_settings(cfg)})

    @app.post("/api/settings/keyring/forget")
    def keyring_forget():
        """Delete the OS-keyring copies, keeping tokens in state.db."""
        try:
            moved = store.forget_keyring_tokens()
        except Exception as exc:
            return api_error(exc)
        return jsonify({"ok": True, "moved": moved})

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True})

    return app


def check_tls(store):
    """Fail loudly and once at startup if the TLS CA bundle is missing
    (broken venv), instead of failing every transfer with a cryptic
    requests error later."""
    try:
        import certifi
        path = certifi.where()
        if not path or not os.path.exists(path):
            raise FileNotFoundError(path or "certifi.where() returned nothing")
    except Exception as exc:
        store.log(None, "tls-broken", "", (
            f"TLS CA certificate bundle unavailable ({exc}); transfers will "
            "fail until the venv is repaired: recreate it and run "
            "'pip install -r requirements.txt'"), level="error")
        print(
            "DriveSync: TLS CA bundle unavailable — downloads/uploads will fail.\n"
            "  Fix: stop serve, recreate the venv, reinstall deps:\n"
            "    rm -rf ~/.local/share/drivesync/.venv\n"
            "    python3 -m venv --system-site-packages ~/.local/share/drivesync/.venv\n"
            "    ~/.local/share/drivesync/.venv/bin/pip install -r requirements.txt pystray pillow",
            file=sys.stderr)


def run_server(store, worker, host="127.0.0.1", port=None):
    if not is_loopback_host(host):
        raise ValueError(
            "DriveSync dashboard is unauthenticated and must bind to localhost "
            "(127.0.0.1, ::1, or localhost). Remote binding is disabled.")
    check_tls(store)
    app = create_app(store, worker)
    port = port or load_config()["server_port"]

    class _NoDevWarning(logging.Filter):
        """Hide Flask's dev-server warning — this app is a localhost-only
        single-user dashboard by design; keep normal request logs."""

        def filter(self, record):
            return "development server" not in record.getMessage()

    logging.getLogger("werkzeug").addFilter(_NoDevWarning())
    app.run(host=host, port=port, threaded=True, use_reloader=False)
