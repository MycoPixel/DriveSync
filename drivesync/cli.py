"""Command line interface.

  drivesync gui             open the native desktop app (GTK window)
  drivesync serve           run the dashboard + background worker
  drivesync auth            authorize a Google account from the terminal
  drivesync accounts        list / remove accounts
  drivesync pairs           add / list / edit / remove sync pairs
  drivesync sync            run one sync pass now (optionally one pair)
  drivesync status          show accounts, pairs and recent activity
  drivesync config          get / set settings
  drivesync doctor          sanity-check the installation
"""

import argparse
import os
import sys

from . import __version__
from .config import app_dir, load_config, update_config
from .db import Store, to_local
from .server import create_app


def _open_store():
    return Store(app_dir() / "state.db")


def cmd_serve(args):
    from .worker import Worker
    from .server import run_server
    store = _open_store()
    cfg = load_config()
    worker = Worker(store, cfg, interval=args.interval or cfg["sync_interval"])
    worker.start()
    url = f"http://127.0.0.1:{args.port or cfg['server_port']}"
    print(f"DriveSync {__version__} — dashboard at {url}  (Ctrl+C to stop)")
    if cfg.get("tray_enabled", True):
        from . import tray
        _, tray_msg = tray.start(worker, url)
        print(f"  {tray_msg}")
    try:
        run_server(store, worker, host=args.host, port=args.port)
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        store.close()


def cmd_auth(args):
    import getpass

    from .auth import interactive_auth, whoami
    store = _open_store()
    cfg = load_config()
    have_id = cfg.get("client_id") or os.environ.get("GOOGLE_CLIENT_ID")
    have_secret = cfg.get("client_secret") or os.environ.get("GOOGLE_CLIENT_SECRET")
    if not (have_id and have_secret):
        print("One-time setup: DriveSync authorizes with your own Google OAuth")
        print("'Desktop app' client (see README for the 2-minute walkthrough).\n")
        try:
            cid = input("Client ID: ").strip()
            csec = getpass.getpass("Client secret: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nerror: input cancelled — run 'drivesync auth' again, or set "
                  "the credentials with 'drivesync config set client_id=...'",
                  file=sys.stderr)
            store.close()
            sys.exit(1)
        if not cid or not csec:
            print("error: both client ID and secret are required", file=sys.stderr)
            store.close()
            sys.exit(1)
        cfg = update_config(client_id=cid, client_secret=csec)
        print("saved — you will not be asked again.\n")
    try:
        tokens = interactive_auth(cfg)
        email = whoami(tokens["access_token"]).get("emailAddress", "unknown")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    row = store.add_account(email, tokens)
    print(f"authorized {email} (account #{row['id']})")
    store.close()


def cmd_accounts(args):
    store = _open_store()
    if args.action == "list":
        for a in store.accounts():
            print(f"[{a['id']}] {a['email']}")
    elif args.action == "remove":
        store.delete_account(args.account_id)
        print(f"removed account #{args.account_id}")
    store.close()


def cmd_pairs(args):
    store = _open_store()
    cfg = load_config()
    if args.action == "add":
        local = os.path.realpath(os.path.expanduser(args.local))
        if not os.path.isdir(local):
            sys.exit(f"error: not a directory: {local}")
        if not args.remote_id:
            sys.exit("error: --remote-id is required (a Drive folder ID; "
                     "find it in the folder's URL: .../folders/<ID>)")
        pair = store.add_pair(args.account, local, args.remote_id,
                              remote_path=args.remote_path or "",
                              direction=args.direction,
                              conflict_policy=args.conflict_policy
                              or cfg["conflict_policy"])
        print(f"added pair #{pair['id']}: {local} <-> {args.remote_path or args.remote_id}")
    elif args.action == "list":
        for p in store.pairs():
            state = "enabled" if p["enabled"] else "disabled"
            print(f"[{p['id']}] {p['direction']:<13} {p['local_path']} <-> "
                  f"{p['remote_path'] or p['remote_id']} ({state}, "
                  f"conflicts: {p['conflict_policy']})")
    elif args.action == "set":
        fields = {}
        for kv in args.fields:
            k, _, v = kv.partition("=")
            if k == "enabled":
                v = 1 if v.lower() in ("1", "true", "yes") else 0
            fields[k] = v
        pair = store.update_pair(args.pair_id, **fields)
        print(f"updated pair #{pair['id']}")
    elif args.action == "remove":
        store.delete_pair(args.pair_id)
        print(f"removed pair #{args.pair_id}")
    store.close()


