"""Look and feel for the GTK app: themes, accent colors and the CSS builder.

The base themes and the decorative styles are separate choices, and every
decorative style ships in both light and dark:

  base themes
  -----------
  light          the original Material 3 palette
  dark           a proper dark palette (native widgets follow via the
                 gtk-application-prefer-dark-theme flag)
  system         follows the desktop's light/dark preference (GNOME
                 color-scheme, falling back to GTK_THEME / desktop hints)

  style variants
  --------------
  glass-light    "liquid glass": translucent layered panels over a light
                 gradient (GTK3 CSS has no backdrop blur, so the frost comes
                 from layered alpha, large radii and hairline borders)
  glass-dark     the same liquid-glass language over a deep gradient
  graphite-light near-monochrome light greys with hairline borders
  graphite-dark  near-monochrome charcoal with hairline borders

The accent color tints every action button, the focused entry border, the
checked tab and the list-selection highlight. The module imports GTK only
inside apply() — building CSS stays pure so it is testable headless.
"""

import os
import sys

# Theme keys are grouped for the UI: base themes first, then the decorative
# styles (each one has an explicit light and dark variant).
BASE_THEMES = ("system", "light", "dark")
STYLE_THEMES = ("glass-light", "glass-dark",
                "graphite-light", "graphite-dark")
THEMES = BASE_THEMES + STYLE_THEMES

# Configs written before the light/dark split keep working. They map to the
# variant they used to be (both were dark-only).
LEGACY_THEMES = {"glass": "glass-dark", "graphite": "graphite-dark"}

# Resolved palettes that need the dark GTK flag.
DARK_THEMES = ("dark", "glass-dark", "graphite-dark")

ACCENTS = {
    # strong/pressed: action buttons; soft/ink: checked tab + selection in
    # light themes; rgba: translucent selection for the glass theme
    "blue":   {"strong": "#0B57D0", "pressed": "#0842A0", "soft": "#C2E7FF",
               "ink": "#001D35", "rgba": "rgba(11,87,208,0.35)"},
    "teal":   {"strong": "#00796B", "pressed": "#005C50", "soft": "#B2DFDB",
               "ink": "#00201C", "rgba": "rgba(0,121,107,0.35)"},
    "green":  {"strong": "#188038", "pressed": "#106328", "soft": "#C8E6C9",
               "ink": "#0B2313", "rgba": "rgba(24,128,56,0.35)"},
    "purple": {"strong": "#7B1FA2", "pressed": "#5E1780", "soft": "#E1BEE7",
               "ink": "#2A0E35", "rgba": "rgba(123,31,162,0.35)"},
    "pink":   {"strong": "#C2185B", "pressed": "#95134A", "soft": "#F8BBD0",
               "ink": "#33091A", "rgba": "rgba(194,24,91,0.35)"},
    "orange": {"strong": "#E65100", "pressed": "#A83A00", "soft": "#FFE0B2",
               "ink": "#2B1300", "rgba": "rgba(230,81,0,0.35)"},
    "red":    {"strong": "#B3261E", "pressed": "#871A15", "soft": "#F9C7C4",
               "ink": "#2C0C0A", "rgba": "rgba(179,38,30,0.35)"},
}

# swatch chips for the Options tab (theme-independent)
_SWATCH_CSS = """
button.swatch { padding: 0; min-width: 30px; min-height: 26px;
  border-radius: 10px; border: 2px solid transparent; }
button.swatch-blue   { background-color: #0B57D0; }
button.swatch-teal   { background-color: #00796B; }
button.swatch-green  { background-color: #188038; }
button.swatch-purple { background-color: #7B1FA2; }
button.swatch-pink   { background-color: #C2185B; }
button.swatch-orange { background-color: #E65100; }
button.swatch-red    { background-color: #B3261E; }
button.swatch.selected {
  border: 2px solid #FFFFFF;
  box-shadow: 0 0 0 2px rgba(0,0,0,0.45);
}
"""

