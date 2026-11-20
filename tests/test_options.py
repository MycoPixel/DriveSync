"""Options round: themes, accents, i18n (EN/ES), autostart, config keys."""

import pytest

from drivesync import autostart, i18n, themes
from drivesync.config import DEFAULT_CONFIG, load_config, update_config


# ---- themes ------------------------------------------------------------------
@pytest.mark.parametrize("theme", themes.THEMES)
def test_every_theme_builds_full_css(theme):
    css = themes.build_css(theme, "blue")
    assert len(css) > 3000
    for token in ("${",):
        assert token not in css               # every placeholder resolved
    for rule in ("headerbar", "notebook tab:checked", "treeview.view:selected",
                 "button.suggested-action"):
        assert rule in css
    expected = theme in themes.DARK_THEMES
    if theme == "system":
        expected = themes.resolve("system") == "dark"
    assert themes.is_dark(theme) == expected


def test_accent_colors_flow_into_the_css():
    # split off the theme-independent swatch chips: every accent hex appears
    # there by design, so assertions target the themed body only
    css = themes.build_css("light", "purple")
    body = css.split("button.swatch", 1)[0]
    assert themes.ACCENTS["purple"]["strong"] in body
    assert themes.ACCENTS["purple"]["soft"] in body
    other = themes.build_css("light", "teal")
    other_body = other.split("button.swatch", 1)[0]
    assert themes.ACCENTS["teal"]["strong"] in other_body
    assert themes.ACCENTS["purple"]["strong"] not in other_body


def test_system_theme_resolves_to_a_concrete_palette(monkeypatch):
    # headless sandbox: no GNOME settings -> falls back to light
    monkeypatch.delenv("GTK_THEME", raising=False)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    assert themes.resolve("system") in ("light", "dark")
    assert themes.resolve("nonsense") == "light"      # unknown -> light


def test_unknown_accent_falls_back_to_blue():
    css = themes.build_css("light", "nope")
    assert themes.ACCENTS["blue"]["strong"] in css


def test_glass_variants_use_translucency_and_the_right_dark_flag():
    dark = themes.build_css("glass-dark", "blue")
    light = themes.build_css("glass-light", "blue")
    assert "rgba(" in dark and "rgba(" in light
    assert themes.is_dark("glass-dark") is True
    assert themes.is_dark("glass-light") is False


def test_graphite_variants_are_both_available():
    assert themes.is_dark("graphite-dark") is True
    assert themes.is_dark("graphite-light") is False
    assert "#26262a" in themes.build_css("graphite-dark", "blue").lower()
    assert "#f5f5f7" in themes.build_css("graphite-light", "blue").lower()


def test_legacy_theme_names_map_to_their_dark_variants():
    # configs saved before the light/dark split keep working
    assert themes.canonical("glass") == "glass-dark"
    assert themes.canonical("graphite") == "graphite-dark"
    assert themes.resolve("glass") == "glass-dark"
    assert themes.canonical("neon") is None


def test_kde_color_scheme_is_detected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert themes._kde_prefers_dark() is None

    (tmp_path / "kdeglobals").write_text(
        "[General]\nColorScheme=BreezeDark\n", encoding="utf-8")
    assert themes._kde_prefers_dark() is True

    (tmp_path / "kdeglobals").write_text(
        "[General]\nColorScheme=BreezeLight\n", encoding="utf-8")
    assert themes._kde_prefers_dark() is False

    # no usable scheme name: fall back to the window background luminance
    (tmp_path / "kdeglobals").write_text(
        "[Colors:Window]\nBackgroundNormal=35,38,41\n", encoding="utf-8")
    assert themes._kde_prefers_dark() is True
    (tmp_path / "kdeglobals").write_text(
        "[Colors:Window]\nBackgroundNormal=252,252,252\n", encoding="utf-8")
    assert themes._kde_prefers_dark() is False


# ---- i18n --------------------------------------------------------------------
def test_every_catalog_entry_has_a_real_translation():
    for key, es in i18n._STRINGS.items():
        assert isinstance(key, str) and key
        assert isinstance(es, str) and es.strip(), f"empty es for {key!r}"


