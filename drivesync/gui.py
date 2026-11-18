"""Native GTK3 desktop UI for DriveSync (PyGObject) — Material 3 styled.

A local window covering everything a fresh install needs: first-run setup
(your own Google OAuth client), account sign-in, sync pair management and
live activity — no browser dashboard required. The app is a resident
desktop citizen: a system tray icon (when pystray is installed) keeps it
alive with the window closed; the tray reopens the window, triggers syncs
and quits. Without a tray, closing the window exits the app.

One step still opens a browser on purpose: Google's consent page during
account authorization (installed-app OAuth flow, loopback redirect on
127.0.0.1:auth_port). Everything else happens inside this window.

Requires python3-gobject (RPM) / python3-gi (deb). Import is lazy —
`drivesync serve` and the CLI never touch this module, so headless
machines without GTK are unaffected.
"""

import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import gi

gi.require_version("Gtk", "3.0")
# Gdk must be pinned BEFORE it is imported: unpinned, PyGObject auto-loads
# the newest typelib (Gdk 4.0) and Gtk 3.0's Gdk 3.0 dependency then fails
# with "Requiring namespace 'Gdk' version '3.0', but '4.0' is already loaded".
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import __version__  # noqa: E402
from .auth import access_token, interactive_auth, token_from_json, whoami  # noqa: E402
from .config import app_dir, load_config, update_config  # noqa: E402
from .db import Store, to_local  # noqa: E402
from .drive import Drive, folders_for_parent  # noqa: E402
from .tray import _icon_image  # noqa: E402
from .worker import Worker  # noqa: E402

DIRECTIONS = ("bidirectional", "upload", "download")
POLICIES = ("newer", "local", "remote", "both")

# Activity tab time-frame presets: combo index → seconds back from now.
_TIMEFRAME_SECS = {1: 15 * 60, 2: 3600, 3: 24 * 3600, 4: 7 * 24 * 3600}


def _activity_row(e):
    """One activity-list row. The hidden 6th column carries the raw UTC ts
    so a selected row can be re-found after each refresh repaint. Every
    value is coerced to str: the TreeView model declares str-only columns,
    and sqlite can hand back NULL for rel_path/message — a non-str value
    makes the ListStore append raise TypeError (this once crashed startup:
    the hidden column was declared float while the raw ts is a string)."""
    return [to_local(e["ts"]), e["level"] or "info", e["action"],
            e["rel_path"] or "", e["message"] or "", e["ts"]]

SETUP_HINT = (
    "DriveSync talks to Google with your own OAuth \u201cDesktop app\u201d client, so "
    "your tokens never pass through a third-party server. Create one once:\n"
    "  1. console.cloud.google.com \u2192 APIs & Services \u2192 enable the Drive API\n"
    "  2. OAuth consent screen \u2192 External \u2192 add yourself as a test user\n"
    "  3. Credentials \u2192 Create credentials \u2192 OAuth client ID \u2192 Desktop app\n"
    "  4. Paste the client ID and secret below."
)

TRAY_HINT = ("Closing this window keeps DriveSync running in the system tray "
             "(open it again from the tray icon).")

