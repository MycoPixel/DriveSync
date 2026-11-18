"""Local web dashboard: JSON API + static single-page UI.

Bound to 127.0.0.1 by default — this server can read your Drive tokens,
so it must never be exposed to the network.
"""

import logging
import os
import sys
import threading

from flask import Flask, jsonify, request, send_from_directory

from . import __version__
from .auth import AuthError, access_token, interactive_auth, token_from_json, whoami
from .config import db_path, load_config, save_config
from .db import Store, to_local
from .drive import Drive, DriveError, FOLDER_MIME, SHORTCUT_MIME

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _folder_entry(item):
    """Picker-ready {id,name} for a folder — shortcuts resolve to their target."""
    if item.get("mimeType") == FOLDER_MIME:
        return {"id": item["id"], "name": item["name"]}
    if item.get("mimeType") == SHORTCUT_MIME:
        details = item.get("shortcutDetails") or {}
        if details.get("targetMimeType") == FOLDER_MIME:
            return {"id": details["targetId"], "name": item["name"]}
    return None


def create_app(store: Store, worker=None):
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False

    # ---- helpers --------------------------------------------------
    def drive_for(account_id):
        account = store.account(account_id)
        if not account:
            raise DriveError("account not found")
        token = token_from_json(account["token_json"])
        return Drive(lambda: access_token(token))

    def api_error(exc, code=400):
        return jsonify({"ok": False, "error": str(exc)}), code

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
        cfg = save_config({
            **load_config(),
            "client_id": data.get("client_id", ""),
            "client_secret": data.get("client_secret", ""),
        })
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
                folders = [f for f in (_folder_entry(c) for c in children) if f]
                current = None
                if parent != "root":
                    current = drive.get(parent, fields="id,name,parents")
        except DriveError as exc:
            return api_error(exc, 502)
        if parent == "root":
            folders = [{"id": "shared", "name": "Shared with me"}] + folders
        return jsonify({"ok": True, "current": current, "folders": folders})

    # ---- pairs ------------------------------------------------------
    def pair_json(p):
        return {"id": p["id"], "account_id": p["account_id"],
                "local_path": p["local_path"], "remote_id": p["remote_id"],
                "remote_path": p["remote_path"], "direction": p["direction"],
                "conflict_policy": p["conflict_policy"], "enabled": bool(p["enabled"]),
                "allow_deletes": bool(p["allow_deletes"])}

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
            pair = store.add_pair(
                int(data["account_id"]), local, data["remote_id"],
                remote_path=data.get("remote_path", ""),
                direction=data.get("direction", "bidirectional"),
                conflict_policy=data.get("conflict_policy")
                or load_config()["conflict_policy"],
                enabled=data.get("enabled", 1))
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

    @app.post("/api/sync")
    def sync_all():
        if worker is None:
            return api_error("worker not available", 503)
        threading.Thread(target=worker.run_pass, daemon=True).start()
        return jsonify({"ok": True, "started": True})

    # ---- events -----------------------------------------------------
    @app.get("/api/events")
    def events():
        limit = min(int(request.args.get("limit", 200)), 1000)
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
        return jsonify({"ok": True, "settings": load_config()})

    @app.post("/api/settings")
    def settings_set():
        data = request.get_json(force=True, silent=True) or {}
        try:
            # merge over the stored config: the form posts only its own
            # fields, and dropping unspecified keys would wipe credentials
            # and settings the UI does not send
            cfg = save_config({**load_config(), **data})
        except ValueError as exc:
            return api_error(str(exc))
        if worker:
            worker.interval = cfg["sync_interval"]
        return jsonify({"ok": True, "settings": cfg})

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
