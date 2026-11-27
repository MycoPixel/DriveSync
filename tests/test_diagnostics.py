"""Support bundle: redaction, contents and safe defaults."""

import json
import zipfile

from drivesync import config
from drivesync.db import Store
from drivesync.diagnostics import build_bundle, redact


def test_redact_masks_google_credentials():
    text = ("access_token=ya29.SECRET refresh_token: 1//REFRESH "
            "GOCSPX-CLIENTSECRET Authorization: Bearer abc.def")
    out = redact(text)
    assert "ya29.SECRET" not in out
    assert "1//REFRESH" not in out
    assert "GOCSPX-CLIENTSECRET" not in out
    assert "abc.def" not in out


def test_bundle_is_redacted_and_contains_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_SYNC_HOME", str(tmp_path / "home"))
    config.update_config(client_id="cid", client_secret="topsecret")
    store = Store(tmp_path / "state.db")
    try:
        store.log(None, "error", "",
                  "failed with refresh_token=1//LEAKED", level="error")
        dest = tmp_path / "bundle.zip"
        path = build_bundle(dest, store=store)
        assert path == dest and dest.exists()
        with zipfile.ZipFile(dest) as zf:
            names = set(zf.namelist())
            assert {"summary.txt", "config.json", "events.json"} <= names
            blob = "\n".join(zf.read(n).decode("utf-8")
                             for n in ("config.json", "events.json"))
            cfg = json.loads(zf.read("config.json"))
        assert "topsecret" not in blob
        assert "1//LEAKED" not in blob
        assert cfg["client_secret"] == "***"
    finally:
        store.close()
