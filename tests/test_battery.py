import threading
from datetime import datetime, timedelta

import pytest

from battery import BatteryMonitor, BatteryState


class FakeHidDevice:
    """Stand-in for hid.device(). Records sent feature reports, returns canned response."""
    def __init__(self, response_bytes=None, raise_on_open=False, raise_on_send=False):
        self._response = response_bytes or []
        self._raise_open = raise_on_open
        self._raise_send = raise_on_send
        self.opened_path = None
        self.sent = []
        self.closed = False

    def open_path(self, path):
        if self._raise_open:
            raise IOError("device unavailable")
        self.opened_path = path

    def set_nonblocking(self, value):
        pass

    def send_feature_report(self, data):
        if self._raise_send:
            raise IOError("send failed")
        self.sent.append(list(data))
        return len(data)

    def get_feature_report(self, report_id, length):
        return list(self._response[:length])

    def close(self):
        self.closed = True


def make_monitor(fake_device, config_battery=None, path="\\\\fake\\path"):
    config = config_battery or {
        "query": [0xAB, 0xCD],
        "report_id": 0,
        "response_length": 8,
        "response_offset": 2,
        "response_scale": 1,
        "charging_offset": None,
        "charging_mask": 0,
    }
    return BatteryMonitor(
        config_battery=config,
        usb_lock=threading.Lock(),
        get_device_path=lambda: path,
        hid_device_factory=lambda: fake_device,
    )


def test_initial_state_is_unknown_and_stale():
    monitor = make_monitor(FakeHidDevice(response_bytes=[0, 0, 85, 0, 0, 0, 0, 0]))
    assert monitor.state.percent is None
    assert monitor.state.is_stale is True
    assert monitor.state.charging is False


def test_read_once_parses_percent_at_offset():
    fake = FakeHidDevice(response_bytes=[0xAA, 0xBB, 87, 0, 0, 0, 0, 0])
    monitor = make_monitor(fake)
    monitor.read_once()
    assert monitor.state.percent == 87
    assert monitor.state.is_stale is False
    assert monitor.state.charging is False
    assert fake.sent == [[0, 0xAB, 0xCD]]
    assert fake.closed is True


def test_read_once_uses_default_query_when_device_has_no_saved_query():
    fake = FakeHidDevice(response_bytes=[0, 0, 87, 0, 0, 0, 0, 0])
    config = {
        "query": [], "report_id": 0, "response_length": 8,
        "response_offset": 2, "response_scale": 1,
        "charging_offset": None, "charging_mask": 0,
    }
    monitor = BatteryMonitor(
        config_battery=config,
        usb_lock=threading.Lock(),
        get_device_path=lambda: "\\\\fake\\path",
        hid_device_factory=lambda: fake,
        default_query=[0xF7, 0x00],
    )

    monitor.read_once()

    assert monitor.state.percent == 87
    assert fake.sent == [[0, 0xF7, 0x00]]


def test_read_once_applies_response_scale():
    fake = FakeHidDevice(response_bytes=[0, 0, 50, 0, 0, 0, 0, 0])
    config = {
        "query": [0x01],
        "report_id": 0,
        "response_length": 8,
        "response_offset": 2,
        "response_scale": 2,
        "charging_offset": None,
        "charging_mask": 0,
    }
    monitor = make_monitor(fake, config_battery=config)
    monitor.read_once()
    assert monitor.state.percent == 100  # 50 * 2


def test_read_once_clamps_above_100():
    fake = FakeHidDevice(response_bytes=[0, 0, 200, 0, 0, 0, 0, 0])
    monitor = make_monitor(fake)
    monitor.read_once()
    assert monitor.state.percent == 100


def test_read_once_clamps_below_0():
    # Negative shouldn't happen, but guard anyway. response_scale=-1 to force it.
    fake = FakeHidDevice(response_bytes=[0, 0, 5, 0, 0, 0, 0, 0])
    config = {
        "query": [0x01], "report_id": 0, "response_length": 8,
        "response_offset": 2, "response_scale": -1,
        "charging_offset": None, "charging_mask": 0,
    }
    monitor = make_monitor(fake, config_battery=config)
    monitor.read_once()
    assert monitor.state.percent == 0


