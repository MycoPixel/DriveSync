"""systemd watchdog plumbing (S8): sd_notify, heartbeat, service unit."""

import time

import pytest

from drivesync.db import Store
from drivesync.drive import Drive
from drivesync.worker import Worker, sd_notify


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


def test_sd_notify_silent_without_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    sd_notify("READY=1")                      # must be a no-op, not an error


def test_sd_notify_delivers_to_a_unix_socket(monkeypatch, tmp_path):
    import socket
    addr = str(tmp_path / "notify.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(addr)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", addr)
        sd_notify("READY=1")
        srv.settimeout(2)
        assert srv.recv(100) == b"READY=1"
    finally:
        srv.close()


def test_sd_notify_handles_abstract_socket(monkeypatch):
    """NOTIFY_SOCKET starting with '@' is the abstract namespace form."""
    import socket
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind("\0drivesync-test-notify")
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", "@drivesync-test-notify")
        sd_notify("WATCHDOG=1")
        srv.settimeout(2)
        assert srv.recv(100) == b"WATCHDOG=1"
    finally:
        srv.close()


def test_watchdog_sends_ready_and_stopping(monkeypatch, store, tmp_path):
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "s.sock"))
    monkeypatch.setenv("WATCHDOG_USEC", "2000000")
    sent = []
    monkeypatch.setattr("drivesync.worker.sd_notify", sent.append)
    worker = Worker(store, {"sync_interval": 30})
    worker.start()
    time.sleep(1.1)                           # first ping interval = 1 s
    worker.stop()
    assert sent[0] == "READY=1"
    assert "STOPPING=1" in sent
    assert "WATCHDOG=1" in sent               # heartbeat was fresh
    assert worker._watchdog_stop.is_set()


def test_no_systemd_means_no_watchdog_thread(monkeypatch, store):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    sent = []
    monkeypatch.setattr("drivesync.worker.sd_notify", sent.append)
    worker = Worker(store, {"sync_interval": 30})
    worker.start()
    worker.stop()
    assert sent == []
    assert worker._watchdog_thread is None


def test_idle_wait_keeps_the_heartbeat_fresh(store):
    """The loop waits up to `interval` seconds; an idle worker must still
    refresh the heartbeat (the watchdog-sized slices exist for that)."""
    worker = Worker(store, {"sync_interval": 1})
    worker._watchdog_sec = 4.0
    before = worker.heartbeat
    time.sleep(0.05)
    worker._wait_wake()                       # full 1 s slice wait, no wake
    assert worker.heartbeat > before


def test_watchdog_usec_sets_the_window(monkeypatch, store):
    monkeypatch.setenv("NOTIFY_SOCKET", "x")
    monkeypatch.setenv("WATCHDOG_USEC", "3000000")
    sent = []
    monkeypatch.setattr("drivesync.worker.sd_notify", sent.append)
    worker = Worker(store, {"sync_interval": 30})
    worker.start()
    worker.stop()
    assert worker._watchdog_sec == 3.0


def test_watchdog_usec_falls_back_on_garbage(monkeypatch, store):
    monkeypatch.setenv("NOTIFY_SOCKET", "x")
    monkeypatch.setenv("WATCHDOG_USEC", "soon")
    sent = []
    monkeypatch.setattr("drivesync.worker.sd_notify", sent.append)
    worker = Worker(store, {"sync_interval": 30})
    worker.start()
    worker.stop()
    assert worker._watchdog_sec == 120.0


# ---- transfer liveness -------------------------------------------------------
class _StreamResp:
    status_code = 200
    text = ""
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def iter_content(self, chunk_size):
        yield b"aaaa"
        yield b"bbbb"


def test_download_chunks_feed_the_heartbeat(tmp_path):
    pings = []
    drive = Drive(lambda: "tok")
    drive.heartbeat = lambda: pings.append(1)

    class Session:
        def get(self, url, **kw):
            return _StreamResp()

    drive._session = Session()
    dest = tmp_path / "out.bin"
    drive.download("f1", str(dest))
    assert dest.read_bytes() == b"aaaabbbb"
    assert len(pings) == 2                    # one ping per chunk


def test_upload_body_pings_without_limiter(tmp_path):
    pings = []
    drive = Drive(lambda: "tok")
    drive.heartbeat = lambda: pings.append(1)
    src = tmp_path / "f.txt"
    src.write_text("hello")

    class Resp:
        status_code = 200
        text = ""
        headers = {}

        def json(self):
            return {"id": "x"}

    class Session:
        def request(self, *a, **kw):
            kw["data"].read(2)                # requests reads in chunks
            return Resp()

    drive._session = Session()
    drive.update_content("f1", str(src))
    assert pings                              # the read pinged the watchdog


def test_service_unit_declares_notify_and_watchdog():
    from pathlib import Path
    unit = Path(__file__).resolve().parents[1] / "drivesync.service"
    text = unit.read_text(encoding="utf-8")
    assert "Type=notify" in text and "WatchdogSec=" in text


def test_service_units_are_sandboxed():
    """Every shipped user unit must carry the same hardening block."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sources = [root / "drivesync.service",
               root / "packaging" / "build-deb.sh",
               root / "packaging" / "drivesync.spec"]
    required = ("NoNewPrivileges=yes", "PrivateTmp=yes",
                "ProtectSystem=full", "ProtectHome=off",
                "ProtectKernelTunables=yes", "ProtectKernelModules=yes",
                "ProtectControlGroups=yes",
                "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
                "RestrictSUIDSGID=yes", "LockPersonality=yes",
                "PrivateDevices=yes", "ProtectClock=yes",
                "ProtectHostname=yes", "ProtectKernelLogs=yes",
                "RestrictNamespaces=yes", "RestrictRealtime=yes",
                "SystemCallArchitectures=native",
                "SystemCallFilter=@system-service", "UMask=0077",
                "TimeoutStopSec=30")
    for src in sources:
        text = src.read_text(encoding="utf-8")
        missing = [line for line in required if line not in text]
        assert not missing, f"{src.name} is missing {missing}"
