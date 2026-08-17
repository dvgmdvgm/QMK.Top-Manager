"""Regression tests for Snap Key's cache and read-only keycap values."""
import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app_flet as app_module
from app_flet import QMKManager
from magnetic import KeyMagneticSettings, MagneticProtocol


def test_known_snap_slots_include_live_cache_and_each_magnetic_preset():
    entry = {
        "magnetic_key_modes": {"8": MagneticProtocol.MODE_SNAP, "9": 0x80},
        "magnetic_profiles": {
            "0": {"key_modes": {"20": MagneticProtocol.MODE_SNAP}},
            "1": {"key_modes": {"26": MagneticProtocol.MODE_SNAP, "200": MagneticProtocol.MODE_SNAP}},
        },
    }
    manager = SimpleNamespace(_active_device=lambda: entry)

    assert QMKManager._known_snap_key_slots(manager) == [8, 20, 26]


def test_known_snap_slots_snapshots_maps_before_a_background_worker_can_replace_them():
    """The Snap summary must not iterate a live profile map during HID work."""
    entry = {
        "magnetic_key_modes": {"8": MagneticProtocol.MODE_SNAP},
        "magnetic_profiles": {"0": {"key_modes": {"20": MagneticProtocol.MODE_SNAP}}},
    }
    manager = SimpleNamespace(_active_device=lambda: entry)
    started = threading.Event()
    completed = threading.Event()
    result = []

    def read_slots():
        started.set()
        result.extend(QMKManager._known_snap_key_slots(manager))
        completed.set()

    with app_module._CONFIG_WRITE_LOCK:
        worker = threading.Thread(target=read_slots)
        worker.start()
        assert started.wait(1)
        # The reader cannot obtain a half-replaced nested profile map.
        assert not completed.wait(0.05)
        entry["magnetic_profiles"] = {
            "0": {"key_modes": {"26": MagneticProtocol.MODE_SNAP}}
        }

    worker.join(1)
    assert completed.is_set()
    assert result == [8, 26]


def test_clear_snap_cache_preserves_per_key_magnetic_values_and_normal_modes():
    key_settings = {"8": {"actuation": 1.2, "rapid_trigger": True}}
    entry = {
        "magnetic_key_settings": key_settings.copy(),
        "magnetic_key_modes": {"8": MagneticProtocol.MODE_SNAP, "9": 0x80},
        "magnetic_profiles": {
            "0": {
                "key_settings": key_settings.copy(),
                "key_modes": {"8": MagneticProtocol.MODE_SNAP, "9": 0x80},
            },
            "1": {"key_settings": {}, "key_modes": {"20": MagneticProtocol.MODE_SNAP}},
        },
    }

    assert QMKManager._clear_snap_modes_from_entry(entry, [8, 20]) is True
    assert entry["magnetic_key_settings"] == key_settings
    assert entry["magnetic_profiles"]["0"]["key_settings"] == key_settings
    assert entry["magnetic_key_modes"] == {"8": 0x80, "9": 0x80}
    assert entry["magnetic_profiles"]["0"]["key_modes"] == {"8": 0x80, "9": 0x80}
    assert entry["magnetic_profiles"]["1"]["key_modes"] == {"20": 0}


def test_snap_keycap_keeps_compact_actuation_and_rt_values():
    settings = KeyMagneticSettings(1.14, True, 0.15, 0.30, 0.05, 0.10)
    manager = SimpleNamespace(_magnetic_settings_for_keyboard=lambda _slot: settings)

    assert QMKManager._magnetic_key_caption(manager, 8) == ("1.14", "RT 0.30/0.15")
    assert QMKManager._magnetic_key_compact_caption(manager, 8, settings, True) == "1.14 · ↑0.30"