def cmd_sync(args):
    from .worker import Worker
    store = _open_store()
    worker = Worker(store, load_config())
    stats, ok = worker.run_pass(args.pair)  # one pass, no loop
    pairs = [store.pair(args.pair)] if args.pair else store.pairs()
    _ = pairs
    worker.status  # noqa: B018 — populated by run_pass
    if worker.status.get("last_stats"):
        s = worker.status["last_stats"]
        print(f"synced pair #{s['pair_id']}: pushed {s['pushed']}, "
              f"pulled {s['pulled']}, deleted {s['deleted']}, "
              f"renamed {s['renamed']}, conflicts {s['conflicts']}")
    for e in reversed(store.recent_events(limit=30)):
        lvl = "" if e["level"] == "info" else f" [{e['level']}]"
        print(f"  {to_local(e['ts'])}  {e['action']:<22} {e['rel_path']}{lvl} {e['message']}")
    store.close()
    sys.exit(0 if ok else 2)


def cmd_status(args):
    store = _open_store()
    cfg = load_config()
    print(f"DriveSync {__version__} — data dir: {app_dir()}")
    print(f"interval: {cfg['sync_interval']}s  conflicts: {cfg['conflict_policy']}")
    accounts = store.accounts()
    print(f"accounts: {len(accounts)}")
    for a in accounts:
        print(f"  [{a['id']}] {a['email']}")
    pairs = store.pairs()
    print(f"pairs: {len(pairs)}")
    for p in pairs:
        print(f"  [{p['id']}] {p['direction']:<13} {p['local_path']} <-> "
              f"{p['remote_path'] or p['remote_id']}")
    events = store.recent_events(limit=5)
    print("recent activity:")
    for e in events:
        print(f"  {to_local(e['ts'])} [{e['level']}] {e['action']} {e['rel_path']} {e['message']}")
    store.close()


def cmd_config(args):
    if args.action == "set":
        key, _, value = args.key_value.partition("=")
        if key == "sync_interval":
            value = int(value)
        if key in ("ignore_patterns",):
            value = [v.strip() for v in value.split(",") if v.strip()]
        if key in ("tray_enabled", "convert_google_docs", "incremental_scan"):
            value = value.strip().lower() in ("1", "true", "yes", "on")
        cfg = update_config(**{key: value})
        print(f"saved: {key} = {cfg[key]}")
    else:
        for k, v in load_config().items():
            print(f"{k} = {v}")


