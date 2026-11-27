"""The web dashboard's Spanish catalog must cover every key it uses.

The front-end falls back to English for missing entries, which is fine for
a brand-new string but hides a real bug when a ``data-i18n`` key is a
symbol (``set.keyring``) rather than English text: English users then see
the raw key. This test fails when a key is added without a translation.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "drivesync" / "static"

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _es_catalog(js):
    start = js.index("const ES = {")
    end = js.index("\n};", start)
    block = js[start:end]
    # Keys are always quoted. Values may be single- or double-quoted and may
    # continue on the next line with +, so only the first literal is read;
    # that is the English half and carries the placeholders.
    rows = re.findall(
        r'^\s*"((?:[^"\\]|\\.)+)"\s*:\s*([\'"])((?:\\.|(?!\2).)*)\2',
        block, re.M)
    return {key: value for key, _quote, value in rows}


def _es_keys(js):
    start = js.index("const ES = {")
    end = js.index("\n};", start)
    block = js[start:end]
    return set(re.findall(r'^\s*"((?:[^"\\]|\\.)+)"\s*:', block, re.M))


def _keys(html, js):
    keys = set(re.findall(r'data-i18n(?:-html)?="([^"]+)"', html))
    # t("...") / T("...") with a literal key; dynamic expressions are
    # covered by the server-side strings they interpolate.
    keys |= set(re.findall(
        r'(?<![A-Za-z0-9_$.])[Tt]\(\s*"((?:[^"\\]|\\.)+)"', js))
    return keys


def test_every_web_key_has_a_spanish_translation():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    missing = sorted(k for k in _keys(html, js) if k not in _es_keys(js))
    assert not missing, (
        "untranslated dashboard keys (add them to the ES catalog in "
        f"app.js): {missing}")


def test_translations_keep_template_placeholders():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    catalog = _es_catalog(js)
    bad = []
    for key, value in catalog.items():
        want = set(PLACEHOLDER.findall(key))
        got = set(PLACEHOLDER.findall(value))
        if want != got:
            bad.append((key, sorted(want), sorted(got)))
    assert not bad, f"placeholder mismatch in Spanish catalog: {bad}"