# Material 3 light palette, expressed as GTK3 CSS. Pill-shaped controls,
# rounded cards and tonal tabs — the app keeps a consistent look on any
# system theme (light preference is forced below).
MD3_CSS = """
window { background-color: #F8FAFD; }
headerbar { background-color: #E9EEF6; border: none; box-shadow: none; }
/* .title/.subtitle are style classes on the headerbar's labels, not
   pseudo-classes: GTK3's parser aborts on `headerbar:subtitle`
   ("Invalid name of pseudo-class") and the window never opens */
headerbar .title { font-weight: 700; font-size: 105%; }
headerbar .subtitle { color: #444746; font-size: 88%; }
.card {
  background-color: #FFFFFF; border-radius: 16px;
  border: 1px solid #E1E8EF; padding: 18px;
}
button {
  border-radius: 100px; padding: 6px 20px;
  background-color: #E9EEF6; color: #0B57D0;
  border: none; text-shadow: none; box-shadow: none;
}
button:hover { background-color: #DCE5F2; }
button:active { background-color: #CFDAEA; }
button.suggested-action { background-color: #0B57D0; color: #FFFFFF; }
button.suggested-action:hover { background-color: #0948AE; }
button.destructive-action { background-color: #B3261E; color: #FFFFFF; }
button.destructive-action:hover { background-color: #991F18; }
entry {
  border-radius: 12px; padding: 8px 12px;
  background-color: #FFFFFF; border: 1px solid #C4C7C5; box-shadow: none;
}
entry:focus { border: 1px solid #0B57D0; }
combobox button.combo {
  border-radius: 12px; padding: 6px 12px;
  background-color: #FFFFFF; border: 1px solid #C4C7C5; box-shadow: none;
}
notebook header { background: transparent; border: none; box-shadow: none; }
notebook header tabs { background: transparent; }
notebook tab {
  border-radius: 100px; padding: 4px 18px; margin: 0 4px;
  background-color: transparent; color: #444746;
  border: none; box-shadow: none; font-weight: 500;
}
notebook tab:hover { background-color: #E9EEF6; }
notebook tab:checked { background-color: #C2E7FF; color: #001D35; }
notebook stack { background: transparent; border: none; }
treeview { background-color: #FFFFFF; }
treeview:hover { background-color: #F0F4F9; }
treeview header button {
  background-color: #F0F4F9; border: none; box-shadow: none;
  border-radius: 10px; padding: 4px 8px;
  font-weight: 600; color: #444746; text-shadow: none;
}
scrolledwindow { border: none; }
label.dim { color: #5E5E5E; font-size: 92%; }
label.hint { color: #444746; font-size: 95%; }
"""


def _apply_css():
    Gtk.Settings.get_default().set_property(
        "gtk-application-prefer-dark-theme", False)
    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(MD3_CSS.encode("utf-8"))
    except GLib.GError as exc:
        # a rule rejected by some parser/theme build must not keep the
        # app from launching — fall back to the system theme styling
        print(f"warning: custom CSS rejected ({exc}); using theme defaults",
              file=sys.stderr)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


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


def _card(child):
    """Wrap a tab's content in a Material 3 elevated card."""
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    card.get_style_context().add_class("card")
    card.pack_start(child, True, True, 0)
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                    spacing=8, border_width=12)
    outer.pack_start(card, True, True, 0)
    return outer


