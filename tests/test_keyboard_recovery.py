"""Regression coverage for the non-destructive header recovery action."""
import inspect
import os
import sys
import threading


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app_flet import QMKManager
from magnetic import MagneticProtocol


def test_recovery_stops_only_diagnostic_state_and_keeps_configuration_untouched():
    manager = QMKManager.__new__(QMKManager)
    complete = threading.Event()
    packets = []
    notices = []
    calls = []
    manager._magnetic_travel_tester = None
    manager._magnetic_calibration = None
    manager._working_hid_path = {"stale": b"old-hid-path"}
    manager._active_device = lambda: {"keyboard_type": "magnetic"}
    manager._stop_magnetic_travel_tester = lambda **_kwargs: calls.append("tester")
    manager._stop_magnetic_calibration = lambda **_kwargs: calls.append("calibration")
    manager._stop_magnetic_profile_switching = lambda: calls.append("profile")
    manager._cancel_pending_magnetic_writes = lambda: calls.append("pending")
    manager._send_magnetic_packets = lambda payload, label: packets.append((payload, label))
    manager._set_keyboard_recovery_busy = lambda busy: calls.append(("busy", busy))
    manager._snack = notices.append
    manager._refresh_battery_for_tray = lambda: calls.append("battery")

    def refresh():
        calls.append("refresh")
        complete.set()

    manager.refresh_devices = refresh
    manager._ui_call = lambda callback: callback()

    QMKManager._recover_keyboard_connection(manager)

    assert complete.wait(1.0)
    assert packets == [
        ([MagneticProtocol.calibration_stop_packet()], "keyboard_recovery_stop_calibration")
    ]
    assert manager._working_hid_path == {}
    assert calls[0] == ("busy", True)
    assert calls[1:5] == ["tester", "calibration", "profile", "pending"]
    assert "refresh" in calls and "battery" in calls
    assert any("Настройки не изменены" in message for message in notices)


def test_recovery_header_control_is_non_destructive_and_before_service_button():
    source = inspect.getsource(QMKManager._build_ui)
    recovery = source.index("self.keyboard_recovery_button")
    service = source.index("self.toggle_button", recovery)
    assert recovery < service
    assert "Настройки не стираются" in source

    recovery_source = inspect.getsource(QMKManager._recover_keyboard_connection)
    assert "calibration_stop_packet" in recovery_source
    assert "factory" in recovery_source.lower()
    assert "reset_packet" not in recovery_source