def cmd_doctor(args):
    ok = True
    print(f"DriveSync {__version__} doctor")
    print(f"  data dir   : {app_dir()} {'OK' if app_dir().exists() else '(will be created)'}")
    try:
        import flask  # noqa: F401
        print("  flask      : OK")
    except ImportError:
        ok = False
        print("  flask      : MISSING (pip install flask)")
    try:
        import requests  # noqa: F401
        print("  requests   : OK")
    except ImportError:
        ok = False
        print("  requests   : MISSING (pip install requests)")
    cfg = load_config()
    has_client = bool(cfg.get("client_id")) or bool(os.environ.get("GOOGLE_CLIENT_ID"))
    print(f"  oauth client: {'configured' if has_client else 'NOT set (see README)'}")
    store = _open_store()
    print(f"  accounts   : {len(store.accounts())}")
    print(f"  pairs      : {len(store.pairs())}")
    accounts = store.accounts()
    if accounts:
        from .auth import access_token, token_from_json
        from .drive import Drive, DriveError
        token = token_from_json(accounts[0]["token_json"])
        stored_cid = token.get("client_id") if isinstance(token, dict) else None
        cfg_cid = cfg.get("client_id") or os.environ.get("GOOGLE_CLIENT_ID", "")
        if stored_cid and cfg_cid and stored_cid != cfg_cid:
            ok = False
            print(f"  drive api  : STALE AUTH — account #{accounts[0]['id']} was "
                  f"authorized with client {stored_cid[:16]}…, but the configured "
                  f"client is {cfg_cid[:16]}… (after recreating the console setup). "
                  f"Fix: 'drivesync accounts remove {accounts[0]['id']}' then "
                  "'drivesync auth'.")
        else:
            try:
                drive = Drive(lambda: access_token(token))
                email = drive.about().get("user", {}).get("emailAddress", "?")
                print(f"  drive api  : OK ({email})")
            except DriveError as exc:
                ok = False
                print(f"  drive api  : FAILED — {str(exc)[:260]}")
            except Exception as exc:
                ok = False
                print(f"  drive api  : FAILED — {exc}")
    else:
        print("  drive api  : (no account authorized yet)")
    store.close()
    sys.exit(0 if ok else 1)


def cmd_gui(args):
    try:
        from .gui import run
    except (ImportError, ValueError) as exc:
        # ImportError: PyGObject missing; ValueError: gi.require_version
        # could not find the Gtk/Gdk 3.0 typelibs
        print("The desktop UI needs GTK (PyGObject):\n"
              "  Fedora/Ultramarine: sudo dnf install python3-gobject\n"
              "  Debian/Ubuntu:      sudo apt install python3-gi\n"
              "Headless alternative: 'drivesync serve' + the web dashboard.")
        sys.exit(f"detail: {exc}")
    run()


def build_parser():
    p = argparse.ArgumentParser(prog="drivesync",
                                description="Google Drive sync client for Linux")
    p.add_argument("--version", action="version",
                   version=f"drivesync {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("gui", help="open the desktop app (GTK window)")
    s.set_defaults(fn=cmd_gui)

    s = sub.add_parser("serve", help="run dashboard + background worker")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--interval", type=int, default=None,
                   help="override sync interval (seconds)")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("auth", help="authorize a Google account")
    s.set_defaults(fn=cmd_auth)

    s = sub.add_parser("accounts", help="list/remove accounts")
    s.add_argument("action", choices=["list", "remove"])
    s.add_argument("account_id", nargs="?", type=int)
    s.set_defaults(fn=cmd_accounts)

    s = sub.add_parser("pairs", help="manage sync pairs")
    s.add_argument("action", choices=["add", "list", "set", "remove"])
    s.add_argument("local", nargs="?", help="local folder (for add)")
    s.add_argument("--account", type=int, default=1)
    s.add_argument("--remote-id", help="Drive folder ID")
    s.add_argument("--remote-path", help="display path, e.g. 'My Drive/projects'")
    s.add_argument("--direction", default="bidirectional",
                   choices=["bidirectional", "upload", "download"])
    s.add_argument("--conflict-policy", default=None)
    s.add_argument("--pair-id", type=int, help="pair id (for set/remove)")
    s.add_argument("fields", nargs="*", help="key=value fields (for set)")
    s.set_defaults(fn=cmd_pairs)

    s = sub.add_parser("sync", help="run one sync pass and exit")
    s.add_argument("--pair", type=int, default=None)
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("status", help="show summary")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("config", help="get/set settings")
    s.add_argument("action", choices=["get", "set"])
    s.add_argument("key_value", nargs="?", help="key=value (for set)")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("doctor", help="sanity-check installation")
    s.set_defaults(fn=cmd_doctor)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.fn(args)
