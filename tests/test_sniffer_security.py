"""Safety regression tests for the opt-in Chromium HID sniffer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import sniffer


def test_cdp_origin_is_loopback_and_never_wildcard():
    assert sniffer._cdp_allowed_origin(43123) == "http://127.0.0.1:43123"


def test_browser_ws_endpoint_requires_our_exact_loopback_browser_target():
    endpoint = sniffer._validate_browser_ws_endpoint(
        "ws://127.0.0.1:43123/devtools/browser/abc-123", 43123
    )
    assert endpoint == "ws://127.0.0.1:43123/devtools/browser/abc-123"
    assert sniffer._validate_browser_ws_endpoint(
        "ws://localhost:43123/devtools/browser/abc-123", 43123
    ) == endpoint

    for unsafe in (
        "ws://127.0.0.1:43124/devtools/browser/abc-123",
        "wss://127.0.0.1:43123/devtools/browser/abc-123",
        "ws://127.0.0.1:43123/devtools/page/abc-123",
        "ws://127.0.0.1:43123/devtools/browser/abc-123?redirect=1",
        "ws://user@127.0.0.1:43123/devtools/browser/abc-123",
    ):
        with pytest.raises(ValueError):
            sniffer._validate_browser_ws_endpoint(unsafe, 43123)


def test_cdp_version_read_is_bounded_and_uses_direct_loopback_http(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def read(self, size):
            captured["read_size"] = size
            return b'{"webSocketDebuggerUrl":"ws://127.0.0.1:43123/devtools/browser/abc-123"}'

    class FakeConnection:
        def __init__(self, host, port, timeout):
            captured.update(host=host, port=port, timeout=timeout)

        def request(self, method, target, headers):
            captured.update(method=method, target=target, headers=headers)

        def getresponse(self):
            return FakeResponse()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(sniffer.http.client, "HTTPConnection", FakeConnection)

    assert sniffer._read_browser_ws_endpoint(43123).endswith("/devtools/browser/abc-123")
    assert captured["host"] == "127.0.0.1"
    assert captured["target"] == "/json/version"
    assert captured["read_size"] == sniffer.CDP_VERSION_RESPONSE_MAX_BYTES + 1
    assert captured["closed"] is True


def test_cdp_version_read_rejects_an_oversized_response(monkeypatch):
    class FakeResponse:
        status = 200

        def read(self, _size):
            return b"x" * (sniffer.CDP_VERSION_RESPONSE_MAX_BYTES + 1)

    class FakeConnection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            return None

        def getresponse(self):
            return FakeResponse()

        def close(self):
            return None

    monkeypatch.setattr(sniffer.http.client, "HTTPConnection", FakeConnection)

    with pytest.raises(ValueError, match="слишком большой"):
        sniffer._read_browser_ws_endpoint(43123)


def test_debug_log_is_rotated_and_each_message_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / "sniffer_debug.log"
    monkeypatch.setattr(sniffer, "DEBUG_LOG_PATH", str(path))
    monkeypatch.setattr(sniffer, "DEBUG_LOG_MAX_BYTES", 80)

    sniffer._dlog("A" * 200)
    sniffer._dlog("B" * 200)

    assert path.is_file()
    assert (tmp_path / "sniffer_debug.log.1").is_file()
    assert "A" in (tmp_path / "sniffer_debug.log.1").read_text(encoding="utf-8")
    # Newlines from an external CDP value cannot forge extra log entries.
    sniffer._dlog("line-one\nline-two")
    assert "line-one\\nline-two" in path.read_text(encoding="utf-8")


def test_sniffer_launches_chromium_with_one_loopback_origin(tmp_path, monkeypatch):
    launched = []
    connected = {}

    class FakeProcess:
        pid = 123

        def poll(self):
            return 0

        def terminate(self):
            raise AssertionError("already exited process must not terminate")

    class FakeThread:
        def __init__(self, *, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            # Do not start a real CDP receive loop in this construction test.
            return None

    ws = SimpleNamespace(
        close=lambda: None,
        send=lambda _payload: None,
        settimeout=lambda _value: None,
    )
    monkeypatch.setattr(sniffer, "DEBUG_LOG_PATH", str(tmp_path / "sniffer_debug.log"))
    monkeypatch.setattr(sniffer, "_free_port", lambda: 43123)
    monkeypatch.setattr(sniffer.tempfile, "mkdtemp", lambda prefix: str(tmp_path / prefix))
    monkeypatch.setattr(
        sniffer.subprocess,
        "Popen",
        lambda args: launched.append(args) or FakeProcess(),
    )
    monkeypatch.setattr(
        sniffer.websocket,
        "create_connection",
        lambda url, **kwargs: connected.update(url=url, **kwargs) or ws,
    )
    monkeypatch.setattr(sniffer.threading, "Thread", FakeThread)

    instance = sniffer.HIDSniffer(lambda _event: None, browser_path="C:/browser/chrome.exe")
    monkeypatch.setattr(instance, "_wait_for_browser_ws", lambda timeout: "ws://127.0.0.1:43123/devtools/browser/id")
    monkeypatch.setattr(sniffer.os.path, "isfile", lambda path: path == "C:/browser/chrome.exe")
    instance.start()

    args = launched[0]
    assert "--remote-debugging-address=127.0.0.1" in args
    assert "--remote-allow-origins=http://127.0.0.1:43123" in args
    assert not any("remote-allow-origins=*" in arg for arg in args)
    assert connected["origin"] == "http://127.0.0.1:43123"
    assert connected["max_size"] == 8 * 1024 * 1024