class DesktopApp:
    """Main window: Setup / Accounts / Folders / Activity + resident tray."""

    def __init__(self):
        self.store = Store(app_dir() / "state.db")
        self.cfg = load_config()
        self.worker = Worker(self.store, self.cfg)
        self.worker.start()
        self._closing = False
        self.tray = None

        self.win = Gtk.Window(title=f"DriveSync {__version__}")
        self.win.set_default_size(860, 600)
        self.win.set_position(Gtk.WindowPosition.CENTER)

        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title(f"DriveSync {__version__}")
        hb.set_subtitle("starting…")
        self.hb = hb
        self.win.set_titlebar(hb)

        self.win.connect("destroy", self._on_close)
        self.win.connect("delete-event", self._on_delete)

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
        self.tray = self._start_tray()
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

    def _dim_label(self, text):
        lbl = Gtk.Label(label=text)
        lbl.get_style_context().add_class("dim")
        lbl.set_line_wrap(True)
        lbl.set_xalign(0)
        lbl.set_halign(Gtk.Align.START)
        return lbl

    # ---- system tray (resident mode) -------------------------------
    def _start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw  # noqa: F401
        except Exception:
            return None

        def idle(fn):
            def inner(*args, **kwargs):
                GLib.idle_add(fn, *args, **kwargs)
            return inner

        def pause_label(_item):
            try:
                paused = self.worker.status.get("paused")
            except Exception:
                paused = False
            return ("Resume background sync" if paused
                    else "Pause background sync")

        menu = pystray.Menu(
            pystray.MenuItem("Open DriveSync", idle(self._tray_open),
                             default=True),
            pystray.MenuItem("Sync all now", idle(self._tray_sync)),
            pystray.MenuItem(pause_label, idle(self._tray_toggle)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit DriveSync", idle(self._tray_quit)),
        )
        try:
            icon = pystray.Icon("DriveSync",
                                _icon_image(Image, ImageDraw),
                                "DriveSync — local Drive sync", menu)
            if hasattr(icon, "run_detached") and icon.run_detached():
                return icon
            threading.Thread(target=icon.run, daemon=True,
                             name="drivesync-tray").start()
            return icon
        except Exception:
            return None

    def _tray_open(self):
        self.win.deiconify()
        self.win.show_all()
        self.win.present()

    def _tray_sync(self):
        self.worker.sync_now()

    def _tray_toggle(self):
        if self.worker.status.get("paused"):
            self.worker.resume()
        else:
            self.worker.pause()

    def _tray_quit(self):
        self._quit()

    # ---- Setup tab --------------------------------------------------
    def _setup_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      border_width=6)
        hint = self._dim_label(SETUP_HINT)
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

        save = Gtk.Button(label="Save client")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", self._on_save_setup)
        table.attach(save, 1, 2, 1, 1)
        self.setup_status = Gtk.Label(xalign=0)
        self.setup_status.get_style_context().add_class("hint")
        table.attach(self.setup_status, 1, 3, 1, 1)
        box.pack_start(table, False, False, 0)
        box.pack_start(self._dim_label(TRAY_HINT), False, False, 0)
        self._refresh_setup_status()
        return _card(box)

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
                      border_width=6)
        self.acc_store, self.acc_view, scroll = _list_view(
            ["#", "Account"], (int, str))
        box.pack_start(scroll, True, True, 0)

        row = Gtk.Box(spacing=6)
        self.btn_signin = Gtk.Button(label="Sign in with Google\u2026")
        self.btn_signin.get_style_context().add_class("suggested-action")
        self.btn_signin.connect("clicked", self._on_signin)
        row.pack_start(self.btn_signin, False, False, 0)
        btn_rm = Gtk.Button(label="Remove selected")
        btn_rm.get_style_context().add_class("destructive-action")
        btn_rm.connect("clicked", self._on_remove_account)
        row.pack_start(btn_rm, False, False, 0)
        box.pack_start(row, False, False, 0)

        note = self._dim_label(
            "Google's consent page opens in your browser once per account — "
            "that step must happen on google.com. Tokens are stored locally "
            "and refreshed automatically.")
        box.pack_start(note, False, False, 0)

        self._refresh_accounts()
        return _card(box)

    def _on_signin(self, _btn):
        self.btn_signin.set_sensitive(False)
        self.hb.set_subtitle("waiting for Google…")
        self._bg(self._auth_and_store, self._signin_done)

    def _auth_and_store(self):
        cfg = load_config()  # fresh read: creds may have just been saved
        tokens = interactive_auth(cfg)
        email = whoami(tokens["access_token"]).get("emailAddress", "unknown")
        row = self.store.add_account(email, tokens)
        return email, row["id"]

    def _signin_done(self, result):
        email, acc_id = result
        self.hb.set_subtitle("")
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
                      border_width=6)
        self.pair_store, self.pair_view, scroll = _list_view(
            ["#", "Direction", "Local folder", "Drive folder", "State"],
            (int, str, str, str, str))
        box.pack_start(scroll, True, True, 0)

        row = Gtk.Box(spacing=6)
        btn_add = Gtk.Button(label="Add folder\u2026")
        btn_add.get_style_context().add_class("suggested-action")
        btn_add.connect("clicked", self._on_add_pair)
        row.pack_start(btn_add, False, False, 0)
        btn_toggle = Gtk.Button(label="Pause / resume")
        btn_toggle.connect("clicked", self._on_toggle_pair)
        row.pack_start(btn_toggle, False, False, 0)
        btn_sync = Gtk.Button(label="Sync selected now")
        btn_sync.connect("clicked", self._on_sync_pair)
        row.pack_start(btn_sync, False, False, 0)
        btn_rm = Gtk.Button(label="Remove")
        btn_rm.get_style_context().add_class("destructive-action")
        btn_rm.connect("clicked", self._on_remove_pair)
        row.pack_start(btn_rm, False, False, 0)
        box.pack_start(row, False, False, 0)

        self._refresh_pairs()
        return _card(box)

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
        grid.attach(Gtk.Label(label="Drive folder:", halign=Gtk.Align.END),
                    0, 2, 1, 1)
        remote_row = Gtk.Box(spacing=6)
        ent_remote = Gtk.Entry(hexpand=True)
        ent_remote.set_placeholder_text("pick with Browse\u2026, or the "
                                        "\u2026/folders/<ID> from its URL")
        remote_row.pack_start(ent_remote, True, True, 0)
        btn_browse = Gtk.Button(label="Browse Drive\u2026")
        remote_row.pack_start(btn_browse, False, False, 0)
        grid.attach(remote_row, 1, 2, 1, 1)
        grid.attach(Gtk.Label(label="Display name:", halign=Gtk.Align.END),
                    0, 3, 1, 1)
        ent_path = Gtk.Entry()
        ent_path.set_placeholder_text("optional, e.g. My Drive/projects")
        grid.attach(ent_path, 1, 3, 1, 1)
        btn_browse.connect("clicked", self._on_browse_drive,
                           acc_combo, ent_remote, ent_path)
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

    def _drive_for(self, acc_id):
        """Drive client bound to the account's stored tokens (as serve does)."""
        account = self.store.account(acc_id)
        if not account:
            raise ValueError(f"account #{acc_id} not found")
        token = token_from_json(account["token_json"])
        return Drive(lambda: access_token(token))

    def _on_browse_drive(self, _btn, acc_combo, ent_remote, ent_path):
        accounts = self.store.accounts()
        if not accounts:
            self._error("No account yet \u2014 authorize one on the Accounts "
                        "tab first.")
            return
        idx = acc_combo.get_active()
        acc_id = accounts[idx if idx >= 0 else 0]["id"]
        self._browse_folders(acc_id, ent_remote, ent_path,
                             "root", "My Drive", "My Drive")

    def _browse_folders(self, acc_id, ent_remote, ent_path,
                        parent_id, parent_name, path):
        """Navigable Drive folder picker — the same flow as the dashboard.

        My Drive (plus a virtual 'Shared with me' entry) \u2192 double-click
        a row to descend, 'Up one level' climbs back, 'Use this folder'
        binds the folder shown above to the pair form.
        """
        dlg = Gtk.Dialog(title="Choose a Drive folder",
                         transient_for=self.win, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                        "Use this folder", Gtk.ResponseType.OK)
        dlg.set_default_size(540, 420)
        state = {"stack": [(parent_id, parent_name, path)],
                 "loading": False, "open": True}

        box = dlg.get_content_area()
        crumb = self._dim_label(path)
        box.pack_start(crumb, False, False, 4)
        sel_store, sel_view, scroll = _list_view(["Folder", "ID"], (str, str))
        box.pack_start(scroll, True, True, 0)
        btn_up = Gtk.Button(label="Up one level")
        btn_up.set_halign(Gtk.Align.START)
        box.pack_start(btn_up, False, False, 4)
        box.pack_start(self._dim_label(
            "Double-click a folder to open it \u2014 \u201cUse this "
            "folder\u201d binds the one shown above."), False, False, 4)

        def load():
            st = state["stack"][-1]
            crumb.set_text(st[2])
            btn_up.set_sensitive(len(state["stack"]) > 1)
            sel_store.clear()
            state["loading"] = True

            def work():
                try:
                    return folders_for_parent(self._drive_for(acc_id), st[0])
                except Exception as exc:
                    return {"error": str(exc)}

            def done(res):
                state["loading"] = False
                if not state["open"]:
                    return
                if isinstance(res, dict):    # error marker from work()
                    self._error(f"Drive listing failed \u2014 "
                                f"{res.get('error', '?')}")
                    return
                rows = ([{"id": "shared", "name": "Shared with me"}]
                        if st[0] == "root" else [])
                rows.extend(res)
                for f in rows:
                    sel_store.append([f["name"], f["id"]])

            self._bg(work, done)

        def on_activate(_view, treepath, _col):
            if state["loading"]:
                return
            name, fid = sel_store[treepath][0], sel_store[treepath][1]
            st = state["stack"][-1]
            if fid == "shared":
                state["stack"].append(("shared", "Shared with me",
                                       "Shared with me"))
            else:
                state["stack"].append((fid, name, f"{st[2]}/{name}"))
            load()

        sel_view.connect("row-activated", on_activate)

        def on_up(_btn):
            if state["loading"] or len(state["stack"]) < 2:
                return
            state["stack"].pop()
            load()

        btn_up.connect("clicked", on_up)

        dlg.show_all()
        load()
        if dlg.run() == Gtk.ResponseType.OK:
            fid, _name, fpath = state["stack"][-1]
            ent_remote.set_text(fid)
            if not ent_path.get_text().strip():
                ent_path.set_text(fpath)
        state["open"] = False
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
                self.store.update_pair(pair_id,
                                       enabled=0 if p["enabled"] else 1)
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
                      border_width=6)
        row = Gtk.Box(spacing=6)
        self.lbl_status = Gtk.Label(xalign=0)
        self.lbl_status.get_style_context().add_class("hint")
        row.pack_start(self.lbl_status, True, True, 0)
        btn = Gtk.Button(label="Sync all now")
        btn.get_style_context().add_class("suggested-action")
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

        # filter / sort bar — combos re-list instantly; Clear wipes the log
        frow = Gtk.Box(spacing=6)
        self.cmb_action = Gtk.ComboBoxText()
        self.cmb_action.append_text("All actions")
        self.cmb_action.set_active(0)
        self.cmb_action.set_tooltip_text("Filter by action")
        self.cmb_action.connect("changed", self._on_activity_filter)
        frow.pack_start(self.cmb_action, False, False, 0)
        self.cmb_level = Gtk.ComboBoxText()
        for lv in ("All levels", "info", "conflict", "error"):
            self.cmb_level.append_text(lv)
        self.cmb_level.set_active(0)
        self.cmb_level.set_tooltip_text("Filter by level")
        self.cmb_level.connect("changed", self._on_activity_filter)
        frow.pack_start(self.cmb_level, False, False, 0)
        self.cmb_time = Gtk.ComboBoxText()
        for tf in ("Any time", "Last 15 minutes", "Last hour",
                   "Last 24 hours", "Last 7 days"):
            self.cmb_time.append_text(tf)
        self.cmb_time.set_active(0)
        self.cmb_time.set_tooltip_text("Time frame")
        self.cmb_time.connect("changed", self._on_activity_filter)
        frow.pack_start(self.cmb_time, False, False, 0)
        self.cmb_sort = Gtk.ComboBoxText()
        self.cmb_sort.append_text("Newest first")
        self.cmb_sort.append_text("Oldest first")
        self.cmb_sort.set_active(0)
        self.cmb_sort.set_tooltip_text("Sort order")
        self.cmb_sort.connect("changed", self._on_activity_filter)
        frow.pack_start(self.cmb_sort, False, False, 0)
        btn_clear = Gtk.Button(label="Clear activity\u2026")
        btn_clear.get_style_context().add_class("destructive-action")
        btn_clear.connect("clicked", self._on_clear_activity)
        frow.pack_end(btn_clear, False, False, 0)
        box.pack_start(frow, False, False, 0)

        # the 6th (hidden) store column keeps the raw UTC ts so the row the
        # user is reading can be re-found after each refresh repaint
        self.evt_store, self.evt_view, self.evt_scroll = _list_view(
            ["Time", "Level", "Action", "Path", "Detail"],
            (str, str, str, str, str, str))
        box.pack_start(self.evt_scroll, True, True, 0)
        self._action_items = ["All actions"]

        self._refresh_activity()
        return _card(box)

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
            f"{int(max(0, time.time() - s['last_run']))}s ago"
        stats = ""
        if s["last_stats"]:
            st = s["last_stats"]
            stats = (f" — last pass pair #{st['pair_id']}: pushed {st['pushed']},"
                     f" pulled {st['pulled']}, deleted {st['deleted']},"
                     f" conflicts {st['conflicts']}")
        err = f" — ERROR: {s['last_error']}" if s["last_error"] else ""
        state = "paused" if s["paused"] else "running"
        self.lbl_status.set_text(
            f"Worker {state} \u2014 last pass: {last}{stats}{err}")
        self.hb.set_subtitle(f"sync {state} \u2014 last pass {last}")

        # apply the filter bar (rebuilding the action combo may nest here;
        # see _sync_action_combo — the guard stops signal ping-pong)
        self._sync_action_combo()
        action = self.cmb_action.get_active_text()
        level = self.cmb_level.get_active_text()
        idx = self.cmb_time.get_active()
        since = None
        if idx in _TIMEFRAME_SECS:
            since = datetime.now(timezone.utc) - \
                timedelta(seconds=_TIMEFRAME_SECS[idx])
        events = self.store.recent_events(
            limit=200,
            level=None if level in (None, "All levels") else level,
            action=None if action in (None, "All actions") else action,
            since=since)
        if self.cmb_sort.get_active() == 1:
            events = list(reversed(events))        # oldest at the top

        # repaint without yanking the view: remember the row the user is
        # reading (raw ts + action + path) and the scroll offset, restore
        # both after the refill — a periodic tick must not jump to the top
        sel = self.evt_view.get_selection()
        _model, sel_path = sel.get_selected()
        sel_key = None
        if sel_path is not None:
            sel_key = (self.evt_store[sel_path][5],     # raw ts
                       self.evt_store[sel_path][2],     # action
                       self.evt_store[sel_path][3])     # rel_path
        adj = self.evt_scroll.get_vadjustment()
        offset = adj.get_value()

        self.evt_store.clear()
        for e in events:
            self.evt_store.append(_activity_row(e))
        if sel_key:
            for i, row in enumerate(self.evt_store):
                if (row[5], row[2], row[3]) == sel_key:
                    path = Gtk.TreePath.new_from_indices([i])
                    sel.select_path(path)
                    self.evt_view.scroll_to_cell(path, None, False, 0, 0)
                    break
        else:
            GLib.idle_add(self._restore_scroll, adj, offset)
        return False

    @staticmethod
    def _restore_scroll(adj, offset):
        """Reapply the previous scroll offset after the rows have laid out."""
        upper = adj.get_upper() - adj.get_page_size()
        adj.set_value(min(max(offset, 0), max(0, upper)))
        return False

    def _sync_action_combo(self):
        """Keep the action filter's options in step with the log contents
        without resetting the user's choice (or recursing via 'changed')."""
        actions = self.store.event_actions()
        items = ["All actions"] + actions
        if items == getattr(self, "_action_items", None):
            return
        self._action_items = items          # set first: stops re-entry
        self._combo_quiet = True
        try:
            active = self.cmb_action.get_active_text()
            self.cmb_action.remove_all()
            for it in items:
                self.cmb_action.append_text(it)
            self.cmb_action.set_active(
                items.index(active) if active in items else 0)
        finally:
            self._combo_quiet = False

    def _on_activity_filter(self, _combo=None):
        if getattr(self, "_combo_quiet", False):
            return
        self._refresh_activity()

    def _on_clear_activity(self, _btn):
        n = self.store.count_events()
        if not n:
            self._info("The activity log is already empty.")
            return
        if self._confirm(f"Delete all {n} activity entries? "
                         "This cannot be undone."):
            self.store.clear_events()
            self._refresh_activity()

    def _tick(self):
        """Periodic UI refresh while the app is alive (even when hidden)."""
        if self._closing:
            return False
        self._refresh_activity()
        return True

    # ---- lifecycle ----------------------------------------------------
    def _on_delete(self, _win, _event):
        """Window close button: hide to tray when a tray exists."""
        if self.tray is not None:
            self.win.hide()
            return True            # swallow the event — app stays resident
        return False               # no tray: destroy -> _on_close exits

    def _quit(self):
        """Full exit (tray menu or no-tray close): stop everything."""
        if self._closing:
            return
        self._closing = True
        try:
            self.worker.stop()
        except Exception:
            pass
        try:
            if self.tray is not None:
                self.tray.stop()
        except Exception:
            pass
        try:
            self.store.close()
        except Exception:
            pass
        Gtk.main_quit()

    def _on_close(self, _win):
        self._quit()

    def show(self):
        self.win.show_all()


def _instance_lock():
    """Single-instance guard: flock held for the process lifetime.

    Returns the open lock file, or None when another desktop instance is
    already running (the systemd 'serve' service is unaffected — it uses
    the sync.lock pass guard instead).
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover
        return object()      # no locking available: allow to proceed
    try:
        app_dir().mkdir(parents=True, exist_ok=True)
        fh = open(app_dir() / "gui.lock", "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        return None


def _already_running_notice():
    """Small transient dialog so a second menu launch is not a no-op."""
    dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.INFO,
                            buttons=Gtk.ButtonsType.OK,
                            text="DriveSync is already running")
    dlg.format_secondary_text("Use the DriveSync tray icon to open the "
                              "window, sync or quit.")
    dlg.run()
    dlg.destroy()


def run():
    lock = _instance_lock()
    if lock is None:
        _apply_css()
        _already_running_notice()
        return
    _apply_css()
    DesktopApp().show()
    Gtk.main()
