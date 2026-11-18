"""Native GTK3 desktop UI for DriveSync (PyGObject).

A local window covering everything a fresh install needs: first-run setup
(your own Google OAuth client), account sign-in, sync pair management and
live activity — no browser dashboard required.

One step still opens a browser on purpose: Google's consent page during
account authorization (installed-app OAuth flow, loopback redirect on
127.0.0.1:auth_port). Everything else happens inside this window.

Requires python3-gobject (RPM) / python3-gi (deb). Import is lazy —
`drivesync serve` and the CLI never touch this module, so headless
machines without GTK are unaffected.
"""

import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import __version__  # noqa: E402
from .auth import interactive_auth, whoami  # noqa: E402
from .config import app_dir, load_config, update_config  # noqa: E402
from .db import Store  # noqa: E402
from .worker import Worker  # noqa: E402

DIRECTIONS = ("bidirectional", "upload", "download")
POLICIES = ("newer", "local", "remote", "both")

SETUP_HINT = (
    "DriveSync talks to Google with your own OAuth \u201cDesktop app\u201d client, so "
    "your tokens never pass through a third-party server. Create one once:\n"
    "  1. console.cloud.google.com \u2192 APIs & Services \u2192 enable the Drive API\n"
    "  2. OAuth consent screen \u2192 External \u2192 add yourself as a test user\n"
    "  3. Credentials \u2192 Create credentials \u2192 OAuth client ID \u2192 Desktop app\n"
    "  4. Paste the client ID and secret below."
)


def _list_view(headers, types):
    """TreeView + ListStore with the given column headers/types."""
    store = Gtk.ListStore(*types)
    view = Gtk.TreeView(model=store)
    view.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
    for i, header in enumerate(headers):
        cell = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn(header, cell, text=i)
        col.set_resizable(True)
        view.append_column(col)
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.add(view)
    return store, view, scroll