def test_read_once_detects_charging():
    # Bit 7 set in byte 3 means charging.
    fake = FakeHidDevice(response_bytes=[0, 0, 60, 0x80, 0, 0, 0, 0])
    config = {
        "query": [0x01], "report_id": 0, "response_length": 8,
        "response_offset": 2, "response_scale": 1,
        "charging_offset": 3, "charging_mask": 0x80,
    }
    monitor = make_monitor(fake, config_battery=config)
    monitor.read_once()
    assert monitor.state.percent == 60
    assert monitor.state.charging is True


def test_read_once_charging_false_when_mask_unset():
    fake = FakeHidDevice(response_bytes=[0, 0, 60, 0x00, 0, 0, 0, 0])
    config = {
        "query": [0x01], "report_id": 0, "response_length": 8,
        "response_offset": 2, "response_scale": 1,
        "charging_offset": 3, "charging_mask": 0x80,
    }
    monitor = make_monitor(fake, config_battery=config)
    monitor.read_once()
    assert monitor.state.charging is False


def test_read_once_no_device_path_marks_stale():
    fake = FakeHidDevice(response_bytes=[0, 0, 50, 0, 0, 0, 0, 0])
    config = {
        "query": [0x01], "report_id": 0, "response_length": 8,
        "response_offset": 2, "response_scale": 1,
        "charging_offset": None, "charging_mask": 0,
    }
    monitor = BatteryMonitor(
        config_battery=config,
        usb_lock=threading.Lock(),
        get_device_path=lambda: None,
        hid_device_factory=lambda: fake,
    )
    monitor.read_once()
    assert monitor.state.percent is None
    assert monitor.state.is_stale is True


def test_read_once_open_failure_marks_stale():
    fake = FakeHidDevice(raise_on_open=True)
    monitor = make_monitor(fake)
    monitor.read_once()
    assert monitor.state.percent is None
    assert monitor.state.is_stale is True


def test_read_once_send_failure_marks_stale_and_closes():
    fake = FakeHidDevice(response_bytes=[0]*8, raise_on_send=True)
    monitor = make_monitor(fake)
    monitor.read_once()
    assert monitor.state.percent is None
    assert monitor.state.is_stale is True
    assert fake.closed is True


def test_read_once_short_response_marks_stale():
    fake = FakeHidDevice(response_bytes=[0, 0])  # offset 2 will IndexError
    monitor = make_monitor(fake)
    monitor.read_once()
    assert monitor.state.percent is None
    assert monitor.state.is_stale is True


def test_read_once_does_not_retain_previous_value_on_failure():
    fake_good = FakeHidDevice(response_bytes=[0, 0, 75, 0, 0, 0, 0, 0])
    monitor = make_monitor(fake_good)
    monitor.read_once()
    assert monitor.state.percent == 75

    # Now swap factory to a failing device.
    fake_bad = FakeHidDevice(raise_on_open=True)
    monitor._make_device = lambda: fake_bad
    monitor.read_once()
    assert monitor.state.percent is None  # Last good value NOT retained.
    assert monitor.state.is_stale is True


def test_zero_response_needs_a_second_poll_before_it_is_shown():
    """A deaf HID endpoint often echoes zeroes; do not flash a fake 0%."""
    fake = FakeHidDevice(response_bytes=[0, 0, 0, 0, 0, 0, 0, 0])
    monitor = make_monitor(fake)

    monitor.read_once()
    assert monitor.state.percent is None
    assert monitor.state.is_stale is True

    monitor.read_once()
    assert monitor.state.percent == 0
    assert monitor.state.is_stale is False


def test_nonzero_response_on_another_hid_path_beats_a_zero_echo():
    zero = FakeHidDevice(response_bytes=[0, 0, 0, 0, 0, 0, 0, 0])
    full = FakeHidDevice(response_bytes=[0, 0, 72, 0, 0, 0, 0, 0])
    devices = iter([zero, full])
    monitor = BatteryMonitor(
        config_battery={
            "query": [0xAB, 0xCD], "report_id": 0, "response_length": 8,
            "response_offset": 2, "response_scale": 1,
            "charging_offset": None, "charging_mask": 0,
        },
        usb_lock=threading.Lock(),
        get_device_path=lambda: "\\\\fake\\fallback",
        get_device_paths=lambda: ["\\\\fake\\zero", "\\\\fake\\battery"],
        hid_device_factory=lambda: next(devices),
    )

    monitor.read_once()

    assert monitor.state.percent == 72
    assert monitor.state.is_stale is False
    assert zero.closed is True
    assert full.closed is True
