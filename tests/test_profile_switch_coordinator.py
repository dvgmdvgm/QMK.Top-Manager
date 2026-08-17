"""Regression tests for rapid Alt+Tab profile automation.

The foreground scanner must never build a HID backlog or use the global
keyboard-suppression hook.  These tests keep timers deterministic so they can
exercise the A -> B -> A race without depending on a real desktop window.
"""

import os
import sys
import threading

import pytest


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app_flet as app_module
from app_flet import QMKManager


class _ControlledTimer:
    instances = []

    def __init__(self, _delay, callback, args=(), kwargs=None):
        self.callback = callback
        self.args = args
        self.kwargs = kwargs or {}
        self.cancelled = False
        self.daemon = False
        self.__class__.instances.append(self)

    def start(self):
        return None

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback(*self.args, **self.kwargs)


class _ImmediateThread:
    def __init__(self, *, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()


def _automation_manager():
    manager = QMKManager.__new__(QMKManager)
    entry = {
        "keyboard_type": "magnetic",
        "profile_switch_delay_ms": 0,
        "payloads": {},
        "battery": {},
    }
    manager.config = {"active_device": "sk75", "devices": {"sk75": entry}}
    manager.is_running = True
    manager.current_binding = "A"
    manager._auto_profile_switch_lock = threading.RLock()
    manager._auto_profile_switch_timer = None
    manager._auto_profile_switch_revision = 0
    manager._auto_profile_switch_desired = None
    manager._auto_profile_switch_worker_active = False
    manager._auto_profile_switch_transport_uncertain = False
    manager._foreground_process_matches = lambda _name: True
    manager._stop_magnetic_profile_switching = lambda: None
    return manager, entry


def test_rapid_alt_tab_coalesces_to_latest_profile_without_keyboard_hook(monkeypatch):
    manager, entry = _automation_manager()
    _ControlledTimer.instances.clear()
    monkeypatch.setattr(app_module.threading, "Timer", _ControlledTimer)
    monkeypatch.setattr(app_module.threading, "Thread", _ImmediateThread)

    calls = []

    def apply(profile_name, payload, **kwargs):
        calls.append((profile_name, list(payload), kwargs))
        assert kwargs["suppress_input"] is False
        assert kwargs["automatic"] is True
        assert kwargs["should_continue"]() is True
        manager.current_binding = profile_name
        return True

    manager.apply_payload = apply

    manager._request_auto_profile_switch(
        "B", [4, 1], process_name="b.exe", entry=entry
    )
    manager._request_auto_profile_switch(
        "C", [4, 2], process_name="c.exe", entry=entry
    )

    assert len(_ControlledTimer.instances) == 2
    assert _ControlledTimer.instances[0].cancelled is True
    _ControlledTimer.instances[0].fire()
    _ControlledTimer.instances[1].fire()

    assert [call[0] for call in calls] == ["C"]
    assert calls[0][1] == [4, 2]


def test_alt_tab_storm_only_runs_the_last_profile_request(monkeypatch):
    """Dozens of foreground changes cannot form a hidden HID backlog."""
    manager, entry = _automation_manager()
    _ControlledTimer.instances.clear()
    monkeypatch.setattr(app_module.threading, "Timer", _ControlledTimer)
    monkeypatch.setattr(app_module.threading, "Thread", _ImmediateThread)
    calls = []
    manager.apply_payload = lambda name, _payload, **_kwargs: calls.append(name) or True

    for index in range(40):
        manager._request_auto_profile_switch(
            f"profile-{index}",
            [4, index],
            process_name=f"window-{index}.exe",
            entry=entry,
        )

    # Timers that had already been queued are allowed to fire by Windows, but
    # every stale callback must be harmless.  Only the final current request
    # reaches the foreground worker/HID call.
    for timer in list(_ControlledTimer.instances):
        timer.fire()

    assert calls == ["profile-39"]


def test_returning_to_original_profile_reasserts_it_after_cancelled_packet(monkeypatch):
    """A -> B -> A cannot leave the physical keyboard on B.

    The B worker is invalidated after it may have started its first HID
    packet.  ``current_binding`` still says A at that point, so the scheduler
    must use its uncertain-transport bit and send A once more.
    """
    manager, entry = _automation_manager()
    _ControlledTimer.instances.clear()
    monkeypatch.setattr(app_module.threading, "Timer", _ControlledTimer)
    monkeypatch.setattr(app_module.threading, "Thread", _ImmediateThread)

    calls = []

    def apply(profile_name, payload, **kwargs):
        calls.append(profile_name)
        if profile_name == "B":
            manager._request_auto_profile_switch(
                "A", [4, 0], process_name="a.exe", entry=entry
            )
            # The old worker sees the new revision and aborts before any
            # auxiliary HID packets or magnetic preset selection.
            assert kwargs["should_continue"]() is False
            return False
        assert kwargs["should_continue"]() is True
        manager.current_binding = "A"
        return True

    manager.apply_payload = apply
    manager._request_auto_profile_switch("B", [4, 1], process_name="b.exe", entry=entry)
    _ControlledTimer.instances[-1].fire()

    # B created a latest A request while it was running.  It is intentionally
    # re-armed even though the app cache still says A.
    assert len(_ControlledTimer.instances) >= 2
    _ControlledTimer.instances[-1].fire()

    assert calls == ["B", "A"]
    assert manager.current_binding == "A"
    assert manager._auto_profile_switch_transport_uncertain is False


def test_auto_apply_does_not_install_global_input_suppression(monkeypatch):
    manager = QMKManager.__new__(QMKManager)
    entry = {"keyboard_type": "mechanical", "transport": "wired", "battery": {}}
    manager.config = {"active_device": "keyboard", "devices": {"keyboard": entry}}
    manager.usb_lock = threading.Lock()
    manager.current_binding = None
    manager._send_hid_payload = lambda _payload, label: label
    manager._profile_info_at_by_name = lambda _name: {}

    monkeypatch.setattr(app_module, "_stage_delay_ms", lambda _entry, _stage: 0)
    monkeypatch.setattr(
        app_module,
        "_suppress_keyboard_start",
        lambda: pytest.fail("automatic switch must not install a keyboard hook"),
    )
    monkeypatch.setattr(
        app_module,
        "_release_all_keys",
        lambda: pytest.fail("automatic switch must not release user keys"),
    )

    assert manager.apply_payload(
        "Gaming", [4, 1], suppress_input=False, automatic=True
    ) is True
    assert manager.current_binding == "Gaming"


def test_stale_auto_profile_stops_before_magnetic_preset_batch(monkeypatch):
    """A stale normal-profile packet cannot launch a 75-key preset write."""
    manager = QMKManager.__new__(QMKManager)
    entry = {"keyboard_type": "magnetic", "transport": "wired", "battery": {}}
    manager.config = {"active_device": "keyboard", "devices": {"keyboard": entry}}
    manager.usb_lock = threading.Lock()
    manager.current_binding = "A"
    active = {"value": True}
    selected = []

    def send(_payload, label):
        # Simulate an Alt+Tab that happens immediately after the primary
        # packet, before any optional stage or local magnetic preset begins.
        active["value"] = False
        return "hid"

    manager._send_hid_payload = send
    manager._profile_info_at_by_name = lambda _name: {}
    manager._profile_index_by_name = lambda _name: 1
    manager._select_magnetic_preset_for_keyboard_profile = lambda *args, **kwargs: selected.append(args)
    monkeypatch.setattr(app_module, "_stage_delay_ms", lambda _entry, _stage: 0)

    assert manager.apply_payload(
        "B",
        [4, 1],
        should_continue=lambda: active["value"],
        suppress_input=False,
        automatic=True,
    ) is False
    assert selected == []
    assert manager.current_binding == "A"


def test_automatic_magnetic_selection_updates_the_local_selector_without_hid_batch():
    """Alt+Tab may select a preset, but must never queue its 75-key writer."""
    manager = QMKManager.__new__(QMKManager)
    entry = {"keyboard_type": "magnetic"}
    manager.config = {"active_device": "sk75", "devices": {"sk75": entry}}
    manager.magnetic_profile_dropdown = object()
    manager._ui_call = lambda callback: callback()
    observed = {}

    def select(event, **kwargs):
        observed["value"] = event.control.value
        observed.update(kwargs)

    manager._on_magnetic_profile_changed = select

    assert manager._select_magnetic_preset_for_keyboard_profile(
        2, automatic=True, should_continue=lambda: True
    ) is True
    assert observed["value"] == "2"
    assert observed["apply_to_keyboard"] is False


def test_automatic_magnetic_selection_does_not_reload_the_foreground_service():
    """Persisting the visible preset must not cancel the Alt+Tab worker itself."""
    manager = QMKManager.__new__(QMKManager)
    entry = {
        "keyboard_type": "magnetic",
        "magnetic_selected_profile": 0,
        "magnetic_profiles": {},
    }
    manager.config = {"active_device": "sk75", "devices": {"sk75": entry}}
    manager.magnetic_profile_index = 0
    manager.magnetic_profile_dropdown = app_module.SimpleNamespace(value="0")
    manager._store_magnetic_controls_in_profile = lambda _index: None
    manager._cancel_pending_magnetic_writes = lambda: None
    manager._refresh_magnetic_profile_dropdown = lambda: None
    manager._refresh_sk75_keyboard_picker = lambda: None
    manager._set_magnetic_status = lambda *_args: None
    manager._stop_magnetic_profile_switching = lambda: None
    manager._schedule_magnetic_profile_apply = lambda *_args, **_kwargs: pytest.fail(
        "automatic selection must not schedule a magnetic HID worker"
    )
    saved = []
    manager.save_config = lambda **kwargs: saved.append(kwargs)

    manager._on_magnetic_profile_changed(
        app_module.SimpleNamespace(control=app_module.SimpleNamespace(value="1")),
        apply_to_keyboard=False,
    )

    assert entry["magnetic_selected_profile"] == 1
    assert saved == [{"reload_runtime": False}]


def test_automatic_profile_packet_skips_all_auxiliary_hid_and_stage_waits(monkeypatch):
    """Rapid Alt+Tab must be one small HID report, even for a magnetic board."""
    manager = QMKManager.__new__(QMKManager)
    entry = {"keyboard_type": "magnetic", "transport": "wired", "battery": {"query": True}}
    manager.config = {"active_device": "keyboard", "devices": {"keyboard": entry}}
    manager.usb_lock = threading.Lock()
    manager.current_binding = None
    sent = []
    manager._send_hid_payload = lambda payload, label: sent.append((list(payload), label)) or "hid"
    manager._profile_info_at_by_name = lambda _name: {
        "polling_rate": 1000,
        "lighting_profile": 1,
    }
    manager._profile_index_by_name = lambda _name: 2
    selected = []
    manager._select_magnetic_preset_for_keyboard_profile = (
        lambda *args, **kwargs: selected.append((args, kwargs))
    )
    manager._refresh_battery_for_tray = lambda: pytest.fail(
        "automatic profile switch must not queue a battery HID request"
    )
    monkeypatch.setattr(
        app_module,
        "_stage_delay_ms",
        lambda *_args: pytest.fail("automatic profile switch must not wait for a stage"),
    )

    assert manager.apply_payload("Gaming", [4, 1], suppress_input=False, automatic=True) is True
    assert sent == [([4, 1], "profile_Gaming")]
    assert selected == [((2,), {"automatic": True, "should_continue": None})]
