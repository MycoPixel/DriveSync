"""Local web dashboard: JSON API + static single-page UI.

Bound to 127.0.0.1 by default — this server can read your Drive tokens,
so it must never be exposed to the network.
"""

import logging
import os
import sys
import threading
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_from_directory

from . import __version__
from . import conflicts
from . import engine
from .auth import (AuthError, access_token_persisting, interactive_auth,
                   token_from_json, whoami)
from .config import db_path, load_config, save_config
from .db import Store, parse_include_patterns, to_local
from .drive import Drive, DriveError, FOLDER_MIME, resolve_folder_entry

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
LOOPBACK = ("127.0.0.1", "localhost", "::1")


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


def create_app(store: Store, worker=None):
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False

    # ---- helpers --------------------------------------------------
    def drive_for(account_id):
        account = store.account(account_id)
        if not account:
            raise DriveError("account not found")
        token = token_from_json(account["token_json"])
        return Drive(lambda: access_token_persisting(store, account["id"],
                                                     token))

    def api_error(exc, code=400):
        return jsonify({"ok": False, "error": str(exc)}), code

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
        if request.headers.get("X-DriveSync") != "1":
            return api_error("missing X-DriveSync header", 403)
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
        return send_from_directory(STATIC_DIR, "index.html")

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
            "db": str(db_path()),
        })

    # ---- accounts --------------------------------------------------
    @app.get("/api/accounts")
    def accounts_list():
        out = []
        for a in store.accounts():
            item = {"id": a["id"], "email": a["email"],
                    "created_at": a["created_at"]}
            try:
                about = drive_for(a["id"]).about()
                item["quota"] = about.get("storageQuota", {})
                item["name"] = (about.get("user") or {}).get("displayName")
            except Exception as exc:
                item["error"] = str(exc)
            out.append(item)
        return jsonify({"ok": True, "accounts": out})

    @app.post("/api/accounts")
    def accounts_add():
        """Store OAuth client credentials and start the consent flow."""
        data = request.get_json(force=True, silent=True) or {}
        current = load_config()
        # the form may omit credentials (re-auth with the saved client):
        # only overwrite when a non-empty value was actually posted
        updates = {}
        for key in ("client_id", "client_secret"):
            value = (data.get(key) or "").strip()
            if value:
                updates[key] = value
        cfg = save_config({**current, **updates})
        try:
            tokens = interactive_auth(cfg)
            email = whoami(tokens["access_token"]).get("emailAddress", "unknown")
        except AuthError as exc:
            return api_error(exc)
        row = store.add_account(email, tokens)
        store.log(None, "account-added", "", email)
        return jsonify({"ok": True, "account": {"id": row["id"], "email": email}})

    @app.delete("/api/accounts/<int:account_id>")
    def accounts_delete(account_id):
        store.delete_account(account_id)
        return jsonify({"ok": True})

    # ---- drive browsing (folder picker) -----------------------------
    @app.get("/api/drive/<int:account_id>/folders")
    def drive_folders(account_id):
        """List folders (and root info) for the picker UI.

        parent=shared lists 'Shared with me' (folders other accounts shared);
        shortcuts to folders resolve to their real target ids so pairs bind
        to the folder itself, not the shortcut.
        """
        parent = request.args.get("parent", "root")
        try:
            drive = drive_for(account_id)
            if parent == "shared":
                folders = drive.list_shared_folders()
                current = {"id": "shared", "name": "Shared with me"}
            else:
                children = drive.list_children(parent)
                folders = [f for f in (resolve_folder_entry(c) for c in children)
                           if f]
                current = None
                if parent != "root":
                    current = drive.get(parent, fields="id,name,parents")
        except DriveError as exc:
            return api_error(exc, 502)
        if parent == "root":
            folders = [{"id": "shared", "name": "Shared with me"}] + folders
        return jsonify({"ok": True, "current": current, "folders": folders})

    @app.get("/api/drive/<int:account_id>/children")
    def drive_children(account_id):
        """Full child listing (files AND folders) for on-demand downloads.

        parent=root lists My Drive; parent=shared lists 'Shared with me'
        folders. Rows carry the metadata the browser needs (id, name,
        mimeType, size, modifiedTime); Google-native docs have no size.
        """
        parent = request.args.get("parent", "root")
        try:
            drive = drive_for(account_id)
            if parent == "shared":
                items = drive.list_shared_folders()
                current = {"id": "shared", "name": "Shared with me"}
            else:
                items = drive.list_children(parent)
                current = None
                if parent != "root":
                    current = drive.get(parent, fields="id,name,parents")
        except DriveError as exc:
            return api_error(exc, 502)
        return jsonify({"ok": True, "current": current, "items": items})

    @app.get("/api/drive/<int:account_id>/search")
    def drive_search(account_id):
        """Drive-wide name search (files and folders, folders first).

        ?q= the text to find, ?folders_only=1 restricts to folders (the
        pair picker uses that mode). Capped at 50 rows — enough to locate
        anything by name without rendering a tree.
        """
        query = (request.args.get("q") or "").strip()
        folders_only = request.args.get("folders_only") in ("1", "true")
        if not query:
            return jsonify({"ok": True, "items": []})
        try:
            drive = drive_for(account_id)
            items = drive.search(query, folders_only=folders_only)
        except DriveError as exc:
            return api_error(exc, 502)
        return jsonify({"ok": True, "items": items})

    # ---- pairs ------------------------------------------------------
    def pair_json(p):
        last = None
        if worker is not None:
            last = (worker.status.get("pairs") or {}).get(p["id"])
        return {"id": p["id"], "account_id": p["account_id"],
                "local_path": p["local_path"], "remote_id": p["remote_id"],
                "remote_path": p["remote_path"], "direction": p["direction"],
                "conflict_policy": p["conflict_policy"],
                "enabled": bool(p["enabled"]),
                "allow_deletes": bool(p["allow_deletes"]),
                "symlink_policy": p["symlink_policy"] or "skip",
                "case_insensitive": bool(p["case_insensitive"]),
                "paused_until": p["paused_until"] or None,
                "include_patterns": parse_include_patterns(
                    p["include_patterns"]),
                "last_sync": last}

    @app.get("/api/pairs")
    def pairs_list():
        return jsonify({"ok": True,
                        "pairs": [pair_json(p) for p in store.pairs()]})

    @app.post("/api/pairs")
    def pairs_add():
        data = request.get_json(force=True, silent=True) or {}
        try:
            local = os.path.realpath(os.path.expanduser(data["local_path"]))
            if not os.path.isdir(local):
                return api_error(f"local path is not a directory: {local}")
            account_id = int(data["account_id"])
            remote_id = data["remote_id"]
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
                direction=data.get("direction", "bidirectional"),
                conflict_policy=data.get("conflict_policy")
                or load_config()["conflict_policy"],
                enabled=data.get("enabled", 1),
                allow_deletes=data.get("allow_deletes", 1),
                include_patterns=data.get("include_patterns"),
                symlink_policy=data.get("symlink_policy", "skip"),
                case_insensitive=data.get("case_insensitive", 0))
        except KeyError as exc:
            return api_error(f"missing field: {exc}")
        except ValueError as exc:
            return api_error(str(exc))
        store.log(pair["id"], "pair-added", "",
                  f"{pair['local_path']} <-> {pair['remote_path'] or pair['remote_id']}")
        return jsonify({"ok": True, "pair": pair_json(pair)})

    @app.patch("/api/pairs/<int:pair_id>")
    def pairs_update(pair_id):
        data = request.get_json(force=True, silent=True) or {}
        try:
            pair = store.update_pair(pair_id, **data)
        except ValueError as exc:
            return api_error(str(exc))
        return jsonify({"ok": True, "pair": pair_json(pair)})

    @app.delete("/api/pairs/<int:pair_id>")
    def pairs_delete(pair_id):
        store.delete_pair(pair_id)
        return jsonify({"ok": True})

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
        data = request.get_json(force=True, silent=True) or {}
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

    @app.post("/api/sync")
    def sync_all():
        if worker is None:
            return api_error("worker not available", 503)
        threading.Thread(target=worker.run_pass, daemon=True).start()
        return jsonify({"ok": True, "started": True})

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
        data = request.get_json(force=True, silent=True) or {}
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
        if worker:
            worker.apply_config(cfg)
        return jsonify({"ok": True, "settings": _public_settings(cfg)})

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
