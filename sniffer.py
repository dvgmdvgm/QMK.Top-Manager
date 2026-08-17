"""HID sniffer that drives a controlled Chrome instance via CDP.

The sniffer is strictly opt-in. It is not started on app launch and is only
used when the user explicitly opens the sniff UI and presses Start.
"""
from __future__ import annotations

import json
import logging
import os
import http.client
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from typing import Callable, Optional

import websocket  # websocket-client

logger = logging.getLogger(__name__)


def _resource_path(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


SNIFFER_JS_PATH = _resource_path("sniffer.js")
DEBUG_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "sniffer_debug.log")
PY_BINDING = "qmkPyEmit"
DEBUG_LOG_MAX_BYTES = 2 * 1024 * 1024
DEBUG_LOG_BACKUP_COUNT = 2
_DEBUG_LOG_LOCK = threading.RLock()
CDP_VERSION_RESPONSE_MAX_BYTES = 64 * 1024
_CDP_BROWSER_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,256}")


def _cdp_allowed_origin(port: int) -> str:
    """Return the sole loopback WebSocket origin used by this sniffer."""
    try:
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError("некорректный локальный CDP-порт") from exc
    if not 1 <= port <= 65535:
        raise ValueError("некорректный локальный CDP-порт")
    return f"http://127.0.0.1:{port}"