class DesktopApp:
    """Main window: Setup / Accounts / Folders / Activity."""

    def __init__(self):
        self.store = Store(app_dir() / "state.db")
        self.cfg = load_config()
        self.worker = Worker(self.store, self.cfg)
        self.worker.start()
        self._closing = False

        self.win = Gtk.Window(title=f"DriveSync {__version__}")
        self.win.set_default_size(780, 560)
        self.win.set_position(Gtk.WindowPosition.CENTER)
        self.win.connect("destroy", self._on_close)

        self.nb = Gtk.Notebook()
        self.nb.append_page(self._setup_tab(), Gtk.Label(label="Setup"))
        self.nb.append_page(self._accounts_tab(), Gtk.Label(label="Accounts"))
        self.nb.append_page(self._folders_tab(), Gtk.Label(label="Folders"))
        self.nb.append_page(self._activity_tab(), Gtk.Label(label="Activity"))

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                        spacing=6, border_width=10)
        outer.pack_start(self.nb, True, True, 0)
        self.win.add(outer)

        if not self.cfg.get("client_id"):
            self.nb.set_current_page(0)
        elif not self.store.accounts():
            self.nb.set_current_page(1)
        else:
            self.nb.set_current_page(3)
        GLib.timeout_add_seconds(3, self._tick)

    # ---- small helpers --------------------------------------------
    def _bg(self, fn, on_done=None):
        """Run fn in a thread; on_done(result) runs on the GTK thread."""

        def wrap():
            try:
                result = fn()
            except Exception as exc:
                GLib.idle_add(self._error,
                              f"{exc.__class__.__name__}: {exc}")
                return
            if on_done:
                GLib.idle_add(on_done, result)

        threading.Thread(target=wrap, daemon=True).start()

    def _error(self, message):
        dlg = Gtk.MessageDialog(transient_for=self.win, modal=True,
                                message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.OK, text=message)
        dlg.format_secondary_text("Operation cancelled — nothing was changed.")
        dlg.run()
        dlg.destroy()
        return False

    def _info(self, message):
        dlg = Gtk.MessageDialog(transient_for=self.win, modal=True,
                                message_type=Gtk.MessageType.INFO,
                                buttons=Gtk.ButtonsType.OK, text=message)
        dlg.run()
        dlg.destroy()
        return False

    def _confirm(self, message):
        dlg = Gtk.MessageDialog(transient_for=self.win, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.OK_CANCEL,
                                text=message)
        answer = dlg.run()
        dlg.destroy()
        return answer == Gtk.ResponseType.OK

    # ---- Setup tab --------------------------------------------------
    def _setup_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      border_width=10)
        hint = Gtk.Label(label=SETUP_HINT)
        hint.set_line_wrap(True)
        hint.set_xalign(0)
        hint.set_halign(Gtk.Align.START)
        box.pack_start(hint, False, False, 0)

        table = Gtk.Grid(column_spacing=10, row_spacing=8, border_width=4)
        table.attach(Gtk.Label(label="Client ID:", halign=Gtk.Align.END),
                     0, 0, 1, 1)
        self.ent_client_id = Gtk.Entry(hexpand=True)
        self.ent_client_id.set_text(self.cfg.get("client_id", ""))
        table.attach(self.ent_client_id, 1, 0, 1, 1)

        table.attach(Gtk.Label(label="Client secret:", halign=Gtk.Align.END),
                     0, 1, 1, 1)
        sec_row = Gtk.Box(spacing=6)
        self.ent_client_secret = Gtk.Entry(hexpand=True)
        self.ent_client_secret.set_visibility(False)
        self.ent_client_secret.set_text(self.cfg.get("client_secret", ""))
        sec_row.pack_start(self.ent_client_secret, True, True, 0)
        show = Gtk.ToggleButton(label="show")
        show.connect("toggled", self._toggle_secret)
        sec_row.pack_start(show, False, False, 0)
        table.attach(sec_row, 1, 1, 1, 1)

        save = Gtk.Button(label="Save")
        save.connect("clicked", self._on_save_setup)
        table.attach(save, 1, 2, 1, 1)
        self.setup_status = Gtk.Label(xalign=0)
        table.attach(self.setup_status, 1, 3, 1, 1)
        box.pack_start(table, False, False, 0)
        self._refresh_setup_status()
        return box

    def _toggle_secret(self, btn):
        self.ent_client_secret.set_visibility(btn.get_active())

    def _on_save_setup(self, _btn):
        cid = self.ent_client_id.get_text().strip()
        csec = self.ent_client_secret.get_text().strip()
        if not cid or not csec:
            self._error("Both client ID and client secret are required.")
            return
        try:
            self.cfg = update_config(client_id=cid, client_secret=csec)
        except Exception as exc:
            self._error(str(exc))
            return
        self._refresh_setup_status()
        self._refresh_accounts()
        self.btn_signin.set_sensitive(True)
        if not self.store.accounts():
            self._info("Client saved. Next: the Accounts tab \u2192 "
                       "\u201cSign in with Google\u2026\u201d.")
            self.nb.set_current_page(1)

    def _refresh_setup_status(self):
        n = len(self.store.accounts())
        ok = bool(self.cfg.get("client_id"))
        self.setup_status.set_text(
            f"OAuth client: {'configured' if ok else 'not set'} \u2014 "
            f"accounts authorized: {n} \u2014 data dir: {app_dir()}")

    # ---- Accounts tab -----------------------------------------------
    def _accounts_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      border_width=10)
        self.acc_store, self.acc_view, scroll = _list_view(
            ["#", "Account"], (int, str))
        box.pack_start(scroll, True, True, 0)

        row = Gtk.Box(spacing=6)
        self.btn_signin = Gtk.Button(label="Sign in with Google\u2026")
        self.btn_signin.connect("clicked", self._on_signin)
        row.pack_start(self.btn_signin, False, False, 0)
        btn_rm = Gtk.Button(label="Remove selected")
        btn_rm.connect("clicked", self._on_remove_account)
        row.pack_start(btn_rm, False, False, 0)
        box.pack_start(row, False, False, 0)

        note = Gtk.Label(
            label="Google's consent page opens in your browser once per "
                  "account — that step must happen on google.com. "
                  "Tokens are stored locally and refreshed automatically.")
        note.set_line_wrap(True)
        note.set_xalign(0)
        note.set_halign(Gtk.Align.START)
        box.pack_start(note, False, False, 0)

        self._refresh_accounts()
        return box

    def _on_signin(self, _btn):
        self.btn_signin.set_sensitive(False)
        self.win.set_title(f"DriveSync {__version__} — waiting for Google…")
        self._bg(self._auth_and_store, self._signin_done)

    def _auth_and_store(self):
        cfg = load_config()  # fresh read: creds may have just been saved
        tokens = interactive_auth(cfg)
        email = whoami(tokens["access_token"]).get("emailAddress", "unknown")
        row = self.store.add_account(email, tokens)
        return email, row["id"]

    def _signin_done(self, result):
        email, acc_id = result
        self.win.set_title(f"DriveSync {__version__}")
        self.btn_signin.set_sensitive(True)
        self._refresh_accounts()
        self._refresh_setup_status()
        self._info(f"Authorized {email} (account #{acc_id}).\n"
                   "Now add folders on the Folders tab.")
        self.nb.set_current_page(2)

    def _on_remove_account(self, _btn):
        sel = self.acc_view.get_selection().get_selected()
        if sel[1] is None:
            return
        acc_id = self.acc_store[sel[1]][0]
        if self._confirm(f"Remove account #{acc_id}? Stored tokens are "
                         "deleted; sync pairs using it are kept but will "
                         "pause until re-authorized."):
            self.store.delete_account(acc_id)
            self._refresh_accounts()
            self._refresh_setup_status()

    def _refresh_accounts(self):
        self.acc_store.clear()
        for a in self.store.accounts():
            self.acc_store.append([a["id"], a["email"]])

    # ---- Folders tab --------------------------------------------------
    def _folders_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      border_width=10)
        self.pair_store, self.pair_view, scroll = _list_view(
            ["#", "Direction", "Local folder", "Drive folder", "State"],
            (int, str, str, str, str))
        box.pack_start(scroll, True, True, 0)

        row = Gtk.Box(spacing=6)
        btn_add = Gtk.Button(label="Add folder\u2026")
        btn_add.connect("clicked", self._on_add_pair)
        row.pack_start(btn_add, False, False, 0)
        btn_toggle = Gtk.Button(label="Pause / resume")
        btn_toggle.connect("clicked", self._on_toggle_pair)
        row.pack_start(btn_toggle, False, False, 0)
        btn_sync = Gtk.Button(label="Sync selected now")
        btn_sync.connect("clicked", self._on_sync_pair)
        row.pack_start(btn_sync, False, False, 0)
        btn_rm = Gtk.Button(label="Remove")
        btn_rm.connect("clicked", self._on_remove_pair)
        row.pack_start(btn_rm, False, False, 0)
        box.pack_start(row, False, False, 0)

        self._refresh_pairs()
        return box

    def _on_add_pair(self, _btn):
        accounts = self.store.accounts()
        if not accounts:
            self._error("No account yet — authorize one on the Accounts tab "
                        "first.")
            return
        dlg = Gtk.Dialog(title="Add sync folder", transient_for=self.win,
                         modal=True)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OK, Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=8, row_spacing=8, border_width=10)
        grid.attach(Gtk.Label(label="Local folder:", halign=Gtk.Align.END),
                    0, 0, 1, 1)
        chooser = Gtk.FileChooserButton(title="Pick a folder",
                                        action=Gtk.FileChooserAction.SELECT_FOLDER)
        grid.attach(chooser, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Google account:", halign=Gtk.Align.END),
                    0, 1, 1, 1)
        acc_combo = Gtk.ComboBoxText()
        for a in accounts:
            acc_combo.append_text(f"#{a['id']}  {a['email']}")
        acc_combo.set_active(0)
        grid.attach(acc_combo, 1, 1, 1, 1)
        grid.attach(Gtk.Label(label="Drive folder ID:", halign=Gtk.Align.END),
                    0, 2, 1, 1)
        ent_remote = Gtk.Entry(hexpand=True)
        ent_remote.set_placeholder_text("from the folder URL: …/folders/<ID>")
        grid.attach(ent_remote, 1, 2, 1, 1)
        grid.attach(Gtk.Label(label="Display name:", halign=Gtk.Align.END),
                    0, 3, 1, 1)
        ent_path = Gtk.Entry()
        ent_path.set_placeholder_text("optional, e.g. My Drive/projects")
        grid.attach(ent_path, 1, 3, 1, 1)
        grid.attach(Gtk.Label(label="Direction:", halign=Gtk.Align.END),
                    0, 4, 1, 1)
        dir_combo = Gtk.ComboBoxText()
        for d in DIRECTIONS:
            dir_combo.append_text(d)
        dir_combo.set_active(0)
        grid.attach(dir_combo, 1, 4, 1, 1)
        grid.attach(Gtk.Label(label="Conflicts:", halign=Gtk.Align.END),
                    0, 5, 1, 1)
        pol_combo = Gtk.ComboBoxText()
        for p in POLICIES:
            pol_combo.append_text(p)
        pol_combo.set_active(0)
        grid.attach(pol_combo, 1, 5, 1, 1)
        dlg.get_content_area().pack_start(grid, True, True, 0)
        dlg.show_all()

        if dlg.run() == Gtk.ResponseType.OK:
            local = chooser.get_filename()
            remote_id = ent_remote.get_text().strip()
            try:
                if not local:
                    raise ValueError("pick a local folder first")
                if not remote_id:
                    raise ValueError("the Drive folder ID is required "
                                     "(…/folders/<ID> in its web URL)")
                acc_ids = [a["id"] for a in accounts]
                pair = self.store.add_pair(
                    acc_ids[acc_combo.get_active()], local, remote_id,
                    remote_path=ent_path.get_text().strip(),
                    direction=DIRECTIONS[dir_combo.get_active()],
                    conflict_policy=POLICIES[pol_combo.get_active()])
                self._refresh_pairs()
                self._info(f"Pair #{pair['id']} added. First sync runs "
                           f"within ~{self.cfg['sync_interval']}s, or press "
                           "\u201cSync selected now\u201d.")
            except Exception as exc:
                self._error(str(exc))
        dlg.destroy()

    def _selected_pair(self):
        sel = self.pair_view.get_selection().get_selected()
        if sel[1] is None:
            return None
        return self.pair_store[sel[1]][0]

    def _on_toggle_pair(self, _btn):
        pair_id = self._selected_pair()
        if pair_id is None:
            return
        for p in self.store.pairs():
            if p["id"] == pair_id:
                self.store.update_pair(pair_id, enabled=0 if p["enabled"] else 1)
                break
        self._refresh_pairs()

    def _on_sync_pair(self, _btn):
        pair_id = self._selected_pair()
        if pair_id is None:
            return
        self._bg(lambda: self.worker.run_pass(pair_id), self._refresh_activity)

    def _on_remove_pair(self, _btn):
        pair_id = self._selected_pair()
        if pair_id is None:
            return
        if self._confirm(f"Remove pair #{pair_id}? Local files and Drive "
                         "contents are kept — only the sync link is deleted."):
            self.store.delete_pair(pair_id)
            self._refresh_pairs()

    def _refresh_pairs(self):
        self.pair_store.clear()
        for p in self.store.pairs():
            remote = p["remote_path"] or p["remote_id"]
            state = "active" if p["enabled"] else "paused"
            self.pair_store.append([p["id"], p["direction"], p["local_path"],
                                    remote, state])

    # ---- Activity tab -------------------------------------------------
    def _activity_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      border_width=10)
        row = Gtk.Box(spacing=6)
        self.lbl_status = Gtk.Label(xalign=0)
        row.pack_start(self.lbl_status, True, True, 0)
        btn = Gtk.Button(label="Sync all now")
        btn.connect("clicked", self._on_sync_all)
        row.pack_start(btn, False, False, 0)
        row.pack_start(Gtk.Label(label="Interval (s):"), False, False, 0)
        self.spin = Gtk.SpinButton.new_with_range(5, 86400, 5)
        self.spin.set_value(self.cfg.get("sync_interval", 30))
        row.pack_start(self.spin, False, False, 0)
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", self._on_apply_interval)
        row.pack_start(apply_btn, False, False, 0)
        box.pack_start(row, False, False, 0)

        self.evt_store, self.evt_view, scroll = _list_view(
            ["Time", "Level", "Action", "Path", "Detail"],
            (str, str, str, str, str))
        box.pack_start(scroll, True, True, 0)

        self._refresh_activity()
        return box

    def _on_sync_all(self, _btn):
        self._bg(lambda: self.worker.run_pass(), self._refresh_activity)

    def _on_apply_interval(self, _btn):
        try:
            self.cfg = update_config(sync_interval=int(self.spin.get_value()))
        except (ValueError, TypeError) as exc:
            self._error(str(exc))
            return
        self.worker.stop()
        self.worker = Worker(self.store, self.cfg)
        self.worker.start()
        self._info(f"Sync interval set to {self.cfg['sync_interval']}s.")

    def _refresh_activity(self, *_args):
        s = self.worker.status
        last = "never" if not s["last_run"] else \
            f"{int(max(0, __import__('time').time() - s['last_run']))}s ago"
        stats = ""
        if s["last_stats"]:
            st = s["last_stats"]
            stats = (f" — last pass pair #{st['pair_id']}: pushed {st['pushed']},"
                     f" pulled {st['pulled']}, deleted {st['deleted']},"
                     f" conflicts {st['conflicts']}")
        err = f" — ERROR: {s['last_error']}" if s["last_error"] else ""
        self.lbl_status.set_text(
            f"Worker {'paused' if s['paused'] else 'running'} \u2014 "
            f"last pass: {last}{stats}{err}")
        self.evt_store.clear()
        for e in reversed(self.store.recent_events(limit=200)):
            self.evt_store.append([e["ts"], e["level"], e["action"],
                                   e["rel_path"], e["message"]])
        return False

    def _tick(self):
        """Periodic UI refresh while the window is open."""
        if self._closing:
            return False
        self._refresh_activity()
        return True

    # ---- lifecycle ----------------------------------------------------
    def _on_close(self, _win):
        self._closing = True
        try:
            self.worker.stop()
            self.store.close()
        except Exception:
            pass
        Gtk.main_quit()

    def show(self):
        self.win.show_all()


def run():
    DesktopApp().show()
    Gtk.main()
