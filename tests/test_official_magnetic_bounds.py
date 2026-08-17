"""Regression coverage for official SK75 magnetic-value bounds."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app_flet import MAGNETIC_PROFILE_COUNT, QMKManager, _sanitize_magnetic_settings_mapping
from womier_import import decode_womier_magnetic_profiles


def _wide_settings():
    return {
        "actuation": 3.50,
        "rapid_trigger": True,
        "rapid_press": 2.50,
        "rapid_release": 2.50,
        "lower_dead_zone": 1.50,
        "upper_dead_zone": 1.50,
    }


def test_config_sanitizer_clamps_only_local_data_without_needing_a_manager_or_hid():
    source = {"8": _wide_settings()}

    sanitized = _sanitize_magnetic_settings_mapping(source)

    # The input remains untouched: config migration is a pure copy operation.
    assert source["8"]["actuation"] == 3.50
    assert sanitized["8"] == {
        "actuation": 3.30,
        "rapid_trigger": True,
        "rapid_press": 2.00,
        "rapid_release": 2.00,
        "lower_dead_zone": 1.00,
        "upper_dead_zone": 1.00,
        "deactivation": 3.30,
    }


def test_device_normalization_migrates_live_and_all_preset_values_without_hid_writes():
    manager = QMKManager.__new__(QMKManager)
    entry = {
        "keyboard_type": "magnetic",
        "payloads": {f"P{index}": {} for index in range(MAGNETIC_PROFILE_COUNT)},
        "bindings": [],
        "battery": {},
        "magnetic_key_settings": {"8": _wide_settings()},
        "magnetic_profiles": {
            str(index): {
                "key_settings": {"8": _wide_settings()},
                "key_modes": {},
                "keyboard_options": {},
                "rt_separate": {},
                "initialized": True,
            }
            for index in range(MAGNETIC_PROFILE_COUNT)
        },
    }

    manager._normalize_device_entry(entry)

    assert entry["magnetic_key_settings"]["8"]["actuation"] == 3.30
    for index in range(MAGNETIC_PROFILE_COUNT):
        migrated = entry["magnetic_profiles"][str(index)]["key_settings"]["8"]
        assert migrated["actuation"] == 3.30
        assert migrated["rapid_press"] == 2.00
        assert migrated["rapid_release"] == 2.00
        assert migrated["lower_dead_zone"] == 1.00
        assert migrated["upper_dead_zone"] == 1.00


def test_official_cache_import_uses_same_current_sk75_bounds():
    raw = {
        "磁轴": [
            {
                "profile": index,
                "modes": [
                    {
                        "original": hid,
                        "travel": 3.50,
                        "liftTravel": 3.50,
                        "fire": True,
                        "firePressTravel": 2.50,
                        "fireLiftTravel": 2.50,
                        "deadZoneTravel": 1.50,
                        "topDeadZoneTravel": 1.50,
                        "option": "normal",
                    }
                    for hid in range(4, 16)
                ],
            }
            for index in range(MAGNETIC_PROFILE_COUNT)
        ]
    }

    imported = decode_womier_magnetic_profiles("DeviceTest_02_2518", raw)

    assert imported is not None
    values = imported.profiles["0"]["key_settings"]
    # HID A maps to physical SK75 slot 9.
    assert values["9"]["actuation"] == 3.30
    assert values["9"]["deactivation"] == 3.30
    assert values["9"]["rapid_press"] == 2.00
    assert values["9"]["rapid_release"] == 2.00