def test_t_translates_and_falls_back():
    i18n.set_language("es")
    try:
        assert i18n.t("Setup") == "Configuración"
        assert i18n.t("no such string in the catalog") == \
            "no such string in the catalog"       # fallback, never a crash
        assert i18n.T("{sec}s ago", sec=5) == "hace 5s"
        assert i18n.get_language() == "es"
        assert i18n.current_name() == "Español"
        assert i18n.other_name() == "English"
    finally:
        i18n.set_language("en")
    assert i18n.t("Setup") == "Setup"             # identity in English
    assert i18n.T("{sec}s ago", sec=5) == "5s ago"
    assert i18n.current_name() == "English"


def test_set_language_degrades_unknown_codes():
    i18n.set_language("fr")
    assert i18n.get_language() == "en"
    i18n.set_language("es")
    i18n.set_language("en")
    assert i18n.get_language() == "en"


def test_essential_chrome_is_translated():
    i18n.set_language("es")
    try:
        for key in ("Setup", "Accounts", "Folders", "Activity",
                    "Options", "Pause sync", "Theme: light",
                    "Theme: glass light", "Theme: glass dark",
                    "Theme: graphite light", "Theme: graphite dark",
                    "Base theme", "Style variants",
                    "Start DriveSync at login",
                    "Stay in the system tray", "Sign in with Google\u2026",
                    "Add folder\u2026", "Sync all now", "Quit DriveSync"):
            # "Drive" is the brand name and reads the same in both
            # languages, so it is deliberately not in this list
            assert i18n.t(key) != key, f"not translated: {key!r}"
    finally:
        i18n.set_language("en")


# ---- autostart ---------------------------------------------------------------
def test_autostart_enable_disable_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert autostart.is_enabled() is False
    path = autostart.enable()
    assert path == tmp_path / "autostart" / "drivesync.desktop"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert autostart.MARKER in text
    assert "-m drivesync gui" in text
    assert autostart.is_enabled() is True
    assert autostart.disable() is True
    assert not path.exists()
    assert autostart.is_enabled() is False


def test_autostart_disable_spares_a_foreign_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "autostart" / "drivesync.desktop"
    path.parent.mkdir(parents=True)
    path.write_text("[Desktop Entry]\nName=Something else entirely\n")
    assert autostart.disable() is False       # not ours: left alone
    assert path.exists()
    assert autostart.is_enabled() is False    # and not reported as ours


def test_autostart_enable_overwrites_stale_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "autostart" / "drivesync.desktop"
    path.parent.mkdir(parents=True)
    path.write_text("[Desktop Entry]\nExec=/old/venv/bin/drivesync\n")
    autostart.enable()
    assert "/old/venv" not in path.read_text(encoding="utf-8")


def test_autostart_uses_the_appimage_path_not_the_mount(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPIMAGE", "/opt/DriveSync.AppImage")
    path = autostart.enable()
    text = path.read_text(encoding="utf-8")
    assert 'Exec="/opt/DriveSync.AppImage" gui' in text
    assert "-m drivesync gui" not in text


# ---- config keys ---------------------------------------------------------------
def test_config_defaults_carry_the_new_keys():
    assert DEFAULT_CONFIG["theme"] == "system"
    assert DEFAULT_CONFIG["accent"] == "blue"
    assert DEFAULT_CONFIG["language"] == "en"
    assert DEFAULT_CONFIG["start_at_login"] is False


def test_config_accepts_the_new_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path))
    cfg = update_config(theme="glass-dark", accent="teal", language="es",
                        start_at_login=True)
    assert (cfg["theme"], cfg["accent"], cfg["language"],
            cfg["start_at_login"]) == ("glass-dark", "teal", "es", True)
    reread = load_config()
    assert reread["theme"] == "glass-dark" and reread["language"] == "es"


def test_config_migrates_legacy_theme_names(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path))
    assert update_config(theme="glass")["theme"] == "glass-dark"
    assert update_config(theme="graphite")["theme"] == "graphite-dark"


@pytest.mark.parametrize("kwargs,frag", [
    ({"theme": "neon"}, "theme must be one of"),
    ({"accent": "chartreuse"}, "accent must be one of"),
    ({"language": "de"}, "language must be 'en' or 'es'"),
])
def test_config_rejects_bad_appearance_values(tmp_path, monkeypatch,
                                              kwargs, frag):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path))
    with pytest.raises(ValueError, match=frag):
        update_config(**kwargs)
