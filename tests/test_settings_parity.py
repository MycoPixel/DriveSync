"""The web Settings page and the desktop Options must stay in step.

Both are thin editors over config.json, so drift shows up as an option that
exists in one UI only (parallel transfers was web-only), or as a key a UI
writes that save_config() does not know and silently drops (the GUI's
Transfers section never saved anything). This reads the two sources with
ast/regex — no GTK needed — so it also runs in CI.
"""

import ast
import re
from pathlib import Path

from drivesync.config import DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parents[1] / "drivesync"

# GUI handlers that persist the sync/transfer/network options. Appearance,
# tray and autostart are desktop-only and live in other handlers.
GUI_HANDLERS = {"_on_schedule_changed", "_on_digest_changed",
                "_on_transfer_option", "_on_advanced_changed",
                "_on_apply_interval"}


def _web_keys():
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    start = js.index('$("#settingsForm").addEventListener("submit"')
    end = js.index('setMsg("#settingsMsg", t("Saved.")', start)
    body = js[start:end]
    return set(re.findall(r"^\s{8}(\w+):", body, flags=re.M))


def _gui_keys():
    """Config keys named in the GUI's save handlers.

    Handlers build their update with loops and dict literals as well as
    keyword arguments (``for ent, key in ((w, "active_from"), ...)``), so
    rather than model every shape this takes each string literal in those
    functions that is a real config key. The handlers only write settings,
    so a mention is a write.
    """
    tree = ast.parse((ROOT / "gui.py").read_text(encoding="utf-8"))
    keys = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name in GUI_HANDLERS:
            keys.update(
                node.value for node in ast.walk(fn)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in DEFAULT_CONFIG)
            keys.update(k.arg for node in ast.walk(fn)
                        if isinstance(node, ast.Call)
                        for k in node.keywords
                        if k.arg in DEFAULT_CONFIG)
    return keys


def test_the_extractors_find_something():
    # guards against the regexes/AST walk silently matching nothing
    assert len(_web_keys()) >= 20
    assert len(_gui_keys()) >= 20


def test_web_and_gui_expose_the_same_options():
    web, gui = _web_keys(), _gui_keys()
    assert not web - gui, f"web-only options (add them to the GUI): {sorted(web - gui)}"
    assert not gui - web, f"GUI-only options (add them to the web page): {sorted(gui - web)}"


def test_every_option_a_ui_writes_survives_save_config():
    """save_config drops keys that are not in DEFAULT_CONFIG."""
    unknown = (_web_keys() | _gui_keys()) - set(DEFAULT_CONFIG)
    assert not unknown, f"written by a UI but dropped on save: {sorted(unknown)}"
