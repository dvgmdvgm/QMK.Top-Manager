"""Regression coverage for final magnetic writes when the window is hidden."""
import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app_flet import QMKManager
from magnetic import KeyMagneticSettings, KeyboardOptions


class _Timer:
    def __init__(self):
        self.cancelled = 0

    def cancel(self):
        self.cancelled += 1


def _flush_manager():
    manager = QMKManager.__new__(QMKManager)
    manager._magnetic_write_lock = threading.Lock()
    manager.usb_lock = threading.Lock()
    manager._magnetic_write_timers = {}
    manager._magnetic_write_revisions = {}
    manager._magnetic_pending_key_writes = {}
    manager._magnetic_inflight_key_writes = {}
    manager._magnetic_options_timer = None
    manager._magnetic_options_revision = 0
    manager._magnetic_pending_options_write = None
    manager._magnetic_options_inflight = None
    manager._set_magnetic_status = lambda *_args, **_kwargs: None
    return manager


def test_hide_flush_sends_latest_key_and_options_once_without_waiting_on_ui():
    manager = _flush_manager()
    settings = KeyMagneticSettings(1.37, True, 0.15, 0.15, 0.05, 0.10)
    key_timer = _Timer()
    options_timer = _Timer()
    manager._magnetic_write_revisions[8] = 4
    manager._magnetic_write_timers[8] = key_timer
    manager._magnetic_pending_key_writes[8] = (settings, [[0x65]], 4, 2)
    manager._magnetic_options_revision = 7
    manager._magnetic_options_timer = options_timer
    manager._magnetic_pending_options_write = (75, True, 7, 2)
    manager._live_magnetic_keyboard_options = lambda: KeyboardOptions()

    calls = []
    manager._send_lighting_packets_locked = lambda packets, label, inter_packet_delay=0.0: calls.append(
        (list(packets), label, inter_packet_delay)
    )
    manager._cache_magnetic_settings = lambda slot, value, profile_index: calls.append(
        ("key-cache", slot, value, profile_index)
    )
    manager._cache_magnetic_keyboard_options = lambda value, profile_index: calls.append(
        ("options-cache", value, profile_index)
    )
    manager.save_config = lambda **_kwargs: calls.append("save")

    worker = manager._flush_pending_magnetic_writes()

    assert worker is not None
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert key_timer.cancelled == 1
    assert options_timer.cancelled == 1
    send_labels = [
        call[1]
        for call in calls
        if isinstance(call, tuple) and isinstance(call[0], list)
    ]
    assert send_labels == [
        "magnetic_key_8",
        "magnetic_kboption",
    ]
    assert ("key-cache", 8, settings, 2) in calls
    assert any(call[0] == "options-cache" and call[2] == 2 for call in calls)
    assert manager._pending_magnetic_writes_are_idle() is True


def test_profile_or_device_cancellation_still_invalidates_a_queued_final_flush():
    manager = _flush_manager()
    manager._magnetic_write_revisions[8] = 1
    manager._magnetic_write_timers[8] = _Timer()
    manager._magnetic_pending_key_writes[8] = (object(), [[0x65]], 1, 0)
    manager._magnetic_options_revision = 3
    manager._magnetic_options_timer = _Timer()
    manager._magnetic_pending_options_write = (25, False, 3, 0)
    # This represents a hide-triggered worker that was claimed but has not
    # opened the USB handle yet.  A profile/device transition must invalidate
    # it too, otherwise the old profile could be written after the switch.
    manager._magnetic_inflight_key_writes[9] = (1, "flush")
    manager._magnetic_write_revisions[9] = 1

    manager._cancel_pending_magnetic_writes()

    assert manager._magnetic_write_revisions[8] == 2
    assert manager._magnetic_write_revisions[9] == 2
    assert manager._magnetic_pending_key_writes == {}
    assert manager._magnetic_inflight_key_writes == {}
    assert manager._magnetic_pending_options_write is None
    assert manager._flush_pending_magnetic_writes() is None


def test_final_flush_does_not_duplicate_a_timer_that_already_claimed_the_same_revision():
    manager = _flush_manager()
    manager._magnetic_write_revisions[8] = 5
    manager._magnetic_inflight_key_writes[8] = (5, "timer")
    manager._magnetic_pending_key_writes[8] = (
        SimpleNamespace(),
        [[0x65]],
        5,
        0,
    )

    assert manager._flush_pending_magnetic_writes() is None
    assert manager._magnetic_inflight_key_writes[8] == (5, "timer")