_PALETTES = {
    "light": {
        "win_bg": "background-color: #F8FAFD;",
        "headerbar_bg": "background-color: #E9EEF6;",
        "header_text": "#1F1F1F",
        "subtitle": "#444746",
        "card_bg": "#FFFFFF",
        "border": "#E1E8EF",
        "text": "#202124",
        "muted": "#444746",
        "dim": "#5E5E5E",
        "entry_bg": "#FFFFFF",
        "entry_border": "#C4C7C5",
        "btn_bg": "#E9EEF6",
        "btn_text": "${a_strong}",
        "btn_hover": "#DCE5F2",
        "btn_active": "#CFDAEA",
        "tab_hover": "#E9EEF6",
        "tree_hover": "#F0F4F9",
        "header_btn_bg": "#F0F4F9",
        "sel_backdrop_bg": "#D8E2EF",
        "sel_backdrop_ink": "#001D35",
        "sel_bg": "${a_soft}",
        "sel_ink": "${a_ink}",
        "tv_bg": "#FFFFFF",
        "card_radius": "16px",
        "ok": "#146C2E",
    },
    "dark": {
        "win_bg": "background-color: #131922;",
        "headerbar_bg": "background-color: #1A212B;",
        "header_text": "#F2F6FC",
        "subtitle": "#9AA6B5",
        "card_bg": "#1E2530",
        "border": "#2A323E",
        "text": "#E7ECF3",
        "muted": "#9AA6B5",
        "dim": "#8A94A3",
        "entry_bg": "#171D26",
        "entry_border": "#3A434F",
        "btn_bg": "#232B37",
        "btn_text": "#DCE6F2",
        "btn_hover": "#2C3542",
        "btn_active": "#343E4C",
        "tab_hover": "#232B37",
        "tree_hover": "#232B37",
        "header_btn_bg": "#232B37",
        "sel_backdrop_bg": "#242D3A",
        "sel_backdrop_ink": "#E7ECF3",
        "sel_bg": "${a_strong}",
        "sel_ink": "#FFFFFF",
        "tv_bg": "#1E2530",
        "card_radius": "16px",
        "ok": "#5FDD8C",
    },
    "glass-light": {
        "win_bg": "background-image: linear-gradient(160deg, #EEF5FF 0%, "
                  "#DCE8F8 52%, #F7FAFF 100%);",
        "headerbar_bg": "background-color: rgba(255,255,255,0.55);",
        "header_text": "#10233F",
        "subtitle": "rgba(16,35,63,0.68)",
        "card_bg": "rgba(255,255,255,0.58)",
        "border": "rgba(16,35,63,0.14)",
        "text": "#10233F",
        "muted": "rgba(16,35,63,0.68)",
        "dim": "rgba(16,35,63,0.55)",
        "entry_bg": "rgba(255,255,255,0.68)",
        "entry_border": "rgba(16,35,63,0.22)",
        "btn_bg": "rgba(255,255,255,0.50)",
        "btn_text": "#10233F",
        "btn_hover": "rgba(255,255,255,0.72)",
        "btn_active": "rgba(255,255,255,0.88)",
        "tab_hover": "rgba(255,255,255,0.45)",
        "tree_hover": "rgba(16,35,63,0.06)",
        "header_btn_bg": "rgba(255,255,255,0.55)",
        "sel_backdrop_bg": "${a_soft}",
        "sel_backdrop_ink": "${a_ink}",
        "sel_bg": "${a_soft}",
        "sel_ink": "${a_ink}",
        "tv_bg": "rgba(255,255,255,0.35)",
        "card_radius": "20px",
        "ok": "#0F7A3D",
    },
    "glass-dark": {
        "win_bg": "background-image: linear-gradient(160deg, #3A4A66 0%, "
                  "#1B2432 55%, #10141C 100%);",
        "headerbar_bg": "background-color: rgba(255,255,255,0.07);",
        "header_text": "#F4F8FF",
        "subtitle": "rgba(233,240,255,0.72)",
        "card_bg": "rgba(255,255,255,0.10)",
        "border": "rgba(255,255,255,0.16)",
        "text": "#F2F6FF",
        "muted": "rgba(233,240,255,0.75)",
        "dim": "rgba(233,240,255,0.60)",
        "entry_bg": "rgba(8,12,20,0.35)",
        "entry_border": "rgba(255,255,255,0.24)",
        "btn_bg": "rgba(255,255,255,0.10)",
        "btn_text": "#F2F6FF",
        "btn_hover": "rgba(255,255,255,0.17)",
        "btn_active": "rgba(255,255,255,0.24)",
        "tab_hover": "rgba(255,255,255,0.10)",
        "tree_hover": "rgba(255,255,255,0.08)",
        "header_btn_bg": "rgba(255,255,255,0.10)",
        "sel_backdrop_bg": "${a_rgba}",
        "sel_backdrop_ink": "#FFFFFF",
        "sel_bg": "${a_rgba}",
        "sel_ink": "#FFFFFF",
        "tv_bg": "rgba(255,255,255,0.03)",
        "card_radius": "20px",
        "ok": "#7CEBA8",
    },
    "graphite-light": {
        "win_bg": "background-color: #F5F5F7;",
        "headerbar_bg": "background-color: #ECECEF;",
        "header_text": "#1D1D20",
        "subtitle": "#5F5F66",
        "card_bg": "#FFFFFF",
        "border": "#DCDCE1",
        "text": "#1D1D20",
        "muted": "#5F5F66",
        "dim": "#717178",
        "entry_bg": "#FFFFFF",
        "entry_border": "#C9C9CF",
        "btn_bg": "#ECECEF",
        "btn_text": "#26262A",
        "btn_hover": "#E2E2E6",
        "btn_active": "#D7D7DC",
        "tab_hover": "#ECECEF",
        "tree_hover": "#F0F0F3",
        "header_btn_bg": "#F0F0F3",
        "sel_backdrop_bg": "#E4E4E8",
        "sel_backdrop_ink": "#1D1D20",
        "sel_bg": "${a_soft}",
        "sel_ink": "${a_ink}",
        "tv_bg": "#FFFFFF",
        "card_radius": "12px",
        "ok": "#146C2E",
    },
    "graphite-dark": {
        "win_bg": "background-color: #1B1B1D;",
        "headerbar_bg": "background-color: #242427;",
        "header_text": "#ECECEF",
        "subtitle": "#A2A2A9",
        "card_bg": "#26262A",
        "border": "#343438",
        "text": "#E6E6EA",
        "muted": "#A2A2A9",
        "dim": "#8F8F96",
        "entry_bg": "#202023",
        "entry_border": "#3D3D42",
        "btn_bg": "#303034",
        "btn_text": "#DADADF",
        "btn_hover": "#3A3A3F",
        "btn_active": "#44444A",
        "tab_hover": "#303034",
        "tree_hover": "#303034",
        "header_btn_bg": "#303034",
        "sel_backdrop_bg": "#2C2C31",
        "sel_backdrop_ink": "#E6E6EA",
        "sel_bg": "${a_strong}",
        "sel_ink": "#FFFFFF",
        "tv_bg": "#26262A",
        "card_radius": "12px",
        "ok": "#6FCF8E",
    },
}

