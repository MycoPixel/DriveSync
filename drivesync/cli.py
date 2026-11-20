"""Command line interface.

  drivesync gui             open the native desktop app (GTK window)
  drivesync serve           run the dashboard + background worker
  drivesync auth            authorize a Google account from the terminal
  drivesync accounts        list / remove accounts
  drivesync pairs           add / list / edit / remove sync pairs
  drivesync sync            run one sync pass now (optionally one pair; --full
                            rehashes files saved with an unchanged mtime)
  drivesync status          show accounts, pairs and recent activity
  drivesync conflicts       list / resolve conflict copies from the terminal
  drivesync config          get / set settings
  drivesync doctor          sanity-check the installation
"""

import argparse
import os
import sys
import threading

from . import __version__
from .config import DEFAULT_CONFIG, app_dir, load_config, update_config
from .db import Store, parse_include_patterns, to_local


def _open_store():
    return Store(app_dir() / "state.db")


def _open_browser(url):
    """Best-effort 'open the dashboard tab' — never an error when headless."""
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def _port_busy(url):
    """True when something already accepts TCP connections at url."""
    import socket
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    try:
        with socket.create_connection((host, parts.port or 80), timeout=0.3):
            return True
    except OSError:
        return False


def _port_hint(url, detail):
    return (
        f"error: the dashboard port is unavailable ({detail}).\n"
        "  A DriveSync service may already be running — its dashboard is "
        f"probably at {url}.\n"
        "  Check:    systemctl --user status drivesync\n"
        "  Stop it:  systemctl --user stop drivesync\n"
        "  Or move:  drivesync config set server_port=5679")


