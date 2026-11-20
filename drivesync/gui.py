"""Native GTK3 desktop UI for DriveSync (PyGObject) — Material 3 styled.

A local window covering everything a fresh install needs: first-run setup
(your own Google OAuth client), account sign-in, sync pair management with
selective sync (per-pair subfolder scopes), an on-demand Drive browser
(like a virtual drive without the mount: browse everything, download what
you need) and live activity — no browser dashboard required. The app is a
resident desktop citizen: a system tray icon (when pystray is installed)
keeps it alive with the window closed; the tray reopens the window,
triggers syncs and quits. Without a tray, closing the window exits the app.

One step still opens a browser on purpose: Google's consent page during
account authorization (installed-app OAuth flow, loopback redirect on
127.0.0.1:auth_port). Everything else happens inside this window.

Requires python3-gobject (RPM) / python3-gi (deb). Import is lazy —
`drivesync serve` and the CLI never touch this module, so headless
machines without GTK are unaffected.
"""

import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import gi

gi.require_version("Gtk", "3.0")
# Gdk must be pinned BEFORE it is imported: unpinned, PyGObject auto-loads
# the newest typelib (Gdk 4.0) and Gtk 3.0's Gdk 3.0 dependency then fails
# with "Requiring namespace 'Gdk' version '3.0', but '4.0' is already loaded".
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from . import autostart  # noqa: E402
from . import engine  # noqa: E402
from . import conflicts  # noqa: E402
from . import i18n  # noqa: E402
from . import instance  # noqa: E402
from . import notify  # noqa: E402
from . import themes  # noqa: E402

from . import __version__  # noqa: E402
from .auth import (access_token_persisting, interactive_auth,  # noqa: E402
                   token_from_json, whoami)
from .config import app_dir, load_config, update_config  # noqa: E402
from .db import Store, parse_include_patterns, to_local  # noqa: E402
from .drive import Drive, folders_for_parent, resolve_export_formats  # noqa: E402
from .tray import _icon_image  # noqa: E402
from .worker import Worker  # noqa: E402

DIRECTIONS = ("bidirectional", "upload", "download")
POLICIES = ("newer", "local", "remote", "both")

# Notebook pages, in append order (Overview, Setup, Accounts, Folders,
# Drive, Activity, Options)
PAGE_DRIVE = 4
PAGE_ACTIVITY = 5
PAGE_OPTIONS = 6

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


def _drive_bytes(n):
    """Byte count in human units ("" for unknown/zero-size rows)."""
    n = float(n or 0)
    if n <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _drive_row(item):
    """Drive listing row -> browser columns (+ hidden id/mime/size).

    Folder shortcuts resolve to their target so double-click descends to
    the real folder; Google-native docs show no size (the API has none).
    """
    name = item.get("name", "?")
    mime = item.get("mimeType", "")
    details = item.get("shortcutDetails") or {}
    modified = _drive_local_time(item.get("modifiedTime"))
    if details.get("targetMimeType") == "application/vnd.google-apps.folder":
        return [name, i18n.t("Folder"), "", modified,
                details.get("targetId", ""), mime, ""]
    if mime == "application/vnd.google-apps.folder":
        return [name, i18n.t("Folder"), "", modified,
                item.get("id", ""), mime, ""]
    if mime.startswith("application/vnd.google-apps."):
        return [name, i18n.t("Doc"), "", modified, item.get("id", ""), mime, ""]
    size = str(item.get("size") or "")
    return [name, i18n.t("File"), _drive_bytes(item.get("size")), modified,
            item.get("id", ""), mime, size]


def _drive_local_time(iso):
    """Drive modifiedTime ISO string -> local 'YYYY-MM-DD HH:MM' (or '')."""
    ts = engine.parse_remote_mtime(iso or "")
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

def _cfg_int(cfg, key, default):
    """Config value as an int for GTK widgets; junk degrades to default.

    load_config trusts config.json as-is, so a hand-edited "sync_interval":
    "30" (string) must not raise TypeError inside set_value() and kill the
    whole window at launch.
    """
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def startup_page(cfg, accounts):
    """Notebook page to open on launch: Overview / Setup / Accounts.

    Computed in __init__ but applied only AFTER the window is shown:
    GTK >= 3.24.49 silently ignores Gtk.Notebook.set_current_page() on a
    not-yet-shown window, which made every launch open on Setup.
    """
    if not cfg.get("client_id"):
        return 1
    if not accounts:
        return 2
    return 0


def _apply_css(theme="system", accent="blue"):
    """Push the configured theme + accent onto the GTK screen.

    All palettes and the CSS builder live in drivesync.themes; this shim
    keeps the historical call sites (run(), tests) stable.
    """
    themes.apply(theme, accent)


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


class _TrayHandle:
    """One stop() hook for whichever tray backend is active."""

    def __init__(self, stop):
        self._stop = stop

    def stop(self):
        try:
            self._stop()
        except Exception:
            pass