# one template for every theme: tokens (${name}) are replaced per palette
_CSS_TEMPLATE = """
window { ${win_bg} color: ${text}; }
headerbar { ${headerbar_bg} border: none; box-shadow: none; }
/* .title/.subtitle are style classes on the headerbar's labels, not
   pseudo-classes: GTK3's parser aborts on `headerbar:subtitle`
   ("Invalid name of pseudo-class") and the window never opens */
headerbar .title { font-weight: 700; font-size: 105%; color: ${header_text}; }
headerbar .subtitle { color: ${subtitle}; font-size: 88%; }
.card {
  background-color: ${card_bg}; border-radius: ${card_radius};
  border: 1px solid ${border}; padding: 18px;
}
button {
  border-radius: 100px; padding: 6px 20px;
  background-color: ${btn_bg}; color: ${btn_text};
  border: none; text-shadow: none; box-shadow: none;
}
button:hover { background-color: ${btn_hover}; }
button:active { background-color: ${btn_active}; }
button.suggested-action { background-color: ${a_strong}; color: #FFFFFF; }
button.suggested-action:hover { background-color: ${a_pressed}; }
button.destructive-action { background-color: #B3261E; color: #FFFFFF; }
button.destructive-action:hover { background-color: #991F18; }
entry {
  border-radius: 12px; padding: 8px 12px;
  background-color: ${entry_bg}; border: 1px solid ${entry_border};
  box-shadow: none; color: ${text};
}
entry:focus { border: 1px solid ${a_strong}; }
combobox button.combo {
  border-radius: 12px; padding: 6px 12px;
  background-color: ${entry_bg}; border: 1px solid ${entry_border};
  box-shadow: none; color: ${text};
}
switch { background-color: ${btn_bg}; }
switch:checked { background-color: ${a_strong}; }
notebook header { background: transparent; border: none; box-shadow: none; }
notebook header tabs { background: transparent; }
notebook tab {
  border-radius: 100px; padding: 4px 18px; margin: 0 4px;
  background-color: transparent; color: ${muted};
  border: none; box-shadow: none; font-weight: 500;
}
notebook tab:hover { background-color: ${tab_hover}; }
notebook tab:checked { background-color: ${sel_bg}; color: ${sel_ink}; }
notebook stack { background: transparent; border: none; }
treeview { background-color: ${tv_bg}; color: ${text}; }
treeview:hover { background-color: ${tree_hover}; }
/* explicit selection styling: without it some system themes leave the
   selected row looking identical to the rest of the list. The .view class
   is required to out-specify Adwaita's `.view:selected` rule (a plain
   `treeview:selected` loses the cascade and the default blue shows). */
treeview.view:selected { background-color: ${sel_bg}; color: ${sel_ink}; }
treeview.view:selected:backdrop { background-color: ${sel_backdrop_bg};
  color: ${sel_backdrop_ink}; }
label.drive-prog { color: ${muted}; font-size: 95%; }
label.drive-prog.ok { color: ${ok}; }
label.drive-prog.err { color: #B3261E; }
treeview header button {
  background-color: ${header_btn_bg}; border: none; box-shadow: none;
  border-radius: 10px; padding: 4px 8px;
  font-weight: 600; color: ${muted}; text-shadow: none;
}
scrolledwindow { border: none; }
label.dim { color: ${dim}; font-size: 92%; }
label.hint { color: ${muted}; font-size: 95%; }
label.error-text { color: #B3261E; }
label.section { font-weight: 700; color: ${header_text}; font-size: 105%; }
"""