def _validate_browser_ws_endpoint(endpoint: object, expected_port: int) -> str:
    """Accept only the browser CDP endpoint owned by our loopback listener.

    ``/json/version`` is read from a local TCP endpoint, but it is still
    external input.  Reject a redirect-like URL, a page target, a different
    port, credentials, query strings and every non-loopback host before it is
    handed to the WebSocket client.
    """
    _cdp_allowed_origin(expected_port)  # validates the expected port too
    if not isinstance(endpoint, str) or len(endpoint) > 1024:
        raise ValueError("Chrome DevTools вернул некорректный WebSocket URL")
    try:
        parsed = urlsplit(endpoint)
        actual_port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("Chrome DevTools вернул некорректный WebSocket URL") from exc

    if (
        parsed.scheme.lower() != "ws"
        # Chromium commonly advertises ``localhost`` even when its DevTools
        # listener was explicitly bound to 127.0.0.1.  Accept that spelling,
        # then canonicalise it below so the WebSocket client never performs a
        # hosts-file/DNS lookup for it.
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or actual_port != int(expected_port)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Chrome DevTools вернул endpoint вне локального браузера")

    parts = parsed.path.split("/")
    if (
        len(parts) != 4
        or parts[:3] != ["", "devtools", "browser"]
        or not _CDP_BROWSER_ID_RE.fullmatch(parts[3])
    ):
        raise ValueError("Chrome DevTools вернул некорректный browser endpoint")
    # Return a canonical string, not a quirky-but-parseable source URL.
    return f"ws://127.0.0.1:{int(expected_port)}{parsed.path}"


def _read_browser_ws_endpoint(port: int) -> str:
    """Fetch a size-limited CDP version document without redirects/proxies."""
    _cdp_allowed_origin(port)
    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=1)
    try:
        connection.request("GET", "/json/version", headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("Chrome DevTools не принял локальный запрос")
        raw = response.read(CDP_VERSION_RESPONSE_MAX_BYTES + 1)
    finally:
        connection.close()

    if len(raw) > CDP_VERSION_RESPONSE_MAX_BYTES:
        raise ValueError("Chrome DevTools вернул слишком большой ответ")
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Chrome DevTools вернул некорректный JSON") from exc
    if not isinstance(info, dict):
        raise ValueError("Chrome DevTools вернул некорректный JSON")
    return _validate_browser_ws_endpoint(info.get("webSocketDebuggerUrl"), port)


def _rotate_debug_log_if_needed() -> None:
    """Keep opt-in protocol diagnostics bounded without fsyncing every event."""
    try:
        if not os.path.isfile(DEBUG_LOG_PATH):
            return
        if os.path.getsize(DEBUG_LOG_PATH) < DEBUG_LOG_MAX_BYTES:
            return
        oldest = f"{DEBUG_LOG_PATH}.{DEBUG_LOG_BACKUP_COUNT}"
        try:
            os.remove(oldest)
        except FileNotFoundError:
            pass
        for index in range(DEBUG_LOG_BACKUP_COUNT - 1, 0, -1):
            source = f"{DEBUG_LOG_PATH}.{index}"
            target = f"{DEBUG_LOG_PATH}.{index + 1}"
            if os.path.isfile(source):
                os.replace(source, target)
        os.replace(DEBUG_LOG_PATH, f"{DEBUG_LOG_PATH}.1")
    except OSError:
        # Debug tracing must never stop a requested HID/session shutdown.
        pass


def _dlog(msg: str) -> None:
    try:
        # CDP payloads are external input.  Keep one diagnostic line bounded
        # and escaped, then rotate before appending; a busy page cannot turn
        # this optional log into an unbounded disk/CPU sink.
        safe = str(msg).replace("\r", "\\r").replace("\n", "\\n")[:2048]
        with _DEBUG_LOG_LOCK:
            _rotate_debug_log_if_needed()
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {safe}\n")
    except Exception:
        pass

# Wrapper appended to sniffer.js: re-points record() to also emit to Python.
PY_BRIDGE_JS = r"""
(function () {
    if (!window.qmkSniffer) return;
    if (window.__qmkPyBridge) return;
    window.__qmkPyBridge = true;
    const origArmBattery = window.qmkSniffer.armBattery;
    // Patch record path: monkey-patch HIDDevice prototypes again to also call binding.
    const sendF = HIDDevice.prototype.sendFeatureReport;
    const sendO = HIDDevice.prototype.sendReport;
    const recvF = HIDDevice.prototype.receiveFeatureReport;
    function emit(payload) {
        try { window.__BINDING__ && window.__BINDING__(JSON.stringify(payload)); } catch (e) {}
    }
    function bytesOf(d) {
        if (d instanceof DataView) return Array.from(new Uint8Array(d.buffer, d.byteOffset, d.byteLength));
        return Array.from(new Uint8Array(d.buffer || d));
    }
    HIDDevice.prototype.sendFeatureReport = function (id, data) {
        emit({dir:'tx', type:'feature', reportId:id, data:bytesOf(data), ts:Date.now()});
        return sendF.apply(this, arguments);
    };
    HIDDevice.prototype.sendReport = function (id, data) {
        emit({dir:'tx', type:'output', reportId:id, data:bytesOf(data), ts:Date.now()});
        return sendO.apply(this, arguments);
    };
    HIDDevice.prototype.receiveFeatureReport = function (id) {
        const p = recvF.apply(this, arguments);
        return p.then(v => { emit({dir:'rx', type:'feature', reportId:id, data:bytesOf(v), ts:Date.now()}); return v; });
    };
    if (navigator.hid && navigator.hid.getDevices) {
        navigator.hid.getDevices().then(ds => ds.forEach(d => {
            if (d.__qmkPyAttached) return;
            d.__qmkPyAttached = true;
            d.addEventListener('inputreport', e => emit({dir:'rx', type:'input', reportId:e.reportId, data:bytesOf(e.data), ts:Date.now()}));
        }));
    }
    // Auto-arm battery mode so all tx/rx are captured (the JS gate uses armed/batteryMode).
    window.qmkSniffer.armBattery();
    console.log('%c[QMK Py bridge] active', 'color:#00aaff;font-weight:bold');
})();
""".replace("__BINDING__", PY_BINDING)


def _find_chrome() -> Optional[str]:
    candidates = [
        os.environ.get("PROGRAMFILES", "") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("PROGRAMFILES(X86)", "") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("LOCALAPPDATA", "") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("PROGRAMFILES", "") + r"\Microsoft\Edge\Application\msedge.exe",
        os.environ.get("PROGRAMFILES(X86)", "") + r"\Microsoft\Edge\Application\msedge.exe",
        os.environ.get("PROGRAMFILES", "") + r"\BraveSoftware\Brave-Browser\Application\brave.exe",
        os.environ.get("PROGRAMFILES(X86)", "") + r"\BraveSoftware\Brave-Browser\Application\brave.exe",
        os.environ.get("LOCALAPPDATA", "") + r"\BraveSoftware\Brave-Browser\Application\brave.exe",
        os.environ.get("LOCALAPPDATA", "") + r"\Yandex\YandexBrowser\Application\browser.exe",
        os.environ.get("LOCALAPPDATA", "") + r"\Vivaldi\Application\vivaldi.exe",
        os.environ.get("PROGRAMFILES", "") + r"\Opera\launcher.exe",
    ]
    # Registry App Paths
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for exe in ("chrome.exe", "msedge.exe", "brave.exe", "vivaldi.exe", "opera.exe", "browser.exe"):
                try:
                    with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                        v, _ = winreg.QueryValueEx(k, "")
                        if v:
                            candidates.append(v)
                except OSError:
                    pass
    except Exception:
        pass
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    for name in ("chrome", "msedge", "brave", "vivaldi"):
        found = shutil.which(name)
        if found:
            return found
    return None


def is_chromium_executable(path: str) -> bool:
    """Best-effort check that a file is a chromium-based browser executable."""
    if not path or not os.path.isfile(path):
        return False
    name = os.path.basename(path).lower()
    return name.endswith(".exe") and any(
        tag in name for tag in ("chrome", "edge", "brave", "vivaldi", "chromium", "browser", "opera")
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HIDSniffer:
    """Lifecycle: start() -> events arrive via on_event(dict) -> stop()."""

    def __init__(
        self,
        on_event: Callable[[dict], None],
        on_status: Callable[[str], None] | None = None,
        url: str = "https://qmk.top",
        browser_path: Optional[str] = None,
        offline_mode: bool = False,
    ):
        self.on_event = on_event
        self.on_status = on_status or (lambda s: None)
        self.url = url
        self.browser_path = browser_path
        self.offline_mode = offline_mode
        self.proc: Optional[subprocess.Popen] = None
        self.profile_dir: Optional[str] = None
        self.port: int = 0
        self.ws: Optional[websocket.WebSocket] = None
        self._msg_id = 0
        self._listener: Optional[threading.Thread] = None
        self._alive = False

    # ---------- public ----------
    def start(self) -> None:
        if self.offline_mode:
            raise RuntimeError("Sniffer is disabled in offline mode.")
        try:
            with open(DEBUG_LOG_PATH, "w", encoding="utf-8") as f:
                f.write(f"=== sniffer start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        except Exception:
            pass
        chrome = self.browser_path if self.browser_path and os.path.isfile(self.browser_path) else _find_chrome()
        logger.debug("sniffer start browser=%s", chrome)
        _dlog(f"browser: {chrome}")
        if not chrome:
            raise RuntimeError("Chromium-браузер не найден. Укажи путь к chrome.exe / msedge.exe вручную.")
        self.port = _free_port()
        self.cdp_origin = _cdp_allowed_origin(self.port)
        self.profile_dir = tempfile.mkdtemp(prefix="qmk_sniff_")
        _dlog(f"port={self.port} profile={self.profile_dir}")
        self.on_status(f"Запускаю браузер (порт {self.port})…")
        self.proc = subprocess.Popen([
            chrome,
            f"--remote-debugging-port={self.port}",
            # CDP owns the whole temporary browser profile.  Bind it to
            # loopback and allow only this process's matching loopback Origin,
            # never Chromium's broad ``*`` development escape hatch.
            "--remote-debugging-address=127.0.0.1",
            f"--remote-allow-origins={self.cdp_origin}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=AutomationControlled",
            self.url,
        ])
        _dlog(f"chrome pid={self.proc.pid}")
        try:
            browser_ws = self._wait_for_browser_ws(timeout=15)
            _dlog(f"browser_ws={browser_ws}")
            self.ws = websocket.create_connection(
                browser_ws,
                timeout=30,
                # A CDP command/event for this sniffer is small.  An unbounded
                # frame was a needless local memory-exhaustion surface.
                max_size=8 * 1024 * 1024,
                origin=self.cdp_origin,
            )
            self.ws.settimeout(None)
            _dlog("ws connected OK")
        except Exception as ex:
            _dlog(f"WS CONNECT FAIL: {type(ex).__name__}: {ex}")
            # A failed CDP startup must not leave a temporary profile or an
            # invisible Chromium process behind.  ``stop`` is safe before the
            # listener is marked alive and makes the retry path clean.
            self.stop()
            raise RuntimeError(f"Не удалось открыть CDP WebSocket: {ex}")
        self._alive = True
        self._sessions = {}
        self._listener = threading.Thread(target=self._listen, daemon=True)
        self._listener.start()
        try:
            with open(SNIFFER_JS_PATH, "r", encoding="utf-8") as f:
                base_js = f.read()
            _dlog(f"sniffer.js loaded, {len(base_js)} bytes")
        except FileNotFoundError:
            base_js = ""
            _dlog("WARNING: sniffer.js NOT FOUND")
        self._full_js = base_js + "\n" + PY_BRIDGE_JS
        _dlog(f"full_js total {len(self._full_js)} bytes")
        self._send("Target.setDiscoverTargets", {"discover": True})
        self._send("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": True,
            "flatten": True,
        })
        _dlog("CDP autoAttach armed")
        self.on_status("Сниффер активен. Открой qmk.top, подключи клавиатуру, меняй настройки.")

    def stop(self) -> None:
        self._alive = False
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass
        if self.profile_dir and os.path.isdir(self.profile_dir):
            shutil.rmtree(self.profile_dir, ignore_errors=True)
        self.on_status("Сниффер остановлен.")

    # ---------- internals ----------
    def _wait_for_browser_ws(self, timeout: float) -> str:
        """Connect to the browser-level CDP endpoint, not a single page."""
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError("Chromium завершил работу до запуска DevTools")
            try:
                return _read_browser_ws_endpoint(self.port)
            except Exception as e:
                last_err = e
            time.sleep(0.3)
        raise RuntimeError(f"Не удалось подключиться к Chrome DevTools: {last_err}")

    def _send(self, method: str, params: dict | None = None, sessionId: str | None = None) -> int:
        self._msg_id += 1
        payload = {"id": self._msg_id, "method": method, "params": params or {}}
        if sessionId:
            payload["sessionId"] = sessionId
        assert self.ws is not None
        _dlog(f"-> {method} sid={sessionId or '-'}")
        self.ws.send(json.dumps(payload))
        return self._msg_id

    def _setup_session(self, session_id: str) -> None:
        """Called once per attached page target. Installs binding + injects JS, then resumes."""
        self._send("Runtime.enable", sessionId=session_id)
        self._send("Page.enable", sessionId=session_id)
        self._send("Runtime.addBinding", {"name": PY_BINDING}, sessionId=session_id)
        self._send("Page.addScriptToEvaluateOnNewDocument",
                   {"source": self._full_js}, sessionId=session_id)
        # Inject into current document too (covers the very first page)
        self._send("Runtime.evaluate",
                   {"expression": self._full_js, "awaitPromise": False},
                   sessionId=session_id)
        # Release the page from the wait-for-debugger pause
        self._send("Runtime.runIfWaitingForDebugger", sessionId=session_id)

    def _listen(self) -> None:
        while self._alive:
            try:
                assert self.ws is not None
                raw = self.ws.recv()
                if not raw:
                    continue
                msg = json.loads(raw)
                method = msg.get("method")
                if method:
                    _dlog(f"<- {method} {json.dumps(msg.get('params', {}))[:200]}")
                elif "error" in msg:
                    _dlog(f"<- ERROR id={msg.get('id')} {msg['error']}")
                elif "result" in msg:
                    _dlog(f"<- ok id={msg.get('id')} {json.dumps(msg.get('result', {}))[:120]}")
                if method == "Target.attachedToTarget":
                    p = msg.get("params", {})
                    info = p.get("targetInfo", {})
                    sid = p.get("sessionId")
                    ttype = info.get("type")
                    if sid and ttype == "page":
                        self._sessions[sid] = info
                        try:
                            self._setup_session(sid)
                            _dlog(f"injected into page sid={sid}")
                        except Exception as ex:
                            _dlog(f"INJECT FAIL: {ex}")
                    elif sid:
                        try:
                            self._send("Runtime.runIfWaitingForDebugger", sessionId=sid)
                        except Exception:
                            pass
                elif method == "Runtime.bindingCalled":
                    p = msg.get("params", {})
                    if p.get("name") == PY_BINDING:
                        try:
                            payload = json.loads(p.get("payload", "{}"))
                            self.on_event(payload)
                        except Exception as ex:
                            _dlog(f"binding parse err: {ex}")
                elif method == "Runtime.executionContextCreated":
                    # Don't re-inject here — sniffer.js is not safe to run twice on the
                    # same context: it self-detects and calls stop() which restores
                    # HIDDevice prototypes, simultaneously the bridge __qmkPyBridge guard
                    # prevents Python hooks from being reinstalled. Net effect: hooks are
                    # gone. The initial Runtime.evaluate on attach + addScriptToEvaluate
                    # OnNewDocument together cover the current doc and all future ones.
                    pass
                elif method == "Runtime.exceptionThrown":
                    p = msg.get("params", {})
                    det = p.get("exceptionDetails", {})
                    text = det.get("text") or det.get("exception", {}).get("description", "")
                    _dlog(f"JS EXCEPTION: {text[:300]}")
            except websocket.WebSocketConnectionClosedException:
                _dlog("ws closed")
                break
            except Exception as ex:
                if not self._alive:
                    break
                _dlog(f"listen err: {ex}")
                time.sleep(0.1)