class DesktopApp:
    """Main window: Setup / Accounts / Folders / Drive / Activity / Options.

    The app version lives on the Options tab (and in the About card), not
    in the window title.
    """

    def __init__(self):
        self.store = Store(app_dir() / "state.db")
        self.cfg = load_config()
        self.worker = Worker(self.store, self.cfg, config_source=load_config)
        self.worker.start()
        self._closing = False
        self.tray = None
        self._listener = None
        # i18n registries: persistent widgets whose text follows the
        # language switch (see _retranslate)
        self._tr = []            # (widget, setter_name, string_key)
        self._tr_cols = []       # (treeview, [header keys])
        self._tr_rebuilders = [] # callables re-filling composite widgets
        self._pages = []         # (tab_label_widget, key)
        self._theme_guard = False

        # Give the window a stable WM_CLASS so the .desktop launcher (and
        # KDE's pinned icon) associates the running app with its entry even
        # while the window is hidden to the tray.
        GLib.set_prgname("drivesync")
        GLib.set_application_name("DriveSync")
        self.win = Gtk.Window(title="DriveSync")
        try:
            self.win.set_wmclass("drivesync", "DriveSync")
        except Exception:
            pass
        self.win.set_default_size(860, 600)
        self.win.set_position(Gtk.WindowPosition.CENTER)

        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title("DriveSync")
        hb.set_subtitle(i18n.t("starting…"))
        self.hb = hb
        self.win.set_titlebar(hb)

        # global pause lives in the headerbar so it is reachable from any
        # tab (it used to hide on the Activity tab only)
        self.btn_global_pause = self._tr_button("Pause sync")
        self.btn_global_pause.set_tooltip_text(
            i18n.t("Pause or resume background sync passes "
                   "(the window stays up)"))
        self.btn_global_pause.connect("clicked", self._on_pause_toggle)
        hb.pack_start(self.btn_global_pause)
        self.win.connect("destroy", self._on_close)
        self.win.connect("delete-event", self._on_delete)

        self.nb = Gtk.Notebook()
        self._add_page(self._overview_tab(), "Overview")
        self._add_page(self._setup_tab(), "Setup")
        self._add_page(self._accounts_tab(), "Accounts")
        self._add_page(self._folders_tab(), "Folders")
        self._add_page(self._drive_tab(), "Drive")
        self._add_page(self._activity_tab(), "Activity")
        self._add_page(self._options_tab(), "Options")

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                        spacing=6, border_width=10)
        outer.pack_start(self.nb, True, True, 0)
        self.win.add(outer)

        self._startup_page = startup_page(self.cfg, self.store.accounts())
        if self.cfg.get("tray_enabled", True):
            self.tray = self._start_tray()
        # A second launch (KDE's pinned icon while the window is hidden)
        # asks this process to show the window instead of exiting silently.
        self._listener = instance.ShowListener(
            lambda: GLib.idle_add(self._tray_open))
        if not self._listener.start():
            self._listener = None
        self._notify_gate = notify.Gate(lambda: self.win.get_visible())
        GLib.timeout_add_seconds(3, self._tick)

    # ---- i18n plumbing ----------------------------------------------
    def _add_page(self, widget, key):
        """Append a notebook page whose tab label follows the language."""
        lbl = Gtk.Label(label=i18n.t(key))
        self._pages.append((lbl, key))
        self.nb.append_page(widget, lbl)

    def _reg(self, widget, setter, key):
        """Register widget text (set via <setter>) for _retranslate()."""
        self._tr.append((widget, setter, key))
        return widget

    def _tr_button(self, key):
        """Gtk.Button whose label follows the language switch."""
        b = Gtk.Button()
        b.set_label(i18n.t(key))
        return self._reg(b, "set_label", key)

    def _tr_label(self, key, **kw):
        """Gtk.Label whose text follows the language switch."""
        lbl = Gtk.Label(label=i18n.t(key), **kw)
        return self._reg(lbl, "set_text", key)

    def _tr_dim(self, key):
        """Dim, wrapped hint label registered for retranslation."""
        lbl = self._dim_label(i18n.t(key))
        return self._reg(lbl, "set_text", key)

    def _tr_columns(self, view, keys):
        """Register TreeView column titles for retranslation."""
        self._tr_cols.append((view, keys))
        return view

    def _reg_rebuilder(self, fn):
        """Register a callable that re-fills a composite widget
        (combos with fixed items) after a language switch."""
        self._tr_rebuilders.append(fn)
        return fn

    def _retranslate(self):
        """Relabel every registered widget after a language switch."""
        for widget, setter, key in self._tr:
            try:
                getattr(widget, setter)(i18n.t(key))
            except Exception:
                pass
        for lbl, key in self._pages:
            lbl.set_text(i18n.t(key))
        for view, keys in self._tr_cols:
            for col, key in zip(view.get_columns(), keys):
                col.set_title(i18n.t(key))
        for fn in self._tr_rebuilders:
            try:
                fn()
            except Exception:
                pass
        self.win.set_title(i18n.t("DriveSync"))
        self.hb.set_title(i18n.t("DriveSync"))
        self._refresh_activity()
        self._sync_lang_button()

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
        dlg.format_secondary_text(i18n.t(
            "Operation cancelled — nothing was changed."))
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
        """Start a tray icon, or return None when none can work here.

        Prefer the native AppIndicator binding, which lives inside GTK's
        own main loop. pystray 0.17's GTK/AppIndicator backends call
        signal.signal() in their run loop, which raises when started from
        a worker thread and used to leave the window hidden with no icon
        to reopen it; only use pystray where that is safe.
        """
        indicator = self._start_appindicator()
        if indicator is not None:
            return indicator
        return self._start_pystray()

    def _start_appindicator(self):
        try:
            import gi
            ns = None
            for candidate in ("AyatanaAppIndicator3", "AppIndicator3"):
                try:
                    gi.require_version(candidate, "0.1")
                    ns = candidate
                    break
                except ValueError:
                    continue
            if ns is None:
                return None
            repo = __import__("gi.repository", fromlist=[ns])
            AppIndicator3 = getattr(repo, ns)
            icon_path = os.path.join(os.path.dirname(__file__), "static",
                                     "mark.svg")
            indicator = AppIndicator3.Indicator.new(
                "drivesync", icon_path,
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS)
            indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            indicator.set_title("DriveSync")
            menu = Gtk.Menu()
            self._tray_pause_item = None
            pause_label_text = i18n.t("Pause background sync")
            for label, callback in (
                    (i18n.t("Open DriveSync"), lambda _i: self._tray_open()),
                    (i18n.t("Sync all now"), lambda _i: self._tray_sync()),
                    (pause_label_text, lambda _i: self._tray_toggle())):
                item = Gtk.MenuItem(label=label)
                item.connect("activate", callback)
                menu.append(item)
                if label == pause_label_text:
                    self._tray_pause_item = item
            menu.append(Gtk.SeparatorMenuItem())
            item = Gtk.MenuItem(label=i18n.t("Quit DriveSync"))
            item.connect("activate", lambda _i: self._tray_quit())
            menu.append(item)
            menu.show_all()
            indicator.set_menu(menu)
            return _TrayHandle(lambda: indicator.set_status(
                AppIndicator3.IndicatorStatus.PASSIVE))
        except Exception:
            return None

    def _start_pystray(self):
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
            return (i18n.t("Resume background sync") if paused
                    else i18n.t("Pause background sync"))

        menu = pystray.Menu(
            pystray.MenuItem(i18n.t("Open DriveSync"), idle(self._tray_open),
                             default=True),
            pystray.MenuItem(i18n.t("Sync all now"), idle(self._tray_sync)),
            pystray.MenuItem(pause_label, idle(self._tray_toggle)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(i18n.t("Quit DriveSync"), idle(self._tray_quit)),
        )

        def run_tray():
            try:
                icon.run()
            except Exception:
                pass

        try:
            icon = pystray.Icon("DriveSync",
                                _icon_image(Image, ImageDraw),
                                i18n.t("DriveSync — local Drive sync"), menu)
            detached = getattr(icon, "run_detached", None)
            if detached is not None:
                try:
                    if detached():
                        return icon
                except Exception:
                    pass
            backend = type(icon).__module__.rsplit(".", 1)[-1]
            # these backends own the main thread; claiming a tray here would
            # make window-close hide the app behind an icon that never shows
            if backend in ("gtk", "appindicator", "dummy"):
                return None
            thread = threading.Thread(target=run_tray, daemon=True,
                                      name="drivesync-tray")
            thread.start()
            # give the backend a moment to fail before promising a tray
            thread.join(timeout=0.2)
            if not thread.is_alive() and getattr(icon, "visible", True) is False:
                return None
            return icon
        except Exception:
            return None

    def _tray_open(self):
        if self._closing:
            return
        self.win.deiconify()
        self.win.show_all()
        # bring the window to the front even when it was hidden to the tray
        self.win.present()
        try:
            self.win.set_urgency_hint(True)
            GLib.timeout_add(
                1500, lambda: (self.win.set_urgency_hint(False), False)[1])
        except Exception:
            pass

    def _tray_sync(self):
        self.worker.sync_now()

    def _tray_toggle(self):
        if self.worker.status.get("paused"):
            self.worker.resume()
        else:
            self.worker.pause()

    def _sync_tray_pause_label(self, paused):
        """Keep the tray's pause item honest about the current state.

        The pystray backend computes its label dynamically; the
        AppIndicator menu is plain GTK, so its item text is updated here
        (same thread as the menu — safe, called from GLib callbacks).
        """
        item = getattr(self, "_tray_pause_item", None)
        if item is not None:
            item.set_label(i18n.t("Resume background sync" if paused
                                  else "Pause background sync"))

    def _tray_quit(self):
        self._quit()

    # ---- Options tab (theme, accent, language, startup, about) --------
    def _options_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      border_width=6)

        # --- appearance ------------------------------------------------
        box.pack_start(self._tr_label("Appearance", xalign=0,
                                      halign=Gtk.Align.START), False, False, 0)
        box.pack_start(self._tr_label("Theme", xalign=0,
                                      halign=Gtk.Align.START), False, False, 0)
        self.btn_theme = {}
        for group_title, keys in (
                ("Base theme", themes.BASE_THEMES),
                ("Style variants", themes.STYLE_THEMES)):
            box.pack_start(self._tr_dim(group_title), False, False, 0)
            row_theme = Gtk.Box(spacing=6)
            for key in keys:
                # "glass-light" -> "Theme: glass light" (the i18n key)
                label_key = f"Theme: {key.replace('-', ' ')}"
                tip_key = f"tooltip theme {key.replace('-', ' ')}"
                b = Gtk.ToggleButton(label=i18n.t(label_key))
                b.set_tooltip_text(i18n.t(tip_key))
                self._reg(b, "set_label", label_key)
                self._reg(b, "set_tooltip_text", tip_key)
                b.connect("toggled", self._on_theme_button, key)
                self.btn_theme[key] = b
                row_theme.pack_start(b, False, False, 0)
            box.pack_start(row_theme, False, False, 0)

        box.pack_start(self._tr_label("Button & highlight color", xalign=0,
                                      halign=Gtk.Align.START),
                       False, False, 0)
        row_accent = Gtk.Box(spacing=6)
        self.swatches = {}
        for name in themes.ACCENTS:
            b = Gtk.Button()
            ctx = b.get_style_context()
            ctx.add_class("swatch")
            ctx.add_class(f"swatch-{name}")
            b.set_size_request(34, 30)
            b.set_tooltip_text(i18n.t("tooltip accent"))
            self._reg(b, "set_tooltip_text", "tooltip accent")
            b.connect("clicked", self._on_accent_button, name)
            self.swatches[name] = b
            row_accent.pack_start(b, False, False, 0)
        box.pack_start(row_accent, False, False, 0)

        # --- language ---------------------------------------------------
        box.pack_start(self._tr_label("Language / Idioma", xalign=0,
                                      halign=Gtk.Align.START),
                       False, False, 0)
        self.btn_lang = Gtk.Button()
        self.btn_lang.set_tooltip_text(i18n.t("tooltip language"))
        self._reg(self.btn_lang, "set_tooltip_text", "tooltip language")
        self.btn_lang.connect("clicked", self._on_language)
        self._sync_lang_button()
        box.pack_start(self.btn_lang, False, False, 0)

        # --- startup ------------------------------------------------------
        box.pack_start(self._tr_label("Startup", xalign=0,
                                      halign=Gtk.Align.START), False, False, 0)
        row_auto = Gtk.Box(spacing=8)
        self.sw_autostart = Gtk.Switch()
        self.sw_autostart.set_active(autostart.is_enabled())
        self.sw_autostart.connect("notify::active", self._on_autostart_toggled)
        row_auto.pack_start(self.sw_autostart, False, False, 0)
        row_auto.pack_start(
            self._tr_label("Start DriveSync at login", xalign=0), True, True, 0)
        box.pack_start(row_auto, False, False, 0)
        row_tray = Gtk.Box(spacing=8)
        self.sw_tray = Gtk.Switch()
        self.sw_tray.set_active(bool(self.cfg.get("tray_enabled", True)))
        self.sw_tray.connect("notify::active", self._on_tray_toggled)
        row_tray.pack_start(self.sw_tray, False, False, 0)
        row_tray.pack_start(
            self._tr_label("Stay in the system tray", xalign=0), True, True, 0)
        box.pack_start(row_tray, False, False, 0)

        # --- about --------------------------------------------------------
        box.pack_start(self._tr_label("About", xalign=0,
                                      halign=Gtk.Align.START), False, False, 0)
        about = self._dim_label(f"DriveSync v{__version__}")
        box.pack_start(about, False, False, 0)
        box.pack_start(self._dim_label(
            i18n.T("Data folder: {dir}", dir=app_dir())), False, False, 0)

        self._sync_theme_buttons()
        self._sync_accent_selection()
        return _card(box)

    def _sync_theme_buttons(self):
        """Reflect the configured theme without re-triggering toggles."""
        self._theme_guard = True
        try:
            current = themes.canonical(self.cfg.get("theme", "system")) \
                or "system"
            for key, b in self.btn_theme.items():
                b.set_active(key == current)
        finally:
            self._theme_guard = False

    def _sync_accent_selection(self):
        """Ring the swatch matching the configured accent color."""
        current = self.cfg.get("accent", "blue")
        for name, b in self.swatches.items():
            ctx = b.get_style_context()
            if name == current:
                ctx.add_class("selected")
            else:
                ctx.remove_class("selected")

    def _on_theme_button(self, btn, key):
        if self._theme_guard or not btn.get_active():
            # a click on the active button turns it off — snap back
            self._sync_theme_buttons()
            return
        try:
            self.cfg = update_config(theme=key)
        except ValueError as exc:
            self._error(str(exc))
            self._sync_theme_buttons()
            return
        themes.apply(key, self.cfg.get("accent", "blue"))
        self._sync_theme_buttons()

    def _on_accent_button(self, _btn, name):
        try:
            self.cfg = update_config(accent=name)
        except ValueError as exc:
            self._error(str(exc))
            return
        themes.apply(self.cfg.get("theme", "system"), name)
        self._sync_accent_selection()

    def _sync_lang_button(self):
        """Show the language a click would switch TO."""
        self.btn_lang.set_label(f"\U0001F310 {i18n.other_name()}")

    def _on_language(self, _btn):
        new = "es" if i18n.get_language() == "en" else "en"
        i18n.set_language(new)
        try:
            self.cfg = update_config(language=new)
        except ValueError as exc:
            self._error(str(exc))
            i18n.set_language("en" if new == "es" else "es")
            return
        # the tray menu holds translated labels too: rebuild it
        self._restart_tray()
        self._retranslate()
        self.hb.set_subtitle(
            i18n.T("Language: Español") if new == "es"
            else i18n.T("Idioma: English"))

    def _on_autostart_toggled(self, sw, _pspec):
        on = sw.get_active()
        ok = autostart.enable() if on else autostart.disable()
        if not ok:
            sw.set_active(False)
            self._error(i18n.T(
                "Could not open {path}: {exc}",
                path=autostart.autostart_path(), exc="not writable"))
            return
        try:
            self.cfg = update_config(start_at_login=on)
        except ValueError as exc:
            self._error(str(exc))

    def _on_tray_toggled(self, sw, _pspec):
        on = sw.get_active()
        try:
            self.cfg = update_config(tray_enabled=on)
        except ValueError as exc:
            self._error(str(exc))
            sw.set_active(not on)
            return
        self._restart_tray()

    def _restart_tray(self):
        """Stop and (when enabled) re-create the tray icon.

        Needed after a language switch (translated menu labels) and a
        tray-enabled toggle. Without a working tray backend the call is a
        silent no-op, matching launch behavior.
        """
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        if self.cfg.get("tray_enabled", True):
            self.tray = self._start_tray()

    # ---- Overview tab -------------------------------------------------
    def _overview_tab(self):
        """At-a-glance sync health, accounts, pairs and recent activity."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      border_width=6)
        self.overview_status = Gtk.Label(xalign=0)
        self.overview_status.set_line_wrap(True)
        self.overview_status.get_style_context().add_class("hint")
        box.pack_start(_card(self.overview_status), False, False, 0)
        self.overview_summary = Gtk.Label(xalign=0)
        self.overview_summary.set_line_wrap(True)
        box.pack_start(_card(self.overview_summary), False, False, 0)
        self.overview_pairs_store, self.overview_pairs_view, scroll = _list_view(
            ["Local folder", "Drive folder", "State", "Last sync"],
            (str, str, str, str))
        self._tr_columns(self.overview_pairs_view,
                         ["Local folder", "Drive folder", "State", "Last sync"])
        scroll.set_size_request(-1, 150)
        box.pack_start(_card(scroll), True, True, 0)
        self.overview_events_store, self.overview_events_view, ev_scroll = _list_view(
            ["When", "Level", "Action", "Path"], (str, str, str, str))
        self._tr_columns(self.overview_events_view,
                         ["When", "Level", "Action", "Path"])
        ev_scroll.set_size_request(-1, 120)
        box.pack_start(_card(ev_scroll), True, True, 0)
        actions = Gtk.Box(spacing=6)
        sync = self._tr_button("Sync all now")
        sync.get_style_context().add_class("suggested-action")
        sync.connect("clicked", self._on_sync_all)
        actions.pack_start(sync, False, False, 0)
        folders = self._tr_button("Manage folders")
        folders.connect("clicked", lambda _b: self.nb.set_current_page(3))
        actions.pack_start(folders, False, False, 0)
        accounts = self._tr_button("Manage accounts")
        accounts.connect("clicked", lambda _b: self.nb.set_current_page(2))
        actions.pack_start(accounts, False, False, 0)
        box.pack_start(actions, False, False, 0)
        self._refresh_overview()
        return _card(box)

    def _refresh_overview(self):
        """Refresh overview labels and compact pair/activity lists."""
        if not hasattr(self, "overview_status"):
            return
        status = self.worker.status
        paused = bool(status.get("paused"))
        state = i18n.t("paused" if paused else "running")
        last_run = status.get("last_run")
        last = (i18n.T("{sec}s ago", sec=int(max(0, time.time() - last_run)))
                if last_run else i18n.t("never"))
        next_run = status.get("next_run")
        next_text = (i18n.T("in {sec}s", sec=int(max(0, next_run - time.time())))
                     if next_run and not paused else i18n.t("not scheduled"))
        error = status.get("last_error")
        self.overview_status.set_text(i18n.T(
            "Background worker: {state} — last sync {last}; next sync {next}",
            state=state, last=last, next=next_text) +
            (i18n.T("Last error: {err}", err=error) if error else ""))
        accounts = self.store.accounts()
        pairs = self.store.pairs()
        active = sum(1 for p in pairs if p["enabled"] and not p["paused_until"])
        paused_pairs = len(pairs) - active
        recent = self.store.recent_events(limit=8)
        problems = sum(1 for e in recent if e["level"] in ("error", "conflict"))
        self.overview_summary.set_text(i18n.T(
            "{accounts} account(s) · {pairs} sync pair(s) · {active} active · {paused} paused · {problems} recent issue(s)",
            accounts=len(accounts), pairs=len(pairs), active=active,
            paused=paused_pairs, problems=problems))
        self.overview_pairs_store.clear()
        for pair in pairs[:8]:
            self.overview_pairs_store.append([
                pair["local_path"], pair["remote_path"] or pair["remote_id"],
                self._pair_state_text(pair),
                self._pair_last_sync_text(pair["id"])])
        self.overview_events_store.clear()
        for event in recent:
            self.overview_events_store.append([
                str(event["ts"]), event["level"] or "info",
                event["action"] or "", event["rel_path"] or ""])

    # ---- Setup tab --------------------------------------------------
    def _setup_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      border_width=6)
        hint = self._tr_dim(
            "DriveSync talks to Google with your own OAuth \u201cDesktop "
            "app\u201d client, so your tokens never pass through a "
            "third-party server. Create one once:\n  1. "
            "console.cloud.google.com \u2192 APIs & Services \u2192 enable "
            "the Drive API\n  2. OAuth consent screen \u2192 External "
            "\u2192 add yourself as a test user\n  3. Credentials \u2192 "
            "Create credentials \u2192 OAuth client ID \u2192 Desktop app\n"
            "  4. Paste the client ID and secret below.")
        box.pack_start(hint, False, False, 0)

        table = Gtk.Grid(column_spacing=10, row_spacing=8, border_width=4)
        table.attach(self._tr_label("Client ID:", halign=Gtk.Align.END),
                     0, 0, 1, 1)
        self.ent_client_id = Gtk.Entry(hexpand=True)
        self.ent_client_id.set_text(str(self.cfg.get("client_id") or ""))
        table.attach(self.ent_client_id, 1, 0, 1, 1)

        table.attach(self._tr_label("Client secret:", halign=Gtk.Align.END),
                     0, 1, 1, 1)
        sec_row = Gtk.Box(spacing=6)
        self.ent_client_secret = Gtk.Entry(hexpand=True)
        self.ent_client_secret.set_visibility(False)
        self.ent_client_secret.set_text(self.cfg.get("client_secret", ""))
        sec_row.pack_start(self.ent_client_secret, True, True, 0)
        show = Gtk.ToggleButton()
        show.set_label(i18n.t("show"))
        self._reg(show, "set_label", "show")
        show.connect("toggled", self._toggle_secret)
        sec_row.pack_start(show, False, False, 0)
        table.attach(sec_row, 1, 1, 1, 1)

        save = self._tr_button("Save client")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", self._on_save_setup)
        table.attach(save, 1, 2, 1, 1)
        cloud = Gtk.LinkButton.new_with_label(
            "https://console.cloud.google.com/apis/credentials",
            i18n.t("Open Google Cloud Console"))
        self._reg(cloud, "set_label", "Open Google Cloud Console")
        cloud.set_halign(Gtk.Align.START)
        cloud.set_tooltip_text(i18n.t("Create the Desktop-app OAuth client "
                                      "there"))
        self._reg(cloud, "set_tooltip_text",
                  "Create the Desktop-app OAuth client there")
        table.attach(cloud, 1, 3, 1, 1)
        self.setup_status = Gtk.Label(xalign=0)
        self.setup_status.get_style_context().add_class("hint")
        table.attach(self.setup_status, 1, 4, 1, 1)
        box.pack_start(table, False, False, 0)
        box.pack_start(self._tr_dim(
            "Closing this window keeps DriveSync running in the system tray "
            "(open it again from the tray icon)."), False, False, 0)
        self._refresh_setup_status()
        return _card(box)

    def _toggle_secret(self, btn):
        self.ent_client_secret.set_visibility(btn.get_active())

    def _on_save_setup(self, _btn):
        cid = self.ent_client_id.get_text().strip()
        csec = self.ent_client_secret.get_text().strip()
        if not cid or not csec:
            self._error(i18n.t("Both client ID and client secret are "
                               "required."))
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
            self._info(i18n.T("Client saved. Next: the Accounts tab "
                              "\u2192 \u201cSign in with Google\u2026\u201d."))
            self.nb.set_current_page(1)

    def _refresh_setup_status(self):
        n = len(self.store.accounts())
        ok = bool(self.cfg.get("client_id"))
        self.setup_status.set_text(
            i18n.T("OAuth client: {ok} — accounts authorized: {n} — "
                   "data dir: {dir}",
                   ok=i18n.t("configured" if ok else "not set"),
                   n=n, dir=app_dir()))

    # ---- Accounts tab -----------------------------------------------
    def _accounts_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      border_width=6)
        self.acc_store, self.acc_view, scroll = _list_view(
            ["#", "Account", "Storage"], (int, str, str))
        self._tr_columns(self.acc_view, ["#", "Account", "Storage"])
        box.pack_start(scroll, True, True, 0)

        row = Gtk.Box(spacing=6)
        self.btn_signin = self._tr_button("Sign in with Google\u2026")
        self.btn_signin.get_style_context().add_class("suggested-action")
        self.btn_signin.connect("clicked", self._on_signin)
        row.pack_start(self.btn_signin, False, False, 0)
        btn_rm = self._tr_button("Remove selected")
        btn_rm.get_style_context().add_class("destructive-action")
        btn_rm.connect("clicked", self._on_remove_account)
        more_row.pack_start(btn_rm, False, False, 0)
        box.pack_start(more_row, False, False, 0)

        note = self._tr_dim(
            "Google's consent page opens in your browser once per account "
            "— that step must happen on google.com. Tokens are stored "
            "locally and refreshed automatically.")
        box.pack_start(note, False, False, 0)

        self._refresh_accounts()
        return _card(box)

    def _on_signin(self, _btn):
        self.btn_signin.set_sensitive(False)
        self.hb.set_subtitle(i18n.t("waiting for Google…"))
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
        self._info(i18n.T("Authorized {email} (account #{acc_id}).\n"
                          "Now add folders on the Folders tab.",
                          email=email, acc_id=acc_id))
        self.nb.set_current_page(2)

    def _on_remove_account(self, _btn):
        sel = self.acc_view.get_selection().get_selected()
        if sel[1] is None:
            return
        acc_id = self.acc_store[sel[1]][0]
        if self._confirm(i18n.T(
                "Remove account #{acc_id}? Stored tokens are deleted; sync "
                "pairs using it are kept but will pause until "
                "re-authorized.", acc_id=acc_id)):
            self.store.delete_account(acc_id)
            self._refresh_accounts()
            self._refresh_setup_status()

    def _refresh_accounts(self):
        self.acc_store.clear()
        accounts = self.store.accounts()
        for a in accounts:
            self.acc_store.append([a["id"], a["email"], "…"])
        if accounts:
            # quota costs one API call per account; fetch in the background
            # so the tab paints instantly and a stale token never blocks it
            self._bg(self._fetch_quotas, self._quotas_done)

    def _fetch_quotas(self):
        out = {}
        for a in self.store.accounts():
            try:
                quota = (self._drive_for(a["id"]).about()
                         .get("storageQuota") or {})
                used = _drive_bytes(int(quota.get("usage") or 0))
                limit = quota.get("limit")
                if limit:
                    out[a["id"]] = i18n.T("{used} / {limit} used",
                                          used=used,
                                          limit=_drive_bytes(int(limit)))
                else:
                    out[a["id"]] = (i18n.T("{used} used", used=used)
                                    if used else "—")
            except Exception as exc:
                out[a["id"]] = f"— ({str(exc)[:60]})"
        return out

    def _quotas_done(self, quotas):
        for row in self.acc_store:
            if row[0] in quotas:
                row[2] = quotas[row[0]]

    # ---- Folders tab --------------------------------------------------
    def _folders_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      border_width=6)
        headers = ["#", "Direction", "Local folder", "Drive folder", "Scope",
                   "State", "Last sync"]
        self.pair_store, self.pair_view, scroll = _list_view(
            [i18n.t(h) for h in headers], (int, str, str, str, str, str, str))
        self._tr_columns(self.pair_view, headers)
        self.pair_view.connect(
            "row-activated", lambda *_a: self._on_open_pair_folder())
        box.pack_start(scroll, True, True, 0)

        self.pairs_hint = self._dim_label("")
        box.pack_start(self.pairs_hint, False, False, 0)

        # Keep the most common actions together in the first row.
        row = Gtk.Box(spacing=6)
        btn_add = self._tr_button("Add folder\\u2026")
        btn_add.get_style_context().add_class("suggested-action")
        btn_add.connect("clicked", self._on_add_pair)
        row.pack_start(btn_add, False, False, 0)
        btn_open = self._tr_button("Open local folder")
        btn_open.set_tooltip_text(i18n.t(
            "Open the selected pair's local folder in your file manager "
            "(double-clicking a row does the same)"))
        self._reg(btn_open, "set_tooltip_text",
                  "Open the selected pair's local folder in your file "
                  "manager (double-clicking a row does the same)")
        btn_open.connect("clicked", self._on_open_pair_folder)
        row.pack_start(btn_open, False, False, 0)
        btn_edit = self._tr_button("Edit\\u2026")
        btn_edit.set_tooltip_text(i18n.t(
            "Change the selected pair's direction and conflict policy"))
        self._reg(btn_edit, "set_tooltip_text",
                  "Change the selected pair's direction and conflict policy")
        btn_edit.connect("clicked", self._on_edit_pair)
        row.pack_start(btn_edit, False, False, 0)
        btn_toggle = self._tr_button("Pause / resume")
        btn_toggle.connect("clicked", self._on_toggle_pair)
        row.pack_start(btn_toggle, False, False, 0)
        btn_sync = self._tr_button("Sync selected now")
        btn_sync.connect("clicked", self._on_sync_pair)
        row.pack_start(btn_sync, False, False, 0)
        box.pack_start(row, False, False, 0)

        # Less frequent and review actions live on a separate row.
        more_row = Gtk.Box(spacing=6)
        btn_until = self._tr_button("Until tomorrow")
        btn_until.set_tooltip_text(i18n.t(
            "Pause until tomorrow at 09:00 — click again to cancel the "
            "pause"))
        self._reg(btn_until, "set_tooltip_text",
                  "Pause until tomorrow at 09:00 — click again to cancel "
                  "the pause")
        btn_until.connect("clicked", self._on_pause_until)
        more_row.pack_start(btn_until, False, False, 0)
        btn_preview = self._tr_button("Preview changes\\u2026")
        btn_preview.set_tooltip_text(i18n.t(
            "Dry-run the selected pair's next pass: what would change, "
            "nothing applied"))
        self._reg(btn_preview, "set_tooltip_text",
                  "Dry-run the selected pair's next pass: what would change, "
                  "nothing applied")
        btn_preview.connect("clicked", self._on_pair_preview)
        more_row.pack_start(btn_preview, False, False, 0)
        btn_scope = self._tr_button("Subfolders\\u2026")
        btn_scope.set_tooltip_text(i18n.t(
            "Selective sync: choose which subfolders of this pair mirror; "
            "the rest stays on Drive and on disk, downloadable on demand "
            "from the Drive tab"))
        self._reg(btn_scope, "set_tooltip_text",
                  "Selective sync: choose which subfolders of this pair "
                  "mirror; the rest stays on Drive and on disk, downloadable "
                  "on demand from the Drive tab")
        btn_scope.connect("clicked", self._on_pair_subfolders)
        more_row.pack_start(btn_scope, False, False, 0)
        btn_conf = self._tr_button("Conflicts\\u2026")
        btn_conf.set_tooltip_text(i18n.t(
            "Review conflict copies kept by the \\u201cboth\\u201d policy: "
            "keep the copy, the original, or both"))
        self._reg(btn_conf, "set_tooltip_text",
                  "Review conflict copies kept by the \\u201cboth\\u201d "
                  "policy: keep the copy, the original, or both")
        btn_conf.connect("clicked", self._on_pair_conflicts)
        more_row.pack_start(btn_conf, False, False, 0)
        btn_rm = self._tr_button("Remove")
        btn_rm.get_style_context().add_class("destructive-action")
        btn_rm.connect("clicked", self._on_remove_pair)
        more_row.pack_start(btn_rm, False, False, 0)
        box.pack_start(more_row, False, False, 0)

        self._refresh_pairs()
        return _card(box)

    def _on_add_pair(self, _btn):
        accounts = self.store.accounts()
        if not accounts:
            self._error(i18n.t("No account yet — authorize one on the "
                               "Accounts tab first."))
            return
        dlg = Gtk.Dialog(title=i18n.t("Add sync folder"),
                         transient_for=self.win, modal=True)
        dlg.add_buttons(i18n.t("Cancel"), Gtk.ResponseType.CANCEL,
                        i18n.t("OK"), Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=8, row_spacing=8, border_width=10)
        grid.attach(self._tr_label("Local folder:", halign=Gtk.Align.END),
                    0, 0, 1, 1)
        chooser = Gtk.FileChooserButton(title=i18n.t("Pick a folder"),
                                        action=Gtk.FileChooserAction.SELECT_FOLDER)
        grid.attach(chooser, 1, 0, 1, 1)
        grid.attach(self._tr_label("Google account:", halign=Gtk.Align.END),
                    0, 1, 1, 1)
        acc_combo = Gtk.ComboBoxText()
        for a in accounts:
            acc_combo.append_text(f"#{a['id']}  {a['email']}")
        acc_combo.set_active(0)
        grid.attach(acc_combo, 1, 1, 1, 1)
        grid.attach(self._tr_label("Drive folder:", halign=Gtk.Align.END),
                    0, 2, 1, 1)
        remote_row = Gtk.Box(spacing=6)
        ent_remote = Gtk.Entry(hexpand=True)
        ent_remote.set_placeholder_text(i18n.t(
            "pick with Browse\u2026, or the \u2026/folders/<ID> from its URL"))
        remote_row.pack_start(ent_remote, True, True, 0)
        btn_browse = self._tr_button("Browse Drive\u2026")
        remote_row.pack_start(btn_browse, False, False, 0)
        grid.attach(remote_row, 1, 2, 1, 1)
        grid.attach(self._tr_label("Display name:", halign=Gtk.Align.END),
                    0, 3, 1, 1)
        ent_path = Gtk.Entry()
        ent_path.set_placeholder_text("optional, e.g. My Drive/projects")
        grid.attach(ent_path, 1, 3, 1, 1)
        btn_browse.connect("clicked", self._on_browse_drive,
                           acc_combo, ent_remote, ent_path)
        grid.attach(self._tr_label("Direction:", halign=Gtk.Align.END),
                    0, 4, 1, 1)
        dir_combo = Gtk.ComboBoxText()
        for d in DIRECTIONS:
            dir_combo.append_text(d)
        dir_combo.set_active(0)
        grid.attach(dir_combo, 1, 4, 1, 1)
        grid.attach(self._tr_label("Conflicts:", halign=Gtk.Align.END),
                    0, 5, 1, 1)
        pol_combo = Gtk.ComboBoxText()
        for p in POLICIES:
            pol_combo.append_text(p)
        pol_combo.set_active(0)
        grid.attach(pol_combo, 1, 5, 1, 1)

        # selective scope is chosen BEFORE the pair exists, so the first
        # pass never downloads subfolders the user did not pick
        grid.attach(self._tr_label("Sync scope:", halign=Gtk.Align.END),
                    0, 6, 1, 1)
        scope_row = Gtk.Box(spacing=6)
        scope_lbl = self._tr_label("Everything", xalign=0)
        scope_lbl.get_style_context().add_class("hint")
        scope_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        scope_row.pack_start(scope_lbl, True, True, 0)
        btn_scope = self._tr_button("Choose subfolders\u2026")
        btn_scope.set_tooltip_text(i18n.t(
            "Pick which Drive subfolders sync from the start; the rest stay "
            "on Drive until you choose them (on demand from the Drive tab)"))
        self._reg(btn_scope, "set_tooltip_text",
                  "Pick which Drive subfolders sync from the start; the rest "
                  "stay on Drive until you choose them (on demand from the "
                  "Drive tab)")
        scope_row.pack_start(btn_scope, False, False, 0)
        grid.attach(scope_row, 1, 6, 1, 1)
        chk_paused = Gtk.CheckButton(
            label=i18n.t("Start paused (review the pair before its first "
                         "sync)"))
        chk_paused.set_tooltip_text(i18n.t(
            "Creates the pair disabled — press \u201cPause / resume\u201d "
            "on the Folders tab when you are ready"))
        grid.attach(chk_paused, 1, 7, 1, 1)

        scope = {"pick": False, "paths": []}

        def reset_scope(*_args):
            scope["pick"], scope["paths"] = False, []
            scope_lbl.set_text(i18n.t("Everything"))

        def choose_scope(_btn):
            remote_id = ent_remote.get_text().strip()
            if not remote_id:
                self._error(i18n.t("Choose the Drive folder first \u2014 its "
                                   "subfolders are what selective sync "
                                   "picks from."))
                return
            idx = acc_combo.get_active()
            acc = accounts[idx if idx >= 0 else 0]
            result = self._scope_dialog(
                acc["id"], remote_id,
                scope["paths"] if scope["pick"] else [],
                i18n.t("Choose subfolders to sync"))
            if result is None:
                return
            pick, paths = result
            scope["pick"], scope["paths"] = pick, paths
            if pick:
                shown = ", ".join(paths[:3]) + ("\u2026" if len(paths) > 3
                                                else "")
                scope_lbl.set_text(i18n.T("{n} subfolder(s): {shown}",
                                          n=len(paths), shown=shown))
            else:
                scope_lbl.set_text(i18n.t("Everything"))

        ent_remote.connect("changed", reset_scope)
        acc_combo.connect("changed", reset_scope)
        btn_scope.connect("clicked", choose_scope)

        dlg.get_content_area().pack_start(grid, True, True, 0)
        dlg.show_all()

        if dlg.run() == Gtk.ResponseType.OK:
            local = chooser.get_filename()
            remote_id = ent_remote.get_text().strip()
            try:
                if not local:
                    raise ValueError(i18n.t("pick a local folder first"))
                if not remote_id:
                    raise ValueError(i18n.t("the Drive folder ID is required "
                                            "(\u2026/folders/<ID> in its "
                                            "web URL)"))
                acc_ids = [a["id"] for a in accounts]
                paused = chk_paused.get_active()
                pair = self.store.add_pair(
                    acc_ids[acc_combo.get_active()], local, remote_id,
                    remote_path=ent_path.get_text().strip(),
                    direction=DIRECTIONS[dir_combo.get_active()],
                    conflict_policy=POLICIES[pol_combo.get_active()],
                    enabled=0 if paused else 1,
                    include_patterns=(scope["paths"] if scope["pick"]
                                      else []))
                self._refresh_pairs()
                if paused:
                    self._info(i18n.T(
                        "Pair #{pid} added paused and will not sync until "
                        "you press \u201cPause / resume\u201d on the Folders "
                        "tab.", pid=pair["id"]))
                else:
                    scope_txt = (i18n.T(" Only {n} chosen subfolder(s) will "
                                        "sync.", n=len(scope["paths"]))
                                 if scope["pick"] else "")
                    self._info(i18n.T(
                        "Pair #{pid} added.{scope} First sync runs within "
                        "~{sec}s, or press \u201cSync selected now\u201d.",
                        pid=pair["id"], scope=scope_txt,
                        sec=self.cfg['sync_interval']))
            except Exception as exc:
                self._error(str(exc))
        dlg.destroy()

    def _drive_for(self, acc_id):
        """Drive client bound to the account's stored tokens (as serve does)."""
        account = self.store.account(acc_id)
        if not account:
            raise ValueError(f"account #{acc_id} not found")
        token = token_from_json(account["token_json"])
        return Drive(lambda: access_token_persisting(self.store, acc_id,
                                                     token))

    def _on_browse_drive(self, _btn, acc_combo, ent_remote, ent_path):
        accounts = self.store.accounts()
        if not accounts:
            self._error(i18n.t("No account yet \u2014 authorize one on the "
                               "Accounts tab first."))
            return
        idx = acc_combo.get_active()
        acc_id = accounts[idx if idx >= 0 else 0]["id"]
        self._browse_folders(acc_id, ent_remote, ent_path,
                             "root", i18n.t("My Drive"),
                             i18n.t("My Drive"))

    def _browse_folders(self, acc_id, ent_remote, ent_path,
                        parent_id, parent_name, path):
        """Navigable Drive folder picker — the same flow as the dashboard.

        My Drive (plus a virtual 'Shared with me' entry) \u2192 double-click
        a row to descend, 'Up one level' climbs back, 'Use this folder'
        binds the folder shown above to the pair form.
        """
        dlg = Gtk.Dialog(title=i18n.t("Choose a Drive folder"),
                         transient_for=self.win, modal=True)
        dlg.add_buttons(i18n.t("Cancel"), Gtk.ResponseType.CANCEL,
                        i18n.t("Use this folder"), Gtk.ResponseType.OK)
        dlg.set_default_size(540, 420)
        state = {"stack": [(parent_id, parent_name, path)],
                 "loading": False, "open": True}

        box = dlg.get_content_area()
        crumb = self._dim_label(path)
        box.pack_start(crumb, False, False, 4)
        headers = [i18n.t("Folder"), i18n.t("ID")]
        sel_store, sel_view, scroll = _list_view(headers, (str, str))
        box.pack_start(scroll, True, True, 0)
        btn_up = Gtk.Button()
        btn_up.set_label(i18n.t("Up one level"))
        btn_up.set_halign(Gtk.Align.START)
        box.pack_start(btn_up, False, False, 4)
        box.pack_start(self._dim_label(i18n.t(
            "Double-click a folder to open it \u2014 \u201cUse this "
            "folder\u201d binds the one shown above.")), False, False, 4)

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
                    self._error(i18n.T("Drive listing failed \u2014 {err}",
                                       err=res.get('error', '?')))
                    return
                rows = ([{"id": "shared", "name": i18n.t("Shared with me")}]
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
                state["stack"].append(("shared", i18n.t("Shared with me"),
                                       i18n.t("Shared with me")))
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

    def _on_pause_until(self, _btn):
        """Toggle the selected pair's 'pause until tomorrow 09:00' window.

        Clicking while the pause is active cancels it right away; the
        worker auto-resumes the pair (with a logged event) once the
        deadline passes.
        """
        pair_id = self._selected_pair()
        if pair_id is None:
            return
        pair = self.store.pair(pair_id)
        if not pair:
            return
        if pair["paused_until"]:
            self.store.update_pair(pair_id, paused_until=None)
            self.store.log(pair_id, "pair-resume", "",
                           "pause-until-tomorrow cancelled — pair active "
                           "again")
        else:
            # tomorrow 09:00 local time, stored as UTC ISO (uniform with
            # the events table so workers on any machine agree)
            now = datetime.now().astimezone()
            morning = (now + timedelta(days=1)).replace(
                hour=9, minute=0, second=0, microsecond=0)
            until_iso = morning.astimezone(timezone.utc) \
                .isoformat(timespec="seconds")
            self.store.update_pair(pair_id, paused_until=until_iso)
            self.store.log(pair_id, "pair-pause", "",
                           f"paused until {to_local(until_iso)} "
                           "(tomorrow 09:00 local)")
        self._refresh_pairs()

    def _pair_state_text(self, p):
        """State cell for one pair: active / paused / paused until <time>."""
        if not p["enabled"]:
            return i18n.t("paused")
        until = p["paused_until"]
        if until:
            try:
                expiry = datetime.fromisoformat(str(until))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < expiry:
                    return i18n.T("paused until {when}", when=to_local(
                        expiry.isoformat(timespec="seconds")))
            except ValueError:
                pass
        return i18n.t("active")

    # op-name labels for the preview dialog (translated via i18n)
    _OP_LABELS = {
        "upload": "Upload", "download": "Download",
        "delete_local": "Delete local", "delete_remote": "Delete on Drive",
        "rmdir_local": "Remove local folder",
        "rmdir_remote": "Remove Drive folder",
        "mkdir_local": "Create local folder",
        "mkdir_remote": "Create Drive folder",
        "rename_local": "Rename local", "rename_remote": "Rename on Drive",
        "reconcile": "Reconcile", "forget": "Forget",
        "record_gdoc": "Record Google doc",
        "skip_gdoc_collision": "Skip (Google doc)",
        "ack_local_delete": "Ack local deletion",
        "ack_remote_delete": "Ack Drive deletion",
    }

    def _on_pair_preview(self, _btn):
        """Dry-run dialog for the selected pair: exactly what the next pass
        would do (uploads, downloads, deletions, renames — in pass order),
        with nothing applied and nothing logged."""
        pair_id = self._selected_pair()
        if pair_id is None:
            return
        pair = self.store.pair(pair_id)
        if not pair:
            return
        dlg = Gtk.Dialog(title=i18n.T("Preview — pair #{pid}", pid=pair_id),
                         transient_for=self.win, modal=True)
        dlg.add_buttons(i18n.t("Close"), Gtk.ResponseType.CLOSE)
        dlg.set_default_size(660, 460)
        box = dlg.get_content_area()
        box.set_spacing(6)
        summary = self._dim_label(i18n.t("Scanning\u2026"))
        box.pack_start(summary, False, False, 0)
        headers = ["Action", "Path", "Detail"]
        model, view, scroll = _list_view([i18n.t(h) for h in headers],
                                         (str, str, str))
        box.pack_start(scroll, True, True, 0)
        box.pack_start(self._dim_label(i18n.t(
            "Nothing is applied — this only shows what the next pass "
            "would do.")), False, False, 0)

        state = {"open": True}

        def done(res):
            if not state["open"]:
                return
            if res.get("error"):
                summary.set_text(i18n.T("Could not scan — {err}",
                                        err=res["error"]))
                return
            t = res["totals"]
            text = i18n.T(
                "{u} upload(s), {d} download(s), {x} deletion(s), "
                "{r} rename(s)",
                u=t["uploads"], d=t["downloads"], x=t["deletions"],
                r=t["renames"])
            if res.get("blocked"):
                text += " " + i18n.T(
                    "({n} deletion(s) blocked by the safety limit)",
                    n=res["blocked"])
            if not res["ops"]:
                text = (i18n.t("Nothing to sync — both sides already match.")
                        if not res.get("blocked") else
                        i18n.T("Both sides match, but {n} deletion(s) "
                               "remain blocked by the safety limit.",
                               n=res["blocked"]))
            summary.set_text(text)
            for op in res["ops"]:
                label = i18n.t(self._OP_LABELS.get(op[0], op[0]))
                detail = op[2] if len(op) > 2 else ""
                model.append([label, op[1], detail])

        def work():
            drive = self._drive_for(pair["account_id"])
            try:
                return engine.preview_pair(self.store, self.cfg, pair, drive)
            except Exception as exc:
                return {"error": str(exc), "ops": [], "totals": {},
                        "counts": {}, "blocked": 0, "pair_id": pair_id}

        self._bg(work, done)
        dlg.show_all()
        dlg.run()
        state["open"] = False
        dlg.destroy()
        self._refresh_pairs()

    def _on_remove_pair(self, _btn):
        pair_id = self._selected_pair()
        if pair_id is None:
            return
        if self._confirm(i18n.T(
                "Remove pair #{pid}? Local files and Drive contents are "
                "kept — only the sync link is deleted.", pid=pair_id)):
            self.store.delete_pair(pair_id)
            self._refresh_pairs()

    def _pair_last_sync_text(self, pair_id):
        """Human 'last sync' cell for one pair (never raises: status is
        written by the worker thread and may not exist yet)."""
        info = (self.worker.status.get("pairs") or {}).get(pair_id)
        if not info or not info.get("ts"):
            return i18n.t("never")
        age = int(max(0, time.time() - info["ts"]))
        ago = i18n.T("{sec}s ago", sec=age) if age < 3600 else \
            i18n.T("{h}h{m:02d}m ago", h=age // 3600, m=(age % 3600) // 60)
        if not info.get("ok"):
            return i18n.T("ERROR {ago}", ago=ago)
        st = info.get("stats") or {}
        moved = int(st.get("pushed", 0)) + int(st.get("pulled", 0)) \
            + int(st.get("deleted", 0)) + int(st.get("renamed", 0))
        conf = int(st.get("conflicts", 0))
        detail = i18n.T("{n} change(s)", n=moved) if moved \
            else i18n.t("up to date")
        if conf:
            detail += i18n.T(", {n} conflict(s)", n=conf)
        return i18n.T("ok {ago} — {detail}", ago=ago, detail=detail)

    def _refresh_pairs(self):
        self.pair_store.clear()
        pairs = self.store.pairs()
        for p in pairs:
            remote = p["remote_path"] or p["remote_id"]
            state = self._pair_state_text(p)
            scope = parse_include_patterns(p["include_patterns"])
            scope_txt = (i18n.t("everything") if not scope
                         else i18n.T("{n} subfolders", n=len(scope)))
            self.pair_store.append([p["id"], p["direction"], p["local_path"],
                                    remote, scope_txt, state,
                                    self._pair_last_sync_text(p["id"])])
        hint = getattr(self, "pairs_hint", None)
        if hint is not None:
            if pairs:
                hint.set_visible(False)
            else:
                hint.set_text(i18n.t(
                    "No sync folders yet \u2014 press \u201cAdd "
                    "folder\u2026\u201d to link a local folder to Drive. "
                    "Choose subfolders there before the first sync so "
                    "unwanted folders are never downloaded."))
                hint.set_visible(True)

    def _on_open_pair_folder(self, _btn=None):
        pair_id = self._selected_pair()
        if pair_id is None:
            return
        pair = self.store.pair(pair_id)
        if not pair:
            return
        path = pair["local_path"]
        if not os.path.isdir(path):
            self._error(i18n.T("The local folder no longer exists:\n{path}",
                               path=path))
            return
        try:
            uri = GLib.filename_to_uri(os.path.abspath(path), None)
            Gtk.show_uri_on_window(self.win, uri, Gdk.CURRENT_TIME)
        except Exception as exc:
            self._error(i18n.T("Could not open {path}: {exc}",
                               path=path, exc=exc))

    def _scope_dialog(self, acc_id, remote_id, stored, title):
        """Checklist of the Drive folder's immediate subfolders.

        Returns ``(pick_only, paths)``: (False, []) means sync everything,
        (True, [...]) means only those subfolder names, and None means the
        user cancelled. Safe to call before the pair exists, so the first
        sync never downloads folders the user did not choose.
        """
        dlg = Gtk.Dialog(title=title, transient_for=self.win, modal=True)
        dlg.add_buttons(i18n.t("Cancel"), Gtk.ResponseType.CANCEL,
                        i18n.t("Save"), Gtk.ResponseType.OK)
        dlg.set_response_sensitive(Gtk.ResponseType.OK, False)
        dlg.set_default_size(540, 460)
        box = dlg.get_content_area()
        box.set_spacing(6)

        radio_all = Gtk.RadioButton.new_with_label_from_widget(
            None, i18n.t("Sync everything in the Drive folder"))
        box.pack_start(radio_all, False, False, 0)
        radio_pick = Gtk.RadioButton.new_with_label_from_widget(
            radio_all, i18n.t("Only the chosen subfolders"))
        if stored:
            radio_pick.set_active(True)
        box.pack_start(radio_pick, False, False, 0)

        tools = Gtk.Box(spacing=6)
        btn_all = Gtk.Button()
        btn_all.set_label(i18n.t("Select all"))
        btn_none = Gtk.Button()
        btn_none.set_label(i18n.t("Select none"))
        tools.pack_start(btn_all, False, False, 0)
        tools.pack_start(btn_none, False, False, 0)
        box.pack_start(tools, False, False, 0)

        sel_store = Gtk.ListStore(bool, str)
        view = Gtk.TreeView(model=sel_store)
        cell_toggle = Gtk.CellRendererToggle()

        def on_toggle(cell, path_str, store):
            self._on_scope_toggled(cell, path_str, store)
            set_sensitive()

        cell_toggle.connect("toggled", on_toggle, sel_store)
        view.append_column(
            Gtk.TreeViewColumn(i18n.t("Sync"), cell_toggle, active=0))
        view.append_column(
            Gtk.TreeViewColumn(i18n.t("Subfolder"),
                               Gtk.CellRendererText(), text=1))
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(view)
        scroll.set_vexpand(True)
        box.pack_start(scroll, True, True, 0)

        status = self._dim_label(i18n.t("Loading subfolders\u2026"))
        box.pack_start(status, False, False, 0)
        box.pack_start(self._dim_label(i18n.t(
            "Files directly in the Drive folder always sync. Unchosen "
            "subfolders stay on Drive and on disk untouched \u2014 choosing "
            "one downloads it on the next pass. Nothing is ever deleted by "
            "narrowing the scope.")), False, False, 0)

        state = {"open": True, "loaded": False}

        def set_sensitive():
            picked = radio_pick.get_active()
            view.set_sensitive(picked)
            btn_all.set_sensitive(picked and len(sel_store) > 0)
            btn_none.set_sensitive(
                picked and any(row[0] for row in sel_store))
            # "sync everything" needs no listing; a picked subset does, so
            # Save stays disabled until the subfolders have loaded
            dlg.set_response_sensitive(Gtk.ResponseType.OK,
                                       state["loaded"] or not picked)

        def set_all(value):
            for row in sel_store:
                row[0] = value
            set_sensitive()

        radio_pick.connect("toggled", lambda _r: set_sensitive())
        btn_all.connect("clicked", lambda _b: set_all(True))
        btn_none.connect("clicked", lambda _b: set_all(False))

        def work():
            try:
                return folders_for_parent(self._drive_for(acc_id), remote_id)
            except Exception as exc:
                return {"error": str(exc)}

        def done(res):
            if not state["open"]:
                return
            if isinstance(res, dict):        # error marker from work()
                status.set_text(i18n.T(
                    "Could not list subfolders \u2014 {err}",
                    err=res.get('error', '?')))
                return
            for f in sorted(res, key=lambda x: x["name"].lower()):
                chosen = any(p == f["name"] or p.startswith(f["name"] + "/")
                             for p in stored)
                sel_store.append([chosen, f["name"]])
            status.set_text(
                i18n.t("This Drive folder has no subfolders.") if not res
                else i18n.T("{n} subfolder(s) \u2014 tick the ones that "
                            "should sync.", n=len(res)))
            state["loaded"] = True
            set_sensitive()

        set_sensitive()
        dlg.show_all()
        self._bg(work, done)
        result = None
        while True:
            if dlg.run() != Gtk.ResponseType.OK:
                break
            if radio_pick.get_active():
                chosen = [row[1] for row in sel_store if row[0]]
                if not chosen:
                    self._error(i18n.t("Pick at least one subfolder, or "
                                       "switch back to \u201cSync "
                                       "everything\u201d."))
                    continue
                result = (True, chosen)
            else:
                result = (False, [])
            break
        state["open"] = False
        dlg.destroy()
        return result

    def _on_pair_subfolders(self, _btn):
        """Selective-sync dialog: pick which subfolders of the selected
        pair mirror. The rest stays on Drive and on disk, untouched by
        passes; it can be fetched on demand from the Drive tab."""
        pair_id = self._selected_pair()
        if pair_id is None:
            return
        pair = self.store.pair(pair_id)
        if not pair:
            return
        stored = parse_include_patterns(pair["include_patterns"])
        result = self._scope_dialog(
            pair["account_id"], pair["remote_id"], stored,
            i18n.T("Selective sync \u2014 pair #{pid}", pid=pair_id))
        if result is None:
            return
        pick, chosen = result
        if pick:
            self.store.update_pair(pair_id, include_patterns=chosen)
            self.store.log(pair_id, "pair-scope", "",
                           "mirroring subfolders: " + ", ".join(chosen))
        else:
            self.store.update_pair(pair_id, include_patterns=[])
            self.store.log(pair_id, "pair-scope", "",
                           "scope reset: mirroring everything")
        self._refresh_pairs()

    @staticmethod
    def _on_scope_toggled(_cell, path_str, store):
        row = store[path_str]
        row[0] = not row[0]

    # ---- pair editing -------------------------------------------------
    def _on_edit_pair(self, _btn):
        """Change an existing pair's direction / conflict policy / deletes.

        Local and Drive paths are deliberately not editable here: repointing
        either invalidates the snapshot (update_pair handles it safely, but
        a mistyped path looks like mass deletions to the user) — remove and
        re-add the pair instead.
        """
        pair_id = self._selected_pair()
        if pair_id is None:
            return
        pair = self.store.pair(pair_id)
        if not pair:
            return
        dlg = Gtk.Dialog(title=i18n.T("Edit pair #{pid}", pid=pair_id),
                         transient_for=self.win, modal=True)
        dlg.add_buttons(i18n.t("Cancel"), Gtk.ResponseType.CANCEL,
                        i18n.t("Save"), Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=8, row_spacing=8, border_width=10)
        grid.attach(self._tr_label("Direction:", halign=Gtk.Align.END),
                    0, 0, 1, 1)
        dir_combo = Gtk.ComboBoxText()
        for d in DIRECTIONS:
            dir_combo.append_text(d)
        dir_combo.set_active(DIRECTIONS.index(pair["direction"])
                             if pair["direction"] in DIRECTIONS else 0)
        grid.attach(dir_combo, 1, 0, 1, 1)
        grid.attach(self._tr_label("Conflicts:", halign=Gtk.Align.END),
                    0, 1, 1, 1)
        pol_combo = Gtk.ComboBoxText()
        for p in POLICIES:
            pol_combo.append_text(p)
        pol_combo.set_active(POLICIES.index(pair["conflict_policy"])
                             if pair["conflict_policy"] in POLICIES else 0)
        grid.attach(pol_combo, 1, 1, 1, 1)
        chk_del = Gtk.CheckButton(
            label=i18n.t("Allow deletions (files go to trash, never vanish)"))
        chk_del.set_active(bool(pair["allow_deletes"]))
        grid.attach(chk_del, 1, 2, 1, 1)
        dlg.get_content_area().pack_start(grid, True, True, 0)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            try:
                self.store.update_pair(
                    pair_id,
                    direction=DIRECTIONS[dir_combo.get_active()],
                    conflict_policy=POLICIES[pol_combo.get_active()],
                    allow_deletes=1 if chk_del.get_active() else 0)
                self.store.log(pair_id, "pair-edited", "",
                               f"direction {DIRECTIONS[dir_combo.get_active()]}"
                               f", conflicts {POLICIES[pol_combo.get_active()]}"
                               f", deletes "
                               f"{'on' if chk_del.get_active() else 'off'}")
                self._refresh_pairs()
            except Exception as exc:
                self._error(str(exc))
        dlg.destroy()

    # ---- conflict copies ----------------------------------------------
    def _on_pair_conflicts(self, _btn=None):
        pair_id = self._selected_pair()
        if pair_id is None:
            return
        pair = self.store.pair(pair_id)
        if not pair:
            return
        self._show_conflicts_dialog(pair)

    def _open_conflicts_for_pair(self, pair_id):
        """Notification entry point: jump to the Folders tab, select the
        pair, and open its conflict review dialog. Returns False so it can
        run through GLib.idle_add."""
        for i, row in enumerate(self.pair_store):
            if row[0] == pair_id:
                self.nb.set_current_page(2)          # Folders tab
                self.pair_view.set_cursor(i)
                break
        pair = self.store.pair(pair_id)
        if pair:
            self._show_conflicts_dialog(pair)
        return False

    def _show_conflicts_dialog(self, pair):
        dlg = Gtk.Dialog(title=i18n.T("Conflicts — pair #{pid}",
                                      pid=pair["id"]),
                         transient_for=self.win, modal=True)
        dlg.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        dlg.set_default_size(680, 420)
        box = dlg.get_content_area()
        box.set_spacing(6)
        headers = [i18n.t("Conflict copy"), i18n.t("Original"),
                   i18n.t("Size"), i18n.t("On disk")]
        store_model, view, scroll = _list_view(
            headers, (str, str, str, str))
        box.pack_start(scroll, True, True, 0)
        buttons = Gtk.Box(spacing=6)
        btn_keep_copy = Gtk.Button()
        btn_keep_copy.set_label(i18n.t("Keep copy"))
        btn_keep_copy.set_tooltip_text(i18n.t(
            "The copy's content wins at the original path on both sides; "
            "the copy is removed"))
        btn_keep_orig = Gtk.Button()
        btn_keep_orig.set_label(i18n.t("Keep original"))
        btn_keep_orig.set_tooltip_text(i18n.t(
            "The original stays as it is; the copy is discarded on both "
            "sides"))
        btn_keep_both = Gtk.Button()
        btn_keep_both.set_label(i18n.t("Keep both"))
        btn_keep_both.set_tooltip_text(i18n.t(
            "Leave both files in place (mark nothing — this dialog just "
            "closes)"))
        for b in (btn_keep_copy, btn_keep_orig, btn_keep_both):
            buttons.pack_start(b, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        status = self._dim_label("")
        box.pack_start(status, False, False, 0)

        state = {"open": True, "busy": False}

        def bytes_txt(n):
            return _drive_bytes(n) or "?"

        def reload():
            store_model.clear()
            status.set_text(i18n.t("Loading\u2026"))

            def work():
                try:
                    return conflicts.list_conflicts(self.store, pair)
                except Exception as exc:
                    return {"error": str(exc)}

            def done(items):
                if not state["open"]:
                    return
                if isinstance(items, dict):
                    status.set_text(i18n.T(
                        "Could not list conflicts — {err}",
                        err=items.get('error', '?')))
                    return
                for it in items:
                    store_model.append([
                        it["path"], it["original"],
                        bytes_txt(it["size"]) if it["size"] is not None
                        else "?",
                        i18n.t("yes") if it["local_exists"]
                        else i18n.t("missing locally")])
                status.set_text(
                    i18n.t("No conflict copies — everything is resolved.")
                    if not items else
                    i18n.T("{n} conflict copy(ies) — select one and choose "
                           "which side to keep.", n=len(items)))

        def selected_copy():
            sel = view.get_selection().get_selected()
            if sel[1] is None:
                self._info(i18n.t("Select a conflict copy first."))
                return None
            return store_model[sel[1]][0]

        def resolve(choice):
            if state["busy"]:
                return
            copy_rel = selected_copy()
            if copy_rel is None:
                return
            if choice == "keep_both":
                dlg.destroy()
                return
            state["busy"] = True
            status.set_text(i18n.T("Resolving {path}\u2026", path=copy_rel))

            def work():
                drive = self._drive_for(pair["account_id"])
                conflicts.resolve_conflict(self.store, pair, drive,
                                           copy_rel, choice)
                return choice

            def done(_choice):
                state["busy"] = False
                if not state["open"]:
                    return
                status.set_text(i18n.t("Resolved."))
                reload()

            self._bg(work, done)

        btn_keep_copy.connect("clicked", lambda _b: resolve("keep_copy"))
        btn_keep_orig.connect("clicked", lambda _b: resolve("keep_original"))
        btn_keep_both.connect("clicked", lambda _b: resolve("keep_both"))
        dlg.show_all()
        reload()
        dlg.run()
        state["open"] = False
        dlg.destroy()
        self._refresh_pairs()

    # ---- Drive tab (on-demand downloads) ------------------------------
    def _drive_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      border_width=6)
        box.pack_start(self._tr_dim(
            "Everything in your Drive, downloadable on demand — like a "
            "virtual drive without the mount. Nothing here touches your "
            "sync pairs: chosen files land exactly where you point them."),
            False, False, 0)

        top = Gtk.Box(spacing=6)
        top.pack_start(self._tr_label("Account:"), False, False, 0)
        self.drive_acc_combo = Gtk.ComboBoxText()
        for a in self.store.accounts():
            self.drive_acc_combo.append_text(f"#{a['id']}  {a['email']}")
        if self.store.accounts():
            self.drive_acc_combo.set_active(0)
        self.drive_acc_combo.connect("changed",
                                     lambda _c: self._drive_refresh())
        top.pack_start(self.drive_acc_combo, False, False, 0)
        btn_refresh = self._tr_button("Refresh")
        btn_refresh.connect("clicked", lambda _b: self._drive_refresh())
        top.pack_start(btn_refresh, False, False, 0)
        box.pack_start(top, False, False, 0)

        self.drive_crumb = self._dim_label(i18n.t("My Drive"))
        box.pack_start(self.drive_crumb, False, False, 0)

        nav = Gtk.Box(spacing=6)
        btn_home = self._tr_button("My Drive")
        btn_home.set_tooltip_text(i18n.t("Jump back to the top of your Drive"))
        self._reg(btn_home, "set_tooltip_text",
                  "Jump back to the top of your Drive")
        btn_home.connect("clicked", lambda _b: self._drive_home())
        nav.pack_start(btn_home, False, False, 0)
        btn_up = self._tr_button("Up one level")
        btn_up.connect("clicked", lambda _b: self._drive_up())
        nav.pack_start(btn_up, False, False, 0)
        self.drive_filter = Gtk.SearchEntry()
        self.drive_filter.set_placeholder_text(i18n.t(
            "Filter this folder\u2026  (Enter searches all of Drive)"))
        self._reg(self.drive_filter, "set_placeholder_text",
                  "Filter this folder\u2026  (Enter searches all of Drive)")
        self.drive_filter.connect("search-changed",
                                  lambda _e: self._render_drive())
        self.drive_filter.connect("activate", self._on_drive_search)
        nav.pack_start(self.drive_filter, True, True, 0)
        box.pack_start(nav, False, False, 0)

        # hidden columns carry id, mimeType and the raw byte size so a
        # selected row can be downloaded without re-querying Drive
        headers = ["Name", "Kind", "Size", "Modified"]
        self.drive_store, self.drive_view, self.drive_scroll = _list_view(
            [i18n.t(h) for h in headers],
            (str, str, str, str, str, str, str))
        self._tr_columns(self.drive_view, headers)
        self.drive_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.drive_view.connect("row-activated", self._on_drive_row_activated)
        box.pack_start(self.drive_scroll, True, True, 0)

        # live download strip (spinner + one line): an on-demand download
        # must never be silent — the old build only blinked the headerbar
        # subtitle, so a running download looked like a frozen app
        prog = Gtk.Box(spacing=8)
        self.drive_spinner = Gtk.Spinner()
        prog.pack_start(self.drive_spinner, False, False, 0)
        self.drive_prog = Gtk.Label(label="")
        self.drive_prog.set_xalign(0.0)
        self.drive_prog.set_ellipsize(Pango.EllipsizeMode.END)
        self.drive_prog.get_style_context().add_class("drive-prog")
        prog.pack_start(self.drive_prog, True, True, 0)
        box.pack_start(prog, False, False, 0)

        row = Gtk.Box(spacing=6)
        btn_selall = self._tr_button("Select all")
        btn_selall.connect(
            "clicked",
            lambda _b: self.drive_view.get_selection().select_all())
        row.pack_start(btn_selall, False, False, 0)
        btn_selnone = self._tr_button("Select none")
        btn_selnone.connect(
            "clicked",
            lambda _b: self.drive_view.get_selection().unselect_all())
        row.pack_start(btn_selnone, False, False, 0)
        btn_dl = self._tr_button("Download selected\u2026")
        btn_dl.get_style_context().add_class("suggested-action")
        btn_dl.connect("clicked", self._on_drive_download_selected)
        row.pack_start(btn_dl, False, False, 0)
        btn_dlf = self._tr_button("Download this folder\u2026")
        btn_dlf.connect("clicked", self._on_drive_download_folder)
        row.pack_start(btn_dlf, False, False, 0)
        box.pack_start(row, False, False, 0)

        # busy counts RUNNING download jobs (not a bool): downloads may be
        # started concurrently and no click is ever silently swallowed
        self._drive_state = {"stack": [("root", "My Drive", "My Drive")],
                             "loading": False, "open": True, "busy": 0,
                             "loaded": False, "items": [], "search": None}
        # lazy: list Drive only when the user first opens the tab, so a
        # stale token or offline network never pops a dialog at launch
        self.nb.connect("switch-page", self._on_nb_switch)
        return _card(box)

    def _on_nb_switch(self, _nb, _page, page_num):
        if page_num == PAGE_DRIVE and not self._drive_state["loaded"]:
            self._drive_state["loaded"] = True
            self._drive_load()

    def _drive_account_id(self):
        accounts = self.store.accounts()
        if not accounts:
            return None
        idx = self.drive_acc_combo.get_active()
        return accounts[idx if idx >= 0 else 0]["id"]

    # ---- on-demand download progress strip --------------------------
    def _drive_prog_show(self, text, cls=""):
        """Update the Drive tab's live status line (GTK thread)."""
        ctx = self.drive_prog.get_style_context()
        for c in ("ok", "err"):
            ctx.remove_class(c)
        if cls:
            ctx.add_class(cls)
        self.drive_prog.set_text(text)

    def _drive_job_started(self):
        st = self._drive_state
        st["busy"] += 1
        if st["busy"] == 1:
            self.drive_spinner.start()
        self._drive_prog_show(
            i18n.T("{n} download(s) running\u2026", n=st['busy']))

    def _drive_job_ended(self, summary, cls="ok"):
        st = self._drive_state
        st["busy"] = max(0, st["busy"] - 1)
        if st["busy"] == 0:
            self.drive_spinner.stop()
            self._drive_prog_show(summary, cls)
        else:
            self._drive_prog_show(
                i18n.T("{summary}  \u00b7  {n} other download(s) still "
                       "running\u2026", summary=summary, n=st['busy']), cls)

    def _drive_refresh(self):
        """Reload the current account from the top (clears the filter)."""
        self.drive_filter.set_text("")
        self._drive_home()

    def _drive_home(self):
        self._drive_state["stack"] = [("root", "My Drive", "My Drive")]
        self._drive_load()

    def _drive_up(self):
        st = self._drive_state
        if st["loading"] or len(st["stack"]) < 2:
            return
        st["stack"].pop()
        self._drive_load()

    def _on_drive_search(self, _entry):
        """Enter in the filter box: search every folder in the account.

        Typing filters the current folder as it always did; pressing Enter
        lifts the same box to a Drive-wide `name contains` search (one API
        call). Refresh, Up or My Drive return to plain browsing.
        """
        st = self._drive_state
        if st["loading"] or not self.store.accounts():
            return
        needle = self.drive_filter.get_text().strip()
        if not needle:
            return
        acc_id = self._drive_account_id()
        st["loading"] = True
        st["search"] = needle
        self.drive_store.clear()
        st["items"] = []
        self.drive_crumb.set_text(
            i18n.T("Searching all of Drive for \u201c{q}\u201d\u2026", q=needle))

        def work():
            try:
                drive = self._drive_for(acc_id)
                return drive.search(needle, limit=100)
            except Exception as exc:
                return {"error": str(exc)}

        def done(res):
            if not st["open"]:
                return
            st["loading"] = False
            if isinstance(res, dict):        # error marker from work()
                st["search"] = None
                self.drive_crumb.set_text(
                    i18n.T("Search failed — {err}", err=res.get('error', '?')))
                return
            st["items"] = res
            self._render_drive()

        self._bg(work, done)

    def _render_drive(self):
        """Paint the current listing, applying the filter box."""
        st = self._drive_state
        items = st.get("items", [])
        needle = self.drive_filter.get_text().strip().lower() \
            if hasattr(self, "drive_filter") else ""
        visible = [it for it in items
                   if not needle
                   or needle in (it.get("name") or "").lower()]
        self.drive_store.clear()
        for item in visible:
            self.drive_store.append(_drive_row(item))
        if st.get("loading"):
            return
        if st.get("search"):
            self.drive_crumb.set_text(i18n.T(
                "Search \u201c{q}\u201d \u2014 {n} result(s) in all of Drive "
                "(double-click a folder to browse it)",
                q=st['search'], n=len(items)))
            return
        path = st["stack"][-1][2]
        if needle:
            self.drive_crumb.set_text(i18n.T(
                "{path} \u2014 {seen} of {total} match",
                path=path, seen=len(visible), total=len(items)))
        else:
            self.drive_crumb.set_text(i18n.T(
                "{path} \u2014 {n} item(s)", path=path, n=len(items)))

    def _drive_load(self):
        """Fill the Drive browser rows for the current stack position."""
        st = self._drive_state
        st["search"] = None        # back to browse mode (a search was lifted)
        if not self.store.accounts():
            self.drive_crumb.set_text(i18n.t(
                "No account yet — authorize one on the Accounts tab first."))
            self.drive_store.clear()
            st["items"] = []
            return
        fid, _name, path = st["stack"][-1]
        self.drive_crumb.set_text(path)
        self.drive_store.clear()
        st["items"] = []
        st["loading"] = True

        def work():
            try:
                drive = self._drive_for(self._drive_account_id())
                if fid == "shared":
                    return drive.list_shared_folders()
                return drive.list_children(fid)
            except Exception as exc:
                return {"error": str(exc)}

        def done(res):
            if not st["open"]:
                return
            st["loading"] = False
            if isinstance(res, dict):        # error marker from work()
                # shown in the list area, not a modal: a broken token or
                # offline network must not hijack the app at tab switch
                self.drive_crumb.set_text(
                    f"Drive listing failed — {res.get('error', '?')}")
                return
            rows = ([{"id": "shared", "name": i18n.t("Shared with me"),
                      "mimeType": "application/vnd.google-apps.folder"}]
                    if fid == "root" else [])
            st["items"] = rows + sorted(res, key=lambda x: (
                0 if x.get("mimeType") == "application/vnd.google-apps.folder"
                or (x.get("shortcutDetails") or {}).get("targetMimeType")
                == "application/vnd.google-apps.folder" else 1,
                x.get("name", "").lower()))
            self._render_drive()

        self._bg(work, done)

    def _on_drive_row_activated(self, _view, treepath, _col):
        st = self._drive_state
        if st["loading"]:
            return
        row = self.drive_store[treepath]
        name, kind, _size, _mod, fid = row[0], row[1], row[2], row[3], row[4]
        if kind == i18n.t("Folder"):
            if fid == "shared":
                st["stack"].append(("shared", i18n.t("Shared with me"),
                                    i18n.t("Shared with me")))
            else:
                st["stack"].append((fid, name,
                                    f"{st['stack'][-1][2]}/{name}"))
            self._drive_load()
            return
        # double-click on a file = quick download to a picked destination
        entry = {"id": fid, "name": name, "mimeType": row[5],
                 "size": row[6]}
        self._drive_download_entries([entry])

    def _on_drive_download_selected(self, _btn):
        st = self._drive_state
        if st["loading"]:
            return
        model, paths = self.drive_view.get_selection().get_selected_rows()
        entries, folders = [], 0
        for path in paths:
            row = self.drive_store[path]
            if row[1] == i18n.t("Folder"):
                folders += 1
                continue
            entries.append({"id": row[4], "name": row[0],
                            "mimeType": row[5], "size": row[6]})
        if folders:
            self._info(i18n.T(
                "{folders} folder(s) in the selection were skipped — use "
                "\u201cDownload this folder\u2026\u201d for those.",
                folders=folders))
        if not entries:
            if not folders:
                self._info(i18n.t(
                    "Select files to download (or double-click one)."))
            return
        self._drive_download_entries(entries)

    def _drive_download_entries(self, entries):
        """Pick destination(s) and fetch the given file entries on demand."""
        if not self.store.accounts():
            self._error(i18n.t("No account yet — authorize one on the "
                               "Accounts tab first."))
            return
        if len(entries) == 1:
            dlg = Gtk.FileChooserDialog(
                title=i18n.t("Save file"), transient_for=self.win,
                modal=True, action=Gtk.FileChooserAction.SAVE)
            dlg.add_buttons(i18n.t("Cancel"), Gtk.ResponseType.CANCEL,
                            i18n.t("Save"), Gtk.ResponseType.ACCEPT)
            dlg.set_current_name(entries[0]["name"])
            dlg.set_do_overwrite_confirmation(True)
        else:
            dlg = Gtk.FileChooserDialog(
                title=i18n.T("Choose where to save {n} files",
                             n=len(entries)),
                transient_for=self.win, modal=True,
                action=Gtk.FileChooserAction.SELECT_FOLDER)
            dlg.add_buttons(i18n.t("Cancel"), Gtk.ResponseType.CANCEL,
                            i18n.t("Select"), Gtk.ResponseType.ACCEPT)
        dest = dlg.get_filename() if dlg.run() == Gtk.ResponseType.ACCEPT \
            else None
        dlg.destroy()
        if not dest:
            return
        acc_id = self._drive_account_id()
        export_map = resolve_export_formats(self.cfg)
        total = len(entries)
        self.hb.set_subtitle(
            i18n.T("downloading {n} file(s)\u2026", n=total))
        self._drive_job_started()

        def work():
            drive = self._drive_for(acc_id)
            ok, failed = [], []
            for i, entry in enumerate(entries, 1):
                target = dest if len(entries) == 1 \
                    else os.path.join(dest, entry["name"])
                GLib.idle_add(self._drive_prog_show,
                              i18n.T("downloading {i}/{n}: {name}",
                                     i=i, n=total, name=entry['name']))
                try:
                    written = engine.download_item(drive, entry, target,
                                                   export_map)
                    ok.append(os.path.basename(written))
                    self.store.log(None, "ondemand-download",
                                   entry["name"],
                                   f"saved to {os.path.dirname(written)}")
                except engine.OnDemandSkip as exc:
                    failed.append(str(exc))
                    self.store.log(None, "ondemand-skip", entry["name"],
                                   str(exc))
                except Exception as exc:
                    failed.append(f"{entry['name']}: {exc}")
                    self.store.log(None, "ondemand-error", entry["name"],
                                   str(exc), level="error")
                GLib.idle_add(self._drive_prog_show,
                              i18n.T("{done}/{total} done, {failed} failed "
                                     "so far\u2026", done=len(ok),
                                     total=total, failed=len(failed)))
            return ok, failed

        def done(result):
            ok, failed = result
            self._refresh_activity()
            if failed and not ok:
                summary = i18n.T("Download failed \u2014 {err}",
                                 err=failed[0])
                cls = "err"
            elif failed:
                summary = i18n.T(
                    "Downloaded {ok}, {failed} failed \u2192 {dest} "
                    "(details in Activity)", ok=len(ok), failed=len(failed),
                    dest=dest)
                cls = "err"
            else:
                summary = i18n.T("Downloaded {n} file(s) \u2192 {dest}",
                                 n=len(ok), dest=dest)
                cls = "ok"
            self.hb.set_subtitle(summary)
            self._drive_job_ended(summary, cls)
            if failed:
                # failures stay silent otherwise — surface them modally
                lines = [i18n.T("Failed/skipped:\n  {list}",
                                list="\n  ".join(failed[:8]))]
                if ok:
                    lines.insert(0, i18n.T(
                        "Downloaded {n} file(s) \u2192 {dest}",
                        n=len(ok), dest=dest))
                self._info("\n\n".join(lines))

        self._bg(work, done)

    def _on_drive_download_folder(self, _btn):
        st = self._drive_state
        if st["loading"]:
            return
        fid, fname, fpath = st["stack"][-1]
        if fid == "shared":
            self._info(i18n.t("Open a concrete folder first (double-click "
                              "one in \u201cShared with me\u201d)."))
            return
        if not self.store.accounts():
            self._error(i18n.t("No account yet — authorize one on the "
                               "Accounts tab first."))
            return
        dlg = Gtk.FileChooserDialog(
            title=i18n.t("Choose where to save the folder contents"),
            transient_for=self.win, modal=True,
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.add_buttons(i18n.t("Cancel"), Gtk.ResponseType.CANCEL,
                        i18n.t("Select"), Gtk.ResponseType.ACCEPT)
        dlg.set_current_name(fname)
        dest = dlg.get_filename() if dlg.run() == Gtk.ResponseType.ACCEPT \
            else None
        dlg.destroy()
        if not dest:
            return
        acc_id = self._drive_account_id()
        export_map = resolve_export_formats(self.cfg)
        self.hb.set_subtitle(i18n.T("downloading {path}…", path=fpath))
        self._drive_job_started()

        def ondemand_log(rel, message, level="info"):
            self.store.log(None, "ondemand-error" if level == "error"
                           else "ondemand-download", rel, message,
                           level=level)

        def ondemand_progress(done_count, total, rel, kind):
            GLib.idle_add(self._drive_prog_show,
                          i18n.T("downloading {path}: {done}/{total} — "
                                 "{rel}", path=fpath, done=done_count,
                                 total=total, rel=rel))

        def work():
            drive = self._drive_for(acc_id)
            return engine.download_tree(
                drive, fid, dest, export_map=export_map,
                concurrency=max(1, _cfg_int(self.cfg,
                                            "transfer_concurrency", 4)),
                log=ondemand_log, progress=ondemand_progress)

        def done(stats):
            summary = i18n.T(
                "Downloaded {n} file(s) ({bytes}), skipped {skipped}, "
                "errors {errors} — {dest}",
                n=stats['downloaded'], bytes=_drive_bytes(stats['bytes']),
                skipped=stats['skipped'], errors=stats['errors'], dest=dest)
            cls = "err" if (stats["errors"] and not stats["downloaded"]) \
                else "ok"
            self.hb.set_subtitle(summary)
            self._drive_job_ended(summary, cls)
            self._refresh_activity()
            self._info(
                i18n.T("Downloaded {n} file(s) ({bytes}), skipped "
                       "{skipped}, errors {errors} — {dest}",
                       n=stats['downloaded'],
                       bytes=_drive_bytes(stats['bytes']),
                       skipped=stats['skipped'], errors=stats['errors'],
                       dest=dest))

        self._bg(work, done)

    # ---- Activity tab -------------------------------------------------
    def _activity_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      border_width=6)
        row = Gtk.Box(spacing=6)
        self.lbl_status = Gtk.Label(xalign=0)
        self.lbl_status.get_style_context().add_class("hint")
        row.pack_start(self.lbl_status, True, True, 0)
        btn = self._tr_button("Sync all now")
        btn.get_style_context().add_class("suggested-action")
        btn.connect("clicked", self._on_sync_all)
        row.pack_start(btn, False, False, 0)
        self.btn_pause = Gtk.Button()
        self.btn_pause.set_label(i18n.t("Pause"))
        self._reg(self.btn_pause, "set_label", "Pause")
        self.btn_pause.set_tooltip_text(i18n.t(
            "Pause or resume background sync passes (the dashboard stays up)"))
        self._reg(self.btn_pause, "set_tooltip_text",
                  "Pause or resume background sync passes (the dashboard "
                  "stays up)")
        self.btn_pause.connect("clicked", self._on_pause_toggle)
        row.pack_start(self.btn_pause, False, False, 0)
        row.pack_start(self._tr_label("Interval (s):"), False, False, 0)
        self.spin = Gtk.SpinButton.new_with_range(5, 86400, 5)
        self.spin.set_value(_cfg_int(self.cfg, "sync_interval", 30))
        row.pack_start(self.spin, False, False, 0)
        apply_btn = self._tr_button("Apply")
        apply_btn.connect("clicked", self._on_apply_interval)
        row.pack_start(apply_btn, False, False, 0)
        box.pack_start(row, False, False, 0)

        srow = Gtk.Box(spacing=6)
        self.ent_search = Gtk.SearchEntry()
        self.ent_search.set_placeholder_text(i18n.t(
            "Search path, action or detail\u2026"))
        self._reg(self.ent_search, "set_placeholder_text",
                  "Search path, action or detail\u2026")
        self.ent_search.connect("search-changed", self._on_activity_filter)
        srow.pack_start(self.ent_search, True, True, 0)
        box.pack_start(srow, False, False, 0)

        # filter / sort bar — combos re-list instantly; Clear wipes the log
        frow = Gtk.Box(spacing=6)
        self.cmb_action = Gtk.ComboBoxText()
        self._action_items = None      # filled by _sync_action_combo below
        self.cmb_action.set_tooltip_text(i18n.t("Filter by action"))
        self._reg(self.cmb_action, "set_tooltip_text", "Filter by action")
        self.cmb_action.connect("changed", self._on_activity_filter)
        frow.pack_start(self.cmb_action, False, False, 0)
        self.cmb_level = Gtk.ComboBoxText()
        self._level_items = ["All levels", "info", "conflict", "error"]
        self.cmb_level.set_tooltip_text(i18n.t("Filter by level"))
        self._reg(self.cmb_level, "set_tooltip_text", "Filter by level")
        self.cmb_level.connect("changed", self._on_activity_filter)
        frow.pack_start(self.cmb_level, False, False, 0)
        self.cmb_time = Gtk.ComboBoxText()
        self._time_items = ["Any time", "Last 15 minutes", "Last hour",
                            "Last 24 hours", "Last 7 days"]
        self.cmb_time.set_tooltip_text(i18n.t("Time frame"))
        self._reg(self.cmb_time, "set_tooltip_text", "Time frame")
        self.cmb_time.connect("changed", self._on_activity_filter)
        frow.pack_start(self.cmb_time, False, False, 0)
        self.cmb_sort = Gtk.ComboBoxText()
        self._sort_items = ["Newest first", "Oldest first"]
        self.cmb_sort.set_tooltip_text(i18n.t("Sort order"))
        self._reg(self.cmb_sort, "set_tooltip_text", "Sort order")
        self.cmb_sort.connect("changed", self._on_activity_filter)
        frow.pack_start(self.cmb_sort, False, False, 0)
        btn_clear = self._tr_button("Clear activity\u2026")
        btn_clear.get_style_context().add_class("destructive-action")
        btn_clear.connect("clicked", self._on_clear_activity)
        frow.pack_end(btn_clear, False, False, 0)
        box.pack_start(frow, False, False, 0)

        # the 6th (hidden) store column keeps the raw UTC ts so the row the
        # user is reading can be re-found after each refresh repaint
        headers = ["Time", "Level", "Action", "Path", "Detail"]
        self.evt_store, self.evt_view, self.evt_scroll = _list_view(
            [i18n.t(h) for h in headers],
            (str, str, str, str, str, str))
        self._tr_columns(self.evt_view, headers)
        self.evt_view.connect("row-activated", self._on_activity_activated)
        box.pack_start(self.evt_scroll, True, True, 0)
        self._action_items = ["All actions"]

        def refill_fixed_combos():
            """Re-fill the fixed-item combos after a language switch,
            preserving the active index of each."""
            for combo, items in (
                    (self.cmb_level, self._level_items),
                    (self.cmb_time, self._time_items),
                    (self.cmb_sort, self._sort_items)):
                idx = combo.get_active()
                combo.remove_all()
                for it in items:
                    combo.append_text(i18n.t(it))
                combo.set_active(idx if idx >= 0 else 0)

        self._reg_rebuilder(refill_fixed_combos)
        refill_fixed_combos()

        self._refresh_activity()
        return _card(box)

    def _on_sync_all(self, _btn):
        self._bg(lambda: self.worker.run_pass(),
                 lambda _result: (self._refresh_activity(),
                                  self._refresh_overview()))

    def _on_pause_toggle(self, _btn=None):
        if self.worker.status.get("paused"):
            self.worker.resume()
        else:
            self.worker.pause()
        self._refresh_activity()
        return False

    def _on_activity_activated(self, _view, path, _col):
        """Double-click/Enter: show the full, selectable log entry."""
        row = self.evt_store[path]
        dlg = Gtk.Dialog(title=i18n.t("Activity detail"),
                         transient_for=self.win, modal=True)
        dlg.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        dlg.set_default_size(560, 320)
        em = "\u2014"
        text = (f"{row[0]} {em} {row[1]}\n"
                f"{i18n.t('Action')}: {row[2]}\n"
                f"{i18n.t('Path')}: {row[3] or em}\n\n"
                f"{row[4] or i18n.t('No detail.')}")
        lbl = Gtk.Label(label=text, selectable=True, wrap=True, xalign=0,
                        yalign=0)
        lbl.set_max_width_chars(72)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(lbl)
        dlg.get_content_area().pack_start(sw, True, True, 10)
        dlg.show_all()
        dlg.run()
        dlg.destroy()

    def _on_apply_interval(self, _btn):
        try:
            self.cfg = update_config(sync_interval=int(self.spin.get_value()))
        except (ValueError, TypeError) as exc:
            self._error(str(exc))
            return
        # adopt the new interval without stopping the worker (stop() joins
        # the running pass and could freeze the GTK thread for seconds)
        self.worker.apply_config(self.cfg)
        self.worker.wake.set()
        self._info(i18n.T("Sync interval set to {sec}s.",
                          sec=self.cfg['sync_interval']))

    def _refresh_activity(self, *_args):
        s = self.worker.status
        last = i18n.t("never") if not s["last_run"] else \
            i18n.T("{sec}s ago", sec=int(max(0, time.time() - s['last_run'])))
        stats = ""
        if s["last_stats"]:
            st = s["last_stats"]
            stats = i18n.T(
                " \u2014 last pass pair #{pid}: pushed {p}, pulled {q}, "
                "deleted {d}, conflicts {c}",
                pid=st['pair_id'], p=st['pushed'], q=st['pulled'],
                d=st['deleted'], c=st['conflicts'])
        err = i18n.T(" \u2014 ERROR: {err}", err=s['last_error']) \
            if s["last_error"] else ""
        state = i18n.t("paused" if s["paused"] else "running")
        self.lbl_status.set_text(i18n.T(
            "Worker {state} \u2014 last pass: {last}{stats}{err}",
            state=state, last=last, stats=stats, err=err))
        ctx = self.lbl_status.get_style_context()
        if s["last_error"]:
            ctx.add_class("error-text")
        else:
            ctx.remove_class("error-text")
        self.btn_pause.set_label(
            i18n.t("Resume" if s["paused"] else "Pause"))
        if hasattr(self, "btn_global_pause"):
            self.btn_global_pause.set_label(
                i18n.t("Resume sync" if s["paused"] else "Pause sync"))
        self._sync_tray_pause_label(bool(s["paused"]))
        countdown = ""
        nxt = s.get("next_run")
        if not s["paused"] and nxt:
            remaining = int(max(0, nxt - time.time()))
            countdown = i18n.T(" \u2014 next sync in {sec}s", sec=remaining)
        self.hb.set_subtitle(i18n.T(
            "sync {state}{countdown} \u2014 last pass {last}",
            state=state, countdown=countdown, last=last))

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
        needle = self.ent_search.get_text().strip().lower()
        events = self.store.recent_events(
            limit=500 if needle else 200,
            level=None if level in (None, i18n.t("All levels")) else level,
            action=None if action in (None, i18n.t("All actions"))
            else action,
            since=since)
        if needle:
            events = [
                e for e in events
                if needle in (e["rel_path"] or "").lower()
                or needle in (e["message"] or "").lower()
                or needle in (e["action"] or "").lower()
                or needle in (e["level"] or "").lower()]
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
        items = [i18n.t("All actions")] + actions
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
            self._info(i18n.t("The activity log is already empty."))
            return
        if self._confirm(i18n.T("Delete all {n} activity entries? This "
                                "cannot be undone.", n=n)):
            self.store.clear_events()
            self._refresh_activity()

    def _tick(self):
        """Periodic UI refresh while the app is alive (even when hidden)."""
        if self._closing:
            return False
        self._refresh_activity()
        self._refresh_pair_status_cells()
        self._refresh_overview()
        self._check_problems()
        return True

    def _refresh_pair_status_cells(self):
        """Update the Folders tab's 'Last sync' cells in place.

        A full _refresh_pairs() repaint every tick would clear the user's
        row selection mid-click; only the status column changes between
        passes, so rewrite those cells (row[6]) directly.
        """
        if not hasattr(self, "pair_store"):
            return
        for row in self.pair_store:
            text = self._pair_last_sync_text(row[0])
            if row[6] != text:
                row[6] = text

    def _check_problems(self):
        """Desktop notification when new conflicts/errors arrive while the
        window is hidden to the tray. Best-effort libnotify (no session bus
        or missing GIR just disables it); drivesync.notify.Gate keeps the
        rate sane and never repeats what the user already saw in the app.
        When the problems include a conflict for a known pair, the
        notification carries a 'Review conflicts' button that opens the
        pair's review dialog."""
        rows = self.store.recent_events(limit=50)
        problem_rows = notify.problems(rows)
        body = self._notify_gate.consider(
            problem_rows, rows[0]["ts"] if rows else None)
        if not body:
            return
        action_pair = None
        for r in problem_rows:
            if r["level"] == "conflict" and r["pair_id"] \
                    and self.store.pair(r["pair_id"]):
                action_pair = r["pair_id"]
                break
        if action_pair is None:
            notify.send(i18n.t("DriveSync — sync problems"), body)
            return
        notify.send(i18n.t("DriveSync — sync problems"), body,
                    action_label=i18n.t("Review conflicts"),
                    on_action=lambda: GLib.idle_add(
                        self._open_conflicts_for_pair, action_pair))

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
            if self._listener is not None:
                self._listener.stop()
        except Exception:
            pass
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
        # set_current_page only works once the window is shown (GTK >=
        # 3.24.49 ignores it pre-show) — apply the startup tab here
        self.nb.set_current_page(self._startup_page)


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
                            text=i18n.t("DriveSync is already running"))
    dlg.format_secondary_text(i18n.t(
        "Use the DriveSync tray icon to open the window, sync or quit."))
    dlg.run()
    dlg.destroy()


def _launch_failure():
    """The window could not be built: show why instead of dying silently.

    Menu launches have no terminal, so a bare traceback would vanish
    unseen. Print it, append it to gui-crash.log in the data dir and show
    the tail in a dialog (best effort: without a display even that fails,
    but the log file remains for 'drivesync doctor'-style debugging).
    """
    text = traceback.format_exc()
    print(text, file=sys.stderr)
    log_path = None
    try:
        app_dir().mkdir(parents=True, exist_ok=True)
        log_path = app_dir() / "gui-crash.log"
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n[{stamp}]\n{text}")
    except OSError:
        pass
    tail = "\n".join(text.strip().splitlines()[-6:])
    try:
        dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.CLOSE,
                                text=i18n.t("DriveSync could not start"))
        dlg.format_secondary_text(
            tail + (f"\n\nFull traceback: {log_path}" if log_path else ""))
        dlg.run()
        dlg.destroy()
    except Exception:
        pass


def run():
    try:
        lock = _instance_lock()
    except Exception:
        _launch_failure()
        return
    cfg = load_config()
    i18n.set_language(cfg.get("language", "en"))
    _apply_css(cfg.get("theme", "system"), cfg.get("accent", "blue"))
    if lock is None:
        # Another GUI instance owns the lock. If it is resident in the tray,
        # show its window; otherwise explain why this launch did nothing.
        if instance.request_show():
            return
        _already_running_notice()
        return
    try:
        app = DesktopApp()
    except Exception:
        _launch_failure()
        return
    app.show()
    Gtk.main()