def cmd_serve(args):
    from .worker import Worker
    from .server import is_loopback_host, run_server
    if not is_loopback_host(args.host):
        sys.exit(
            "error: the DriveSync dashboard has no remote authentication; "
            "use --host 127.0.0.1 (or localhost / ::1).")
    store = _open_store()
    if getattr(store, "recovered", False):
        store.log(None, "db-recovered", "",
                  "state.db was corrupt; it was restored from the automatic "
                  "backup taken after the last clean pass")
        print("note: state.db was corrupt — restored from state.db.bak")
    cfg = load_config()
    url = f"http://127.0.0.1:{args.port or cfg['server_port']}"
    # Probe first: werkzeug treats a busy port as a fatal error and exits
    # the process itself, so the friendly hint after run_server never runs.
    if _port_busy(url):
        store.close()
        print(_port_hint(url, f"port {args.port or cfg['server_port']} is in use"),
              file=sys.stderr)
        sys.exit(1)
    # --interval wins for this run; otherwise the worker follows the config
    # and hot-reloads it on every pass (config_source).
    worker = Worker(store, cfg, interval=args.interval,
                    config_source=load_config)
    worker.start()
    print(f"DriveSync {__version__} — dashboard at {url}  (Ctrl+C to stop)")
    if args.open:
        threading.Thread(target=_open_browser, args=(url,), daemon=True,
                         name="drivesync-open").start()
    if cfg.get("tray_enabled", True):
        from . import tray
        _, tray_msg = tray.start(worker, url)
        print(f"  {tray_msg}")
    watch_handle = None
    if cfg.get("watch_enabled", True):
        from . import watch
        watch_handle, watch_msg = watch.start(worker, store, cfg)
        print(f"  {watch_msg}")
    try:
        run_server(store, worker, host=args.host, port=args.port)
    except OSError as exc:
        # still possible when the port is grabbed between probe and bind
        print(_port_hint(url, exc), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        if watch_handle:
            watch_handle.stop()
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
        if not args.local:
            sys.exit("usage: drivesync pairs add <local-folder> "
                     "--remote-id <Drive-folder-ID>")
        local = os.path.realpath(os.path.expanduser(args.local))
        if not os.path.isdir(local):
            sys.exit(f"error: not a directory: {local}")
        if not args.remote_id:
            sys.exit("error: --remote-id is required (a Drive folder ID; "
                     "find it in the folder's URL: .../folders/<ID>)")
        if not store.account(args.account):
            sys.exit(f"error: no account #{args.account} — connect one with "
                     "'drivesync auth' first (see what is connected with "
                     "'drivesync accounts list')")
        pair = store.add_pair(args.account, local, args.remote_id,
                              remote_path=args.remote_path or "",
                              direction=args.direction,
                              conflict_policy=args.conflict_policy
                              or cfg["conflict_policy"],
                              include_patterns=args.include)
        print(f"added pair #{pair['id']}: {local} <-> {args.remote_path or args.remote_id}")
    elif args.action == "list":
        for p in store.pairs():
            state = "enabled" if p["enabled"] else "disabled"
            scope = parse_include_patterns(p["include_patterns"])
            scope_txt = ("everything" if not scope
                         else f"subfolders: {', '.join(scope)}")
            print(f"[{p['id']}] {p['direction']:<13} {p['local_path']} <-> "
                  f"{p['remote_path'] or p['remote_id']} ({state}, "
                  f"conflicts: {p['conflict_policy']}, scope: {scope_txt})")
    elif args.action == "set":
        # argparse fills the optional `local` positional before `fields`,
        # so a lone 'pairs set key=value' lands in args.local — accept both
        kvs = ([args.local] if args.local else []) + args.fields
        fields = {}
        for kv in kvs:
            k, _, v = kv.partition("=")
            if not k or k not in {"local_path", "remote_id", "remote_path",
                                  "direction", "conflict_policy", "enabled",
                                  "allow_deletes", "include_patterns",
                                  "symlink_policy", "case_insensitive"}:
                sys.exit(f"error: unknown pair field '{kv}' — valid fields: "
                         "local_path, remote_id, remote_path, direction, "
                         "conflict_policy, enabled, allow_deletes, "
                         "include_patterns, symlink_policy, case_insensitive")
            if k in ("enabled", "allow_deletes", "case_insensitive"):
                v = 1 if v.lower() in ("1", "true", "yes") else 0
            fields[k] = v
        if not store.pair(args.pair_id):
            sys.exit(f"error: no sync pair #{args.pair_id} — list them with "
                     "'drivesync pairs list'")
        try:
            pair = store.update_pair(args.pair_id, **fields)
        except ValueError as exc:
            sys.exit(f"error: {exc}")
        print(f"updated pair #{pair['id']}")
    elif args.action == "remove":
        store.delete_pair(args.pair_id)
        print(f"removed pair #{args.pair_id}")
    store.close()


def cmd_sync(args):
    from .worker import Worker
    store = _open_store()
    if args.pair is not None and not store.pair(args.pair):
        store.close()
        sys.exit(f"error: no sync pair #{args.pair} — list them with "
                 "'drivesync pairs list'")
    if not store.pairs(enabled_only=True):
        print("No enabled sync pairs yet — nothing to sync.")
        print("Add one in the dashboard, or:")
        print("  drivesync pairs add <local-folder> --remote-id <Drive-folder-ID>")
        print("(find the folder ID in its web URL: .../folders/<ID>)")
        store.close()
        return
    worker = Worker(store, load_config(), config_source=load_config)
    stats, ok = worker.run_pass(args.pair, force_full=args.full)  # one pass
    scope = f"pair #{args.pair}" if args.pair is not None else "all pairs"
    print(f"synced {scope}: pushed {int(stats.pushed)}, "
          f"pulled {int(stats.pulled)}, deleted {int(stats.deleted)}, "
          f"renamed {int(stats.renamed)}, conflicts {int(stats.conflicts)}, "
          f"errors {int(stats.errors)}")
    for e in reversed(store.recent_events(limit=30)):
        lvl = "" if e["level"] == "info" else f" [{e['level']}]"
        print(f"  {to_local(e['ts'])}  {e['action']:<22} {e['rel_path']}{lvl} {e['message']}")
    store.close()
    sys.exit(0 if ok else 2)


def cmd_conflicts(args):
    """Terminal access to the conflict-review engine (drivesync.conflicts)."""
    from .conflicts import CHOICES, list_conflicts, resolve_conflict
    store = _open_store()
    try:
        if args.action == "list":
            pair_id = args.pair
            if pair_id is None and args.target is not None:
                try:
                    pair_id = int(args.target)
                except ValueError:
                    sys.exit("error: 'conflicts list' takes an optional pair "
                             "id (e.g. 'drivesync conflicts list 2')")
            pairs = store.pairs()
            if pair_id is not None:
                pair = store.pair(pair_id)
                if not pair:
                    sys.exit(f"error: no sync pair #{pair_id} — list them "
                             "with 'drivesync pairs list'")
                pairs = [pair]
            found = 0
            for pair in pairs:
                items = list_conflicts(store, pair)
                for c in items:
                    found += 1
                    size = "-" if c["size"] is None else f"{c['size']} bytes"
                    where = "" if c["local_exists"] else "  (missing on disk)"
                    print(f"pair #{c['pair_id']}  {c['path']}")
                    print(f"    original: {c['original']}  ({size}){where}")
            if not found:
                print("No conflict copies — everything is resolved.")
            return
        # resolve
        if not args.target:
            sys.exit("error: give the conflict copy path: "
                     "'drivesync conflicts resolve <path> --keep ...'")
        if not args.keep:
            sys.exit("error: choose what to keep with --keep "
                     "copy|original|both (both just re-checks and exits)")
        choice = f"keep_{args.keep}"
        if choice not in CHOICES:
            sys.exit("error: --keep must be one of copy, original, both")
        pair_id = args.pair
        pair = store.pair(pair_id) if pair_id is not None else None
        if pair_id is not None and not pair:
            sys.exit(f"error: no sync pair #{pair_id} — list them with "
                     "'drivesync pairs list'")
        if pair is None:
            # no --pair given: find the pair that tracks this copy path
            matches = []
            for p in store.pairs():
                for c in list_conflicts(store, p):
                    if c["path"] == args.target:
                        matches.append((p, c))
            if not matches:
                sys.exit(f"error: no pair tracks a conflict copy named "
                         f"{args.target!r} — run 'drivesync conflicts list' "
                         "and pass --pair <id> to be explicit")
            if len(matches) > 1:
                listing = ", ".join(f"#{p['id']}" for p, _c in matches)
                sys.exit(f"error: several pairs track {args.target!r} ({listing})"
                         " — pass --pair <id>")
            pair = matches[0][0]
        account = store.account(pair["account_id"])
        if not account:
            sys.exit(f"error: pair #{pair['id']} has no authorized account")
        from .auth import access_token_persisting, token_from_json
        from .drive import Drive, DriveError
        token = token_from_json(account["token_json"])
        drive = Drive(lambda: access_token_persisting(store, account["id"],
                                                      token))
        try:
            result = resolve_conflict(store, pair, drive, args.target, choice)
        except ValueError as exc:
            sys.exit(f"error: {exc}")
        except DriveError as exc:
            sys.exit(f"error: Drive refused the resolution: {exc}")
        verb = {"keep_copy": "kept the copy", "keep_original": "kept the original",
                "keep_both": "left both copies in place"}[choice]
        print(f"pair #{result['pair_id']}: {verb} — {result['path']} "
              f"(original: {result['original']})")
    finally:
        store.close()


def cmd_status(args):
    store = _open_store()
    cfg = load_config()
    print(f"DriveSync {__version__} — data dir: {app_dir()}")
    print(f"interval: {cfg['sync_interval']}s  conflicts: {cfg['conflict_policy']}")
    accounts = store.accounts()
    print(f"accounts: {len(accounts)}")
    for a in accounts:
        print(f"  [{a['id']}] {a['email']}")
    if not accounts:
        print("  none yet — connect one with 'drivesync auth' (or 'drivesync gui')")
    pairs = store.pairs()
    print(f"pairs: {len(pairs)}")
    for p in pairs:
        print(f"  [{p['id']}] {p['direction']:<13} {p['local_path']} <-> "
              f"{p['remote_path'] or p['remote_id']}")
    if not pairs:
        print("  none yet — add one in the dashboard, or:")
        print("  drivesync pairs add <local-folder> --remote-id <Drive-folder-ID>")
    events = store.recent_events(limit=5)
    print("recent activity:")
    for e in events:
        print(f"  {to_local(e['ts'])} [{e['level']}] {e['action']} {e['rel_path']} {e['message']}")
    store.close()


def cmd_config(args):
    if args.action == "set":
        key, _, value = args.key_value.partition("=")
        if key not in DEFAULT_CONFIG:
            sys.exit("error: unknown setting '{}' — valid settings:\n  {}".format(
                key, "\n  ".join(sorted(DEFAULT_CONFIG))))
        try:
            if key in ("sync_interval", "bandwidth_limit_kb"):
                value = int(value)
            if key == "ignore_patterns":
                value = [v.strip() for v in value.split(",") if v.strip()]
            if key in ("tray_enabled", "convert_google_docs",
                       "incremental_scan", "watch_enabled"):
                value = value.strip().lower() in ("1", "true", "yes", "on")
            cfg = update_config(**{key: value})
        except ValueError as exc:
            sys.exit(f"error: {exc}")
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
    try:
        import watchdog  # noqa: F401
        print("  watchdog   : OK (instant local-change sync)")
    except ImportError:
        print("  watchdog   : optional — install python3-watchdog for "
              "instant local-change sync")
    try:
        from . import notify
        state = "OK (desktop notifications)" if notify.available() \
            else "unavailable — desktop notifications disabled"
        print(f"  libnotify  : {state}")
    except Exception:
        print("  libnotify  : unavailable — desktop notifications disabled")
    cfg = load_config()
    has_client = bool(cfg.get("client_id")) or bool(os.environ.get("GOOGLE_CLIENT_ID"))
    print(f"  oauth client: {'configured' if has_client else 'NOT set (see README)'}")
    store = _open_store()
    print(f"  accounts   : {len(store.accounts())}")
    print(f"  pairs      : {len(store.pairs())}")
    accounts = store.accounts()
    if accounts:
        from .auth import access_token_persisting, token_from_json
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
                drive = Drive(lambda: access_token_persisting(
                    store, accounts[0]["id"], token))
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
    sub = p.add_subparsers(dest="cmd", required=False)

    s = sub.add_parser("gui", help="open the desktop app (GTK window)")
    s.set_defaults(fn=cmd_gui)

    s = sub.add_parser("serve", help="run dashboard + background worker")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--open", action="store_true",
                   help="open the dashboard in a browser once it is up")
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
    s.add_argument("--include", default=None,
                   help="selective sync: comma-separated subfolder paths to "
                        "mirror, e.g. docs,photos (default: everything)")
    s.add_argument("--pair-id", type=int, help="pair id (for set/remove)")
    s.add_argument("fields", nargs="*", help="key=value fields (for set)")
    s.set_defaults(fn=cmd_pairs)

    s = sub.add_parser("sync", help="run one sync pass and exit")
    s.add_argument("--pair", type=int, default=None)
    s.add_argument("--full", action="store_true",
                   help="re-list Drive and rehash local files — the recovery "
                        "for editors that save with an unchanged mtime")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("conflicts", help="list/resolve conflict copies")
    s.add_argument("action", choices=["list", "resolve"])
    s.add_argument("target", nargs="?",
                   help="pair id (list) / conflict copy path (resolve)")
    s.add_argument("--pair", type=int, default=None,
                   help="restrict listing to one pair / resolve within it")
    s.add_argument("--keep", choices=["copy", "original", "both"],
                   help="what wins when resolving (copy = the .conflict-… "
                        "file replaces the original)")
    s.set_defaults(fn=cmd_conflicts)

    s = sub.add_parser("status", help="show summary")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("config", help="get/set settings")
    s.add_argument("action", choices=["get", "set"])
    s.add_argument("key_value", nargs="?", help="key=value (for set)")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("doctor", help="sanity-check installation")
    s.set_defaults(fn=cmd_doctor)
    return p


def _getting_started():
    """Shown by bare 'drivesync' — the first thing a new user runs."""
    print(f"DriveSync {__version__} — Google Drive sync for Linux desktops")
    print(f"data dir: {app_dir()}")
    print("\nNext steps:")
    print("  drivesync gui       open the desktop app (setup, sign-in, folders)")
    print("  drivesync auth      connect a Google account from the terminal")
    print("  drivesync serve     headless: dashboard + background worker")
    print("  drivesync doctor    check this installation")
    print("\nAll commands: drivesync --help")


def main(argv=None):
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)

    if unknown:
        if args.cmd == "pairs" and args.action == "set":
            # Preserve key=value arguments that argparse does not
            # recognize as positional arguments on some Python versions.
            args.fields = (
                ([args.local] if args.local else [])
                + list(args.fields or [])
                + unknown
            )
            args.local = None
        else:
            parser.error("unrecognized arguments: " + " ".join(unknown))

    if getattr(args, "cmd", None) is None:
        _getting_started()
        return

    args.fn(args)