def _kde_prefers_dark():
    """Read KDE's color scheme (None when there is no KDE config).

    KDE Plasma does not publish GNOME's color-scheme setting, so on a KDE
    desktop the System theme used to fall through to a light palette even
    with Breeze Dark selected. kdeglobals carries the scheme name and the
    actual window background colour; either is enough to decide.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    try:
        with open(os.path.join(base, "kdeglobals"), encoding="utf-8",
                  errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if section == "General" and key == "ColorScheme":
            low = value.lower()
            if "dark" in low:
                return True
            if "light" in low:
                return False
        if section == "Colors:Window" and key == "BackgroundNormal":
            try:
                r, g, b = (int(part) for part in value.split(",")[:3])
            except (TypeError, ValueError):
                continue
            return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 128
    return None


def system_prefers_dark():
    """Best-effort desktop dark-mode detection (False when unsure).

    GNOME 42+ exposes color-scheme in org.gnome.desktop.interface; KDE
    exposes its choice in kdeglobals; other desktops fall back to GTK_THEME
    / XDG_CURRENT_DESKTOP name hints.
    """
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib
        value = GLib.Settings.new("org.gnome.desktop.interface") \
            .get_string("color-scheme")
        if value == "prefer-dark":
            return True
        if value == "prefer-light":
            return False
    except Exception:
        pass
    kde = _kde_prefers_dark()
    if kde is not None:
        return kde
    hint = f"{os.environ.get('GTK_THEME', '')} " \
           f"{os.environ.get('XDG_CURRENT_DESKTOP', '')}".lower()
    return "dark" in hint


def canonical(theme):
    """Return the canonical theme key, or None for an unknown value.

    Legacy keys (glass, graphite) are translated to their dark variant so
    configs written before the light/dark split keep working.
    """
    key = LEGACY_THEMES.get(theme, theme)
    return key if key in THEMES else None


def resolve(theme):
    """Map 'system' to the concrete light/dark palette; pass others through."""
    key = canonical(theme) or "light"
    if key == "system":
        return "dark" if system_prefers_dark() else "light"
    return key


def is_dark(theme):
    """True when the resolved look needs the dark gtk-application flag."""
    return resolve(theme) in DARK_THEMES


def build_css(theme, accent):
    """Full GTK CSS for the given theme + accent (pure — testable headless)."""
    pal = dict(_PALETTES.get(resolve(theme), _PALETTES["light"]))
    acc = ACCENTS.get(accent, ACCENTS["blue"])
    pal["a_strong"] = acc["strong"]
    pal["a_pressed"] = acc["pressed"]
    pal["a_soft"] = acc["soft"]
    pal["a_ink"] = acc["ink"]
    pal["a_rgba"] = acc["rgba"]
    css = _CSS_TEMPLATE
    for key, value in pal.items():
        css = css.replace("${%s}" % key, value)
    return css + _SWATCH_CSS


_provider = None


def apply(theme, accent):
    """Push the theme onto the default GTK screen (imports GTK lazily).

    Keeps ONE provider alive for the process and reloads it on every call:
    stacking a new provider per switch would leak style contexts.
    """
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
    settings = Gtk.Settings.get_default()
    settings.set_property("gtk-application-prefer-dark-theme", is_dark(theme))
    css = build_css(theme, accent)
    global _provider
    if _provider is None:
        _provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), _provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    try:
        _provider.load_from_data(css.encode("utf-8"))
    except GLib.GError as exc:
        # a rule rejected by some parser/theme build must not keep the
        # app from launching — fall back to the system theme styling
        print(f"warning: custom CSS rejected ({exc}); using theme defaults",
              file=sys.stderr)
    return css
