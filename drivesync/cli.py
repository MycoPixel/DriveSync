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
  drivesync recovery        deletion holds, trash, versions, history
  drivesync config          get / set / export / import settings
  drivesync mount           read-only FUSE mount (optional extra)
  drivesync audit           credential/file-permission check
  drivesync doctor          sanity-check the installation
"""

import argparse
import os
import sys
import threading

from . import __version__
from .config import (DEFAULT_CONFIG, app_dir, db_path, load_config,
                     update_config)
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


def cmd_digest(args):
    """Phase 4.4: the digest content, on demand.

    Headless users get here what desktop users get as a notification:
    the activity window since the last digest (or the whole log when
    none has fired yet), plus whether/when the daily digest will fire.
    """
    from datetime import datetime
    store = _open_store()
    cfg = load_config()
    since_id = int(store.kv_get("last_digest_id") or 0)
    s = store.digest_summary(since_id)
    window = (f"since the last digest (event #{since_id})" if since_id
              else "the whole activity log (no digest has fired yet)")
    print(f"window: {window}")
    print(f"files: {s['files']} ({s['pushed']} pushed, {s['pulled']} pulled, "
          f"{s['deleted']} deleted)")
    print(f"conflicts: {s['conflicts']}  errors: {s['errors']}  "
          f"folders touched: {s['pairs']}")
    if not cfg.get("digest_enabled", False):
        print("daily digest: disabled (enable via the dashboard or "
              "'digest_enabled')")
        return
    at = str(cfg.get("digest_time") or "").strip() or "00:00"
    state = ("already sent today"
             if str(store.kv_get("last_digest_day") or "") ==
             datetime.now().date().isoformat()
             else f"will fire on the first tick at/after {at}")
    print(f"daily digest: enabled at {at} — {state}")


def cmd_network(args):
    """Phase 4.3: what the worker sees on the metered front.

    Useful headless: shows WHY automatic passes pause or throttle (or
    why they will not) without opening the dashboard.
    """
    from drivesync import netstate
    metered, source = netstate.is_metered()
    state = ("metered" if metered else
             "not metered" if metered is False else "unknown")
    print(f"network: {state} (source: {source})")
    cfg = load_config()
    if not cfg.get("respect_metered", True):
        print("respect_metered: off — the metered state is ignored")
        return
    if cfg.get("metered_action", "pause") == "throttle":
        cap = int(cfg.get("metered_bandwidth_kb", 0) or 0)
        cap_txt = f"{cap} KB/s" if cap else "no extra cap"
        print("respect_metered: on — while metered, automatic passes are "
              f"throttled to {cap_txt} (all rates capped)")
    else:
        print("respect_metered: on — while metered, automatic passes are "
              "paused (manual syncs still run)")


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
        from .drive import is_virtual_root
        if is_virtual_root(args.remote_id):
            sys.exit("error: '{}' is a browser view, not a folder — open a "
                     "folder (or a Shared Drive) inside it and use that "
                     "ID".format(args.remote_id))
        if not store.account(args.account):
            sys.exit(f"error: no account #{args.account} — connect one with "
                     "'drivesync auth' first (see what is connected with "
                     "'drivesync accounts list')")
        pair = store.add_pair(args.account, local, args.remote_id,
                              remote_path=args.remote_path or "",
                              direction=args.direction,
                              conflict_policy=args.conflict_policy
                              or cfg["conflict_policy"],
                              include_patterns=args.include,
                              bandwidth_limit_kb=args.bandwidth,
                              peak_from=args.peak_from,
                              peak_to=args.peak_to,
                              bandwidth_limit_kb_peak=args.peak_bandwidth)
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
                                  "symlink_policy", "case_insensitive",
                                  "bandwidth_limit_kb", "peak_from",
                                  "peak_to", "bandwidth_limit_kb_peak"}:
                sys.exit(f"error: unknown pair field '{kv}' — valid fields: "
                         "local_path, remote_id, remote_path, direction, "
                         "conflict_policy, enabled, allow_deletes, "
                         "include_patterns, symlink_policy, case_insensitive, "
                         "bandwidth_limit_kb, peak_from, peak_to, "
                         "bandwidth_limit_kb_peak")
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


def _pair_for_path(pairs, folder):
    """Most-specific enabled pair whose local root contains `folder`."""
    target = os.path.realpath(os.path.expanduser(folder))
    matches = []
    for pair in pairs:
        root = os.path.realpath(os.path.expanduser(str(pair["local_path"])))
        try:
            inside = os.path.commonpath([root, target]) == root
        except ValueError:          # different drives on Windows
            inside = False
        if inside:
            matches.append((len(root), pair))
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def cmd_sync(args):
    from .worker import Worker
    store = _open_store()
    if args.pair is not None and not store.pair(args.pair):
        store.close()
        sys.exit(f"error: no sync pair #{args.pair} — list them with "
                 "'drivesync pairs list'")
    if args.path:
        pairs = store.pairs(enabled_only=True)
        match = _pair_for_path(pairs, args.path)
        if not match:
            store.close()
            sys.exit(f"error: no enabled sync pair contains {args.path!r} — "
                     "add one with 'drivesync pairs add'")
        args.pair = match["id"]
        print(f"{args.path} belongs to pair #{match['id']} "
              f"({match['local_path']})")
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


def cmd_recovery(args):
    """Terminal access to the recovery layer (deletion holds, trash,
    versions, history).

    status/allow/keep handle the mass-deletion holds the safety valve
    writes; trash / drive-trash / versions are read-only listings; the
    restore-* actions put files back (each one is logged to the recovery
    history). Drive-backed actions need the pair's account tokens, exactly
    like 'conflicts resolve'.
    """
    from . import recovery
    store = _open_store()
    try:
        action = args.action

        def need_pair(pair_id):
            pair = store.pair(pair_id)
            if not pair:
                sys.exit(f"error: no sync pair #{pair_id} — list them with "
                         "'drivesync pairs list'")
            return pair

        def need_drive(pair):
            account = store.account(pair["account_id"])
            if not account:
                sys.exit(f"error: pair #{pair['id']} has no authorized "
                         "account")
            from .auth import access_token_persisting, token_from_json
            from .drive import Drive
            token = token_from_json(account["token_json"])
            return Drive(lambda: access_token_persisting(
                store, account["id"], token))

        if action == "status":
            holds = recovery.all_pending_deletions(store)
            if not holds:
                print("No deletions are held for confirmation.")
            for hold in holds:
                print(f"pair #{hold['pair_id']} ({hold.get('pair_name', '?')}): "
                      f"{hold['count']} deletion(s) held "
                      f"(delete_safety_limit={hold['limit']})")
                for rel in hold["paths"][:10]:
                    print(f"    {rel}")
                if hold.get("more"):
                    print(f"    … and {hold['more']} more")
                print(f"    confirm: 'drivesync recovery allow "
                      f"{hold['pair_id']}'  or  'drivesync recovery keep "
                      f"{hold['pair_id']}'")
            trashed = recovery.list_local_trash(store, limit=1000)
            print(f"\nLocal trash: {len(trashed)} file(s) recoverable "
                  "('drivesync recovery trash')")
            print("Drive trash / versions: 'drivesync recovery drive-trash "
                  "<pair>'  and  'drivesync recovery versions <pair> "
                  "--rel <path>'")
            return

        if action == "allow":
            pair = need_pair(args.pair)
            if not recovery.pending_deletions(store, pair["id"]):
                sys.exit("error: no deletions are held for this pair")
            recovery.allow_pending_deletions(store, pair["id"])
            print(f"pair #{pair['id']}: the held deletions are approved for "
                  "the next pass (one pass only).")
            return

        if action == "keep":
            pair = need_pair(args.pair)
            if not recovery.pending_deletions(store, pair["id"]):
                sys.exit("error: no deletions are held for this pair")
            recovery.keep_pending_files(store, pair["id"])
            print(f"pair #{pair['id']}: every held deletion was kept — "
                  "deletes are now OFF for this pair (re-enable with "
                  "'drivesync pairs set').")
            return

        if action == "trash":
            items = recovery.list_local_trash(
                store, pair_id=args.pair, limit=500)
            if not items:
                print("The local trash is empty.")
                return
            for it in items:
                print(f"pair #{it['pair_id']}  pass {it['stamp']}  "
                      f"{it['rel']}  ({it['size']} bytes)")
            return

        if action == "restore-trash":
            pair = need_pair(args.pair)
            if not args.stamp or not args.rel:
                sys.exit("error: restore-trash needs --stamp <pass-id> and "
                         "--rel <path> (see 'drivesync recovery trash')")
            try:
                result = recovery.restore_from_trash(store, pair["id"],
                                                     args.stamp, args.rel)
            except ValueError as exc:
                sys.exit(f"error: {exc}")
            note = " (renamed — the original path was occupied)" \
                if result["renamed"] else ""
            print(f"pair #{pair['id']}: restored to {result['rel']}{note}")
            return

        if action == "drive-trash":
            pair = need_pair(args.pair)
            from .drive import DriveError
            try:
                items = recovery.list_drive_trash(
                    pair, need_drive(pair), limit=100)
            except DriveError as exc:
                sys.exit(f"error: Drive refused the listing: {exc}")
            if not items:
                print("The Drive trash is empty.")
                return
            for it in items:
                print(f"{it['id']}  {it.get('name', '?')}  "
                      f"trashed {it.get('trashedTime') or '?'}")
            return

        if action == "restore-drive":
            pair = need_pair(args.pair)
            if not args.file_id:
                sys.exit("error: restore-drive needs --file-id (see "
                         "'drivesync recovery drive-trash <pair>')")
            from .drive import DriveError
            try:
                result = recovery.restore_drive_trash(
                    store, pair, need_drive(pair), args.file_id)
            except DriveError as exc:
                sys.exit(f"error: Drive refused the restore: {exc}")
            print(f"pair #{pair['id']}: {result['name']} is out of the "
                  "Drive trash; the next pass reconciles it.")
            return

        if action == "versions":
            pair = need_pair(args.pair)
            if not args.rel:
                sys.exit("error: versions needs --rel <path>")
            items = recovery.list_versions(pair["id"], args.rel)
            if not items:
                print("No kept previous versions for that path "
                      "(versions are kept when an overwrite pull happens).")
                return
            for it in items:
                print(f"{it['stamp']}  {it['rel']}  ({it['size']} bytes)")
            return

        if action == "restore-version":
            pair = need_pair(args.pair)
            if not args.rel or not args.stamp:
                sys.exit("error: restore-version needs --rel <path> and "
                         "--stamp <version> (see 'drivesync recovery "
                         "versions')")
            try:
                result = recovery.restore_version(store, pair["id"],
                                                  args.rel, args.stamp)
            except ValueError as exc:
                sys.exit(f"error: {exc}")
            print(f"pair #{pair['id']}: {result['rel']} is back at version "
                  f"{result['stamp']}; the next pass pushes it to Drive.")
            return

        # history
        rows = store.recent_recovery(limit=100)
        if not rows:
            print("The recovery history is empty.")
            return
        for r in rows:
            pair_txt = f"pair #{r['pair_id']}" if r["pair_id"] else "-"
            print(f"{to_local(r['ts'])}  {pair_txt}  {r['action']}  "
                  f"{r['rel_path']}")
            if r["detail"]:
                print(f"    {r['detail']}")
        return
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
    if args.action in ("export", "import"):
        from .portable import export_settings, import_settings
        if args.action == "import" and not args.key_value:
            sys.exit("error: usage: drivesync config import <file> [--dry-run]")
        path = args.key_value or "drivesync-config.json"
        store = Store(db_path())
        try:
            if args.action == "export":
                try:
                    export_settings(store, path)
                except OSError as exc:
                    sys.exit(f"error: cannot write {path}: {exc}")
                print(f"exported settings and {len(store.pairs())} pair(s) "
                      f"to {path}")
                print("  credentials are never exported — run "
                      "'drivesync auth' on the other machine")
                return
            try:
                report = import_settings(store, path, dry_run=args.dry_run)
            except ValueError as exc:
                sys.exit(f"error: {exc}")
        finally:
            store.close()
        prefix = "would import" if report["dry_run"] else "imported"
        print(f"{prefix} {len(report['config_keys'])} setting(s): "
              f"{', '.join(report['config_keys']) or 'none'}")
        print(f"{prefix} {len(report['added'])} pair(s)")
        for item in report["added"]:
            print(f"  + {item}")
        if report["skipped"]:
            print(f"skipped {len(report['skipped'])} pair(s):")
            for item in report["skipped"]:
                print(f"  - {item}")
        for warning in report["warnings"]:
            print(f"warning: {warning}")
        if report["dry_run"]:
            print("dry run: nothing was written")
        return
    if args.action == "set":
        key, _, value = args.key_value.partition("=")
        if key not in DEFAULT_CONFIG:
            sys.exit("error: unknown setting '{}' — valid settings:\n  {}".format(
                key, "\n  ".join(sorted(DEFAULT_CONFIG))))
        try:
            if key in ("sync_interval", "bandwidth_limit_kb",
                       "bandwidth_limit_kb_peak"):
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


def cmd_audit(args):
    from .security import apply_fixes, audit
    store = Store(db_path())
    try:
        report = audit(store)
    finally:
        store.close()
    print(f"DriveSync {__version__} security audit")
    for check in report["checks"]:
        mark = "OK  " if check["ok"] else "FAIL"
        print(f"  [{mark}] {check['name']}: {check['detail']}")
    if report["ok"]:
        print("all checks passed")
        return
    if args.fix:
        fixed = apply_fixes(report)
        print(f"applied {fixed} permission fix(es); re-run to confirm")
        return
    print("run 'drivesync audit --fix' to repair file permissions")
    sys.exit(1)


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


def cmd_mount(args):
    """Mount one Drive folder read-only via FUSE (optional extra).

    This blocks while the mount is active; run it in a terminal, from a
    systemd user unit, or with ``&``. The GUI Drive browser is the no-FUSE
    alternative.
    """
    from .auth import access_token_persisting, token_from_json
    from .drive import Drive, resolve_export_formats
    from .fuse import FuseUnavailable, mount

    store = _open_store()
    try:
        accounts = store.accounts()
        if not accounts:
            sys.exit("error: no account authorized — run 'drivesync auth' first")
        account = accounts[0]
        if args.account is not None:
            account = store.account(args.account)
            if not account:
                sys.exit(f"error: no account #{args.account}")
        token = token_from_json(account["token_json"])
        drive = Drive(lambda: access_token_persisting(store, account["id"],
                                                      token))
        try:
            mount(drive, args.mountpoint, root_id=args.remote_id,
                  export_formats=resolve_export_formats(load_config()),
                  allow_other=args.allow_other, debug=args.debug)
        except FuseUnavailable as exc:
            sys.exit(f"error: {exc}")
        except ValueError as exc:
            sys.exit(f"error: {exc}")
        except KeyboardInterrupt:
            pass
        print(f"unmounted {args.mountpoint}")
    finally:
        store.close()


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
    run(add_pair_path=getattr(args, "add_pair", None))


def build_parser():
    p = argparse.ArgumentParser(prog="drivesync",
                                description="Google Drive sync client for Linux")
    p.add_argument("--version", action="version",
                   version=f"drivesync {__version__}")
    sub = p.add_subparsers(dest="cmd", required=False)

    s = sub.add_parser("gui", help="open the desktop app (GTK window)")
    s.add_argument("--add-pair", metavar="FOLDER", default=None,
                   help="open the add-sync-folder dialog for this local "
                        "folder (used by the Dolphin context menu)")
    s.set_defaults(fn=cmd_gui)

    s = sub.add_parser("mount",
                       help="mount a Drive folder read-only (needs fusepy)")
    s.add_argument("mountpoint", help="empty local directory to mount on")
    s.add_argument("--account", type=int, default=None,
                   help="account id (default: the first authorized account)")
    s.add_argument("--remote-id", default="root",
                   help="Drive folder / Shared Drive id (default: My Drive)")
    s.add_argument("--allow-other", action="store_true",
                   help="let other local users read the mount")
    s.add_argument("--debug", action="store_true",
                   help="verbose FUSE request logging")
    s.set_defaults(fn=cmd_mount)

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
    s.add_argument("--bandwidth", default=None,
                   help="per-pair bandwidth cap in KB/s (default: inherit "
                        "the global limit)")
    s.add_argument("--peak-bandwidth", default=None,
                   help="per-pair cap inside the pair's peak window "
                        "(0 = fall back to the pair/global general cap)")
    s.add_argument("--peak-from", default=None,
                   help="pair peak window start, HH:MM (with --peak-to)")
    s.add_argument("--peak-to", default=None,
                   help="pair peak window end, HH:MM (may cross midnight)")
    s.add_argument("--pair-id", type=int, help="pair id (for set/remove)")
    s.add_argument("fields", nargs="*", help="key=value fields (for set)")
    s.set_defaults(fn=cmd_pairs)

    s = sub.add_parser("network",
                       help="show the metered-connection state DriveSync sees")
    s.set_defaults(fn=cmd_network)

    s = sub.add_parser("digest",
                       help="print the activity summary the daily digest uses")
    s.set_defaults(fn=cmd_digest)

    s = sub.add_parser("sync", help="run one sync pass and exit")
    s.add_argument("--pair", type=int, default=None)
    s.add_argument("--path", default=None,
                   help="sync the pair whose local folder contains this path "
                        "(used by the Dolphin context menu)")
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

    s = sub.add_parser("recovery",
                       help="deletion holds, trash, versions, history")
    s.add_argument("action",
                   choices=["status", "allow", "keep", "trash",
                            "restore-trash", "drive-trash", "restore-drive",
                            "versions", "restore-version", "history"],
                   help="status: held deletions + counts; allow: approve the "
                        "held deletions for one pass; keep: keep every file "
                        "(deletes off); trash: list locally preserved files; "
                        "drive-trash: list Google's trash; versions: list "
                        "kept previous versions; history: decision log")
    s.add_argument("pair", type=int, nargs="?", default=None,
                   help="sync pair id (allow/keep/restore-*/drive-trash/"
                        "versions)")
    s.add_argument("--rel", default=None,
                   help="path inside the pair (restore-trash, versions, "
                        "restore-version)")
    s.add_argument("--stamp", default=None,
                   help="trash pass id or version stamp for the restore "
                        "actions")
    s.add_argument("--file-id", default=None,
                   help="Drive file id for restore-drive")
    s.set_defaults(fn=cmd_recovery)

    s = sub.add_parser("status", help="show summary")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("config", help="get/set/export/import settings")
    s.add_argument("action", choices=["get", "set", "export", "import"])
    s.add_argument("key_value", nargs="?",
                   help="key=value (set) or file path (export/import)")
    s.add_argument("--dry-run", action="store_true",
                   help="show what 'config import' would change")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("audit",
                       help="check permissions and credential handling")
    s.add_argument("--fix", action="store_true",
                   help="repair insecure file permissions")
    s.set_defaults(fn=cmd_audit)

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
