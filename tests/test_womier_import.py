"""Regression coverage for the read-only Womier Driver magnetic importer."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app_flet as app_module
from app_flet import QMKManager
from womier_import import (
    WomierMagneticImport,
    _snappy_uncompress,
    decode_womier_magnetic_profiles,
)


def _official_profile(index, *, travel, deactivation=None, fire, press, lift, lower, upper):
    # Twelve recognised SK75 HID usages are enough to prove this is not a
    # generic stale Womier record from another keyboard.
    return {
        "profile": index,
        "modes": [
            {
                "original": hid,
                "index": 0,
                "option": "normal",
                "travel": travel,
                "liftTravel": travel if deactivation is None else deactivation,
                "fire": fire,
                "firePressTravel": press,
                "fireLiftTravel": lift,
                "deadZoneTravel": lower,
                "topDeadZoneTravel": upper,
            }
            for hid in (4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)
        ],
    }


def test_snappy_decoder_reads_literal_and_copy_stream():
    # Uncompressed length=12; literal "abcd", then two copy-1 operations.
    # This is the exact raw block format LevelDB uses.
    encoded = bytes([12, 12, ord("a"), ord("b"), ord("c"), ord("d"), 1, 4, 1, 4])
    assert _snappy_uncompress(encoded) == b"abcdabcdabcd"


def test_official_womier_profiles_convert_to_sk75_presets():
    raw = {
        "磁轴": [
            _official_profile(0, travel=1.2, deactivation=2.4, fire=True, press=0.3, lift=0.2, lower=0.3, upper=0.1),
            _official_profile(1, travel=0.1, fire=True, press=0.01, lift=0.01, lower=0.0, upper=0.0),
            _official_profile(2, travel=0.6, fire=False, press=0.2, lift=0.2, lower=0.0, upper=0.0),
            _official_profile(3, travel=3.0, fire=False, press=0.3, lift=0.3, lower=0.3, upper=0.0),
        ]
    }

    converted = decode_womier_magnetic_profiles("DeviceTest_02_2518", raw)

    assert converted is not None
    assert converted.imported_profile_count == 4
    first = converted.profiles["0"]
    # HID A is slot 9 in the SK75 matrix.
    assert first["key_settings"]["9"] == {
        "actuation": 1.2,
        "rapid_trigger": True,
        "rapid_press": 0.3,
        "rapid_release": 0.2,
        "lower_dead_zone": 0.3,
        "upper_dead_zone": 0.1,
        "deactivation": 2.4,
    }
    assert first["rt_separate"]["9"] is True
    assert converted.profiles["1"]["key_settings"]["9"]["actuation"] == 0.1
    assert converted.profiles["3"]["key_settings"]["9"]["actuation"] == 3.0


def test_startup_import_replaces_only_active_magnetic_sk75_presets(monkeypatch):
    imported = WomierMagneticImport(
        storage_key="DeviceTest_02_2518",
        profiles={
            str(index): {
                "key_settings": {
                    "8": {
                        "actuation": 0.1 + index,
                        "rapid_trigger": True,
                        "rapid_press": 0.1,
                        "rapid_release": 0.1,
                        "lower_dead_zone": 0.0,
                        "upper_dead_zone": 0.0,
                    }
                },
                "key_modes": {"8": 128},
                "keyboard_options": {},
                "rt_separate": {"8": False},
                "initialized": True,
            }
            for index in range(4)
        },
        imported_profile_count=4,
    )
    monkeypatch.setattr(app_module, "find_womier_magnetic_import", lambda: imported)
    monkeypatch.setattr(app_module, "womier_storage_fingerprint", lambda: "snapshot")
    data = {
        "settings": {},
        "active_device": "sk75",
        "devices": {
            "sk75": {
                "vid": 0x3151,
                "pid": 0x5030,
                "usage_page": 0xFFFF,
                "keyboard_type": "magnetic",
                "magnetic_selected_profile": 2,
                "magnetic_profiles": {},
            },
            "other": {
                "vid": 1,
                "pid": 2,
                "usage_page": 3,
                "keyboard_type": "mechanical",
                "magnetic_profiles": {"0": {"key_settings": {"8": {"actuation": 2.0}}}},
            },
        },
    }
    manager = QMKManager.__new__(QMKManager)

    output, report = manager._import_womier_magnetic_profiles_data(data)

    assert output is data
    assert report["profiles_imported"] == 4
    entry = data["devices"]["sk75"]
    assert entry["magnetic_profiles"]["0"]["key_settings"]["8"]["actuation"] == 0.1
    assert entry["magnetic_profiles"]["3"]["key_settings"]["8"]["actuation"] == 3.1
    # The visible cache follows the selected profile rather than profile 1.
    assert entry["magnetic_key_settings"]["8"]["actuation"] == 2.1
    assert entry["magnetic_profiles_before_womier_import"]
    assert data["devices"]["other"]["magnetic_profiles"]["0"]["key_settings"]["8"]["actuation"] == 2.0

    # Marker makes the import one-shot so future local edits are not clobbered.
    _, second_report = manager._import_womier_magnetic_profiles_data(data)
    assert second_report is None
