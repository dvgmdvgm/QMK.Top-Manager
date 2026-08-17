"""Tests for safe migration of the previous local configuration."""
import os
import sys
import asyncio
import threading
import json
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app_flet as app_module
from app_flet import (
    DEFAULT_PROFILE_SWITCH_DELAY_MS,
    MAGNETIC_PROFILE_COUNT,
    MAX_PROFILE_SWITCH_DELAY_MS,
    QMKManager,
    _merge_legacy_config,
    _normalise_imported_configuration,
    _resolved_profile_switch_delay_ms,
    _write_json_atomically,
)
from magnetic import KeyMagneticSettings, MagneticProtocol


def _profile(slot: int, **extra):
    data = {"data": [4, slot], **extra}
    return data


class TestProfileSwitchDelay:
    def test_default_delay_is_short_and_safe(self):
        assert _resolved_profile_switch_delay_ms({}) == DEFAULT_PROFILE_SWITCH_DELAY_MS

    def test_delay_is_clamped_to_safe_range(self):
        assert _resolved_profile_switch_delay_ms({"profile_switch_delay_ms": -50}) == 0
        assert _resolved_profile_switch_delay_ms({"profile_switch_delay_ms": 99999}) == MAX_PROFILE_SWITCH_DELAY_MS

    def test_delay_accepts_pasted_string_value(self):
        assert _resolved_profile_switch_delay_ms({"profile_switch_delay_ms": "150"}) == 150


def test_magnetic_timer_and_separate_rt_mutations_wait_for_detached_config_snapshot(monkeypatch):
    """A HID timer must not mutate nested profiles while save_config serialises.

    The UI switch and the delayed magnetic write run on different threads.  A
    shared state lock keeps both mutations outside the JSON-copy interval, so
    a rapid separate-RT toggle cannot surface Python's ``dictionary changed
    size during iteration`` error.
    """
    manager = QMKManager.__new__(QMKManager)
    manager.is_running = False
    manager.magnetic_profile_index = 0
    manager.config = {
        "mode": "auto",
        "active_device": "sk75",
        "devices": {
            "sk75": {
                "magnetic_key_settings": {},
                "magnetic_key_modes": {},
                "magnetic_rt_separate": {},
                "magnetic_profiles": {
                    str(index): {
                        "key_settings": {},
                        "key_modes": {},
                        "keyboard_options": {},
                        "rt_separate": {},
                        "initialized": True,
                    }
                    for index in range(MAGNETIC_PROFILE_COUNT)
                },
            }
        },
    }

    snapshot_started = threading.Event()
    allow_snapshot_to_finish = threading.Event()
    switch_finished = threading.Event()
    timer_finished = threading.Event()
    errors = []
    original_copy = app_module._json_copy

    def held_copy(value):
        if isinstance(value, dict) and "devices" in value and not snapshot_started.is_set():
            snapshot_started.set()
            assert allow_snapshot_to_finish.wait(2)
        return original_copy(value)

    monkeypatch.setattr(app_module, "_json_copy", held_copy)
    monkeypatch.setattr(app_module, "_write_json_atomically", lambda *_args: None)

    save_thread = threading.Thread(target=lambda: manager.save_config())

    def flip_separate_rt():
        try:
            manager._store_magnetic_rt_separate(8, True)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            switch_finished.set()

    def complete_delayed_write():
        try:
            manager._cache_magnetic_settings(
                8,
                KeyMagneticSettings(1.2, True, 0.3, 0.3, 0.1, 0.0),
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            timer_finished.set()

    save_thread.start()
    assert snapshot_started.wait(2)
    switch_thread = threading.Thread(target=flip_separate_rt)
    timer_thread = threading.Thread(target=complete_delayed_write)
    switch_thread.start()
    timer_thread.start()

    # Both writers must wait for the detached snapshot, rather than modifying
    # a dictionary while json.dumps() walks it.
    assert not switch_finished.wait(0.05)
    assert not timer_finished.wait(0.05)
    allow_snapshot_to_finish.set()

    save_thread.join(2)
    switch_thread.join(2)
    timer_thread.join(2)
    assert not save_thread.is_alive()
    assert not switch_thread.is_alive()
    assert not timer_thread.is_alive()
    assert errors == []
    entry = manager.config["devices"]["sk75"]
    assert entry["magnetic_rt_separate"]["8"] is True
    assert entry["magnetic_profiles"]["0"]["key_settings"]["8"]["actuation"] == 1.2


class TestLegacyConfigMerge:
    def test_merges_names_options_and_missing_processes_without_overwriting_hid_data(self):
        current = {
            "mode": "auto",
            "settings": {"browser_path": ""},
            "active_device": "3151:5030:ffff",
            "devices": {
                "3151:5030:ffff": {
                    "vid": 0x3151,
                    "pid": 0x5030,
                    "usage_page": 0xFFFF,
                    "keyboard_type": "magnetic",
                    "cooldown_ms": 100,
                    "payloads": {
                        "Профиль 1": _profile(0),
                        "Профиль 2": _profile(1),
                        "Профиль 3": _profile(2),
                        "Профиль 4": _profile(3),
                    },
                    "bindings": [{"process": "cs2.exe", "profile_index": 0, "enabled": False}],
                    "magnetic_key_settings": {"14": {"actuation": 1.4}},
                }
            },
        }
        legacy = {
            "settings": {"browser_path": "C:/old/chrome.exe"},
            "devices": {
                "3151:5030:ffff": {
                    "vid": 0x3151,
                    "pid": 0x5030,
                    "usage_page": 0xFFFF,
                    "keyboard_type": "magnetic",
                    "payloads": {
                        "typing": {"hotkey": "ctrl+1", "polling_rate": 1000},
                        "game": {"hotkey": "ctrl+2", "polling_rate": 8000},
                    },
                    "bindings": [
                        {"process": "CS2.EXE", "profile_index": 2},
                        {"process": "osu!.exe", "profile_index": 1},
                    ],
                }
            },
        }

        merged, report = _merge_legacy_config(current, legacy)
        entry = merged["devices"]["3151:5030:ffff"]

        assert list(entry["payloads"])[:2] == ["typing", "game"]
        assert entry["payloads"]["typing"]["data"] == [4, 0]
        assert entry["payloads"]["game"]["polling_rate"] == 8000
        assert "hotkey" not in entry["payloads"]["typing"]
        assert entry["magnetic_key_settings"] == {"14": {"actuation": 1.4}}
        assert entry["bindings"] == [
            {"process": "cs2.exe", "profile_index": 0, "enabled": False},
            {"process": "osu!.exe", "profile_index": 1},
        ]
        assert report["bindings_added"] == 1
        # The function does not mutate the source dictionaries supplied by the caller.
        assert list(current["devices"]["3151:5030:ffff"]["payloads"]) == [
            "Профиль 1", "Профиль 2", "Профиль 3", "Профиль 4"
        ]

    def test_normalizer_preserves_disabled_rule_state(self):
        manager = QMKManager.__new__(QMKManager)
        entry = {
            "vid": 1,
            "pid": 2,
            "usage_page": 3,
            "keyboard_type": "magnetic",
            "payloads": {f"P{slot}": {} for slot in range(4)},
            "bindings": [{"process": "Game.EXE", "profile_index": 1, "enabled": False}],
            "battery": {},
        }
        manager._normalize_device_entry(entry)
        assert entry["bindings"] == [{"process": "game.exe", "profile_index": 1, "enabled": False}]


def test_magnetic_profile_slots_migrate_legacy_live_values_without_shared_aliases():
    """One old global magnetic cache becomes four independent local presets."""
    manager = QMKManager.__new__(QMKManager)
    entry = {
        "vid": 1,
        "pid": 2,
        "usage_page": 3,
        "keyboard_type": "magnetic",
        "payloads": {f"P{slot}": {} for slot in range(4)},
        "bindings": [],
        "battery": {},
        "magnetic_key_settings": {
            "8": {
                "actuation": 1.25,
                "rapid_trigger": True,
                "rapid_press": 0.15,
                "rapid_release": 0.2,
                "lower_dead_zone": 0.05,
                "upper_dead_zone": 0.1,
            }
        },
        "magnetic_key_modes": {"8": 128},
        "magnetic_keyboard_options": {"fn_index": 0, "anti_accidental": False, "rt_stab": 25, "wasd_swap": False, "system": "win"},
    }

    manager._normalize_device_entry(entry)

    assert set(entry["magnetic_profiles"]) == {str(index) for index in range(MAGNETIC_PROFILE_COUNT)}
    assert entry["magnetic_profiles"]["0"]["key_settings"] == entry["magnetic_key_settings"]
    assert entry["magnetic_profiles"]["3"]["keyboard_options"]["rt_stab"] == 25
    entry["magnetic_profiles"]["1"]["key_settings"]["8"]["actuation"] = 2.0
    assert entry["magnetic_profiles"]["0"]["key_settings"]["8"]["actuation"] == 1.25


def test_live_read_seeds_only_empty_magnetic_presets():
    manager = QMKManager.__new__(QMKManager)
    manager.config = {
        "active_device": "keyboard",
        "devices": {
            "keyboard": {
                "magnetic_key_settings": {"8": {"actuation": 1.1}},
                "magnetic_key_modes": {"8": 128},
                "magnetic_keyboard_options": {"rt_stab": 25},
                "magnetic_rt_separate": {},
                "magnetic_profiles": {
                    "0": {"key_settings": {"8": {"actuation": 2.0}}, "key_modes": {}, "keyboard_options": {}, "rt_separate": {}, "initialized": True},
                    "1": {"key_settings": {}, "key_modes": {}, "keyboard_options": {}, "rt_separate": {}, "initialized": False},
                    "2": {"key_settings": {}, "key_modes": {}, "keyboard_options": {}, "rt_separate": {}, "initialized": False},
                    "3": {"key_settings": {}, "key_modes": {}, "keyboard_options": {}, "rt_separate": {}, "initialized": False},
                },
            }
        },
    }

    assert manager._seed_uninitialized_magnetic_profiles(manager._active_device()) is True
    assert manager._active_device()["magnetic_profiles"]["0"]["key_settings"]["8"]["actuation"] == 2.0
    assert manager._active_device()["magnetic_profiles"]["1"]["key_settings"]["8"]["actuation"] == 1.1


def test_magnetic_presets_are_kept_in_portable_config_export():
    manager = QMKManager.__new__(QMKManager)
    manager.config = {
        "mode": "auto",
        "devices": {
            "keyboard": {
                "payloads": {},
                "magnetic_profiles": {
                    "0": {"key_settings": {"8": {"actuation": 1.0}}, "initialized": True}
                },
            }
        },
        "active_device": "keyboard",
        # Runtime aliases must not be present in a portable export.
        "payloads": {},
        "bindings": [],
        "battery": {},
        "device": None,
    }

    exported = manager._exportable_config()

    assert exported["config"]["devices"]["keyboard"]["magnetic_profiles"]["0"]["key_settings"]["8"] == {"actuation": 1.0}


def test_copy_paste_round_trip_keeps_all_persistent_configuration_sections():
    """A copy export must survive import with every current feature intact."""
    manager = QMKManager.__new__(QMKManager)
    manager.config = {
        "mode": "auto",
        "settings": {
            "start_minimized": True,
            "autostart_service": False,
            "profile_switch_delay_ms": 150,
            "custom_future_setting": {"kept": True},
        },
        "active_device": "3151:5030:ffff",
        "devices": {
            "3151:5030:ffff": {
                "vid": 0x3151,
                "pid": 0x5030,
                "usage_page": 0xFFFF,
                "keyboard_type": "magnetic",
                "label": "SK75 TMR",
                "payloads": {
                    "gaming": {
                        "data": [4, 1],
                        "polling_rate": 8000,
                        "lighting_profile": 3,
                        # A removed manual-mode field must never return after
                        # an import, but every supported payload field stays.
                        "hotkey": "ctrl+alt+1",
                    }
                },
                "bindings": [
                    {"process": "game.exe", "profile_index": 1, "enabled": True}
                ],
                "battery": {"query": [1, 2, 3], "report_id": 4},
                "lighting_lab": {
                    "effect_index": 7,
                    "brightness": 5,
                    "speed": 2,
                    "color_hex": "#F0A020",
                    "custom_colors": True,
                },
                "magnetic_selected_profile": 2,
                "magnetic_key_settings": {
                    "8": {
                        "actuation": 1.2,
                        "rapid_trigger": True,
                        "rapid_press": 0.3,
                        "rapid_release": 0.3,
                        "lower_dead_zone": 0.2,
                        "upper_dead_zone": 0.1,
                    }
                },
                "magnetic_key_modes": {"8": 128},
                "magnetic_keyboard_options": {
                    "rt_stab": 50,
                    "anti_accidental": True,
                    "fn_index": 3,
                },
                "magnetic_rt_separate": {"8": True},
                "magnetic_profiles": {
                    "2": {
                        "key_settings": {"8": {"actuation": 1.2}},
                        "key_modes": {"8": 128},
                        "keyboard_options": {"rt_stab": 50},
                        "rt_separate": {"8": True},
                        "initialized": True,
                    }
                },
            }
        },
        # Runtime aliases are intentionally not portable.
        "payloads": {"gaming": {"data": [4, 1]}},
        "bindings": [],
        "battery": {},
        "device": {"vid": 0x3151},
    }

    export = manager._exportable_config()
    clipboard_json = json.dumps(export, ensure_ascii=False)
    restored = _normalise_imported_configuration(json.loads(clipboard_json))

    assert restored == export["config"]
    device = restored["devices"]["3151:5030:ffff"]
    assert device["bindings"][0]["process"] == "game.exe"
    assert device["lighting_lab"]["color_hex"] == "#F0A020"
    assert device["magnetic_profiles"]["2"]["key_settings"]["8"]["actuation"] == 1.2
    assert "hotkey" not in device["payloads"]["gaming"]
    restored["devices"]["3151:5030:ffff"]["magnetic_profiles"]["2"]["initialized"] = False
    assert export["config"]["devices"]["3151:5030:ffff"]["magnetic_profiles"]["2"]["initialized"] is True


def test_atomic_config_write_keeps_previous_file_when_serialization_fails(tmp_path):
    """A bad export/import value must not truncate an existing config file."""
    config_path = tmp_path / "profiles_config.json"
    config_path.write_text('{"saved": true}', encoding="utf-8")

    with pytest.raises(TypeError):
        _write_json_atomically(config_path, {"not_json": {"a", "set"}})

    assert json.loads(config_path.read_text(encoding="utf-8")) == {"saved": True}
    assert not list(tmp_path.glob(".profiles_config.json.*.tmp"))


def test_magnetic_profile_selection_uses_new_preset_immediately_and_schedules_apply():
    """The dropdown must not lag one Flet paint behind the preset cache."""
    manager = QMKManager.__new__(QMKManager)
    manager.config = {
        "active_device": "keyboard",
        "devices": {
            "keyboard": {
                "keyboard_type": "magnetic",
                "magnetic_selected_profile": 0,
                "magnetic_profiles": {
                    str(index): {
                        "key_settings": {},
                        "key_modes": {},
                        "keyboard_options": {},
                        "rt_separate": {},
                        "initialized": True,
                    }
                    for index in range(MAGNETIC_PROFILE_COUNT)
                },
            }
        },
    }
    manager.magnetic_profile_index = 0
    # Simulate Flet retaining the old render value while dispatching an event.
    manager.magnetic_profile_dropdown = SimpleNamespace(value="0")
    calls = []
    manager._store_magnetic_controls_in_profile = lambda index: calls.append(("store", index))
    manager._cancel_pending_magnetic_writes = lambda: calls.append(("cancel",))
    manager.save_config = lambda **_kwargs: calls.append(("save",))
    manager._load_magnetic_controls = lambda slot: calls.append(("load", slot))
    manager.magnetic_actuation_slider = object()
    manager.magnetic_selected_slot = 8
    manager.magnetic_rt_stab_dropdown = SimpleNamespace(value="25")
    manager.magnetic_anti_accidental_switch = SimpleNamespace(value=False)
    manager._cached_magnetic_keyboard_options = lambda: SimpleNamespace(rt_stab=25, anti_accidental=False)
    manager._refresh_sk75_keyboard_picker = lambda: calls.append(("refresh",))
    manager._set_magnetic_status = lambda *args: calls.append(("status",))
    manager._schedule_magnetic_profile_apply = lambda index: calls.append(("apply", index))

    manager._on_magnetic_profile_changed(
        # Flet may dispatch the fresh selection in ``data`` before its
        # Dropdown control has painted the new value.
        SimpleNamespace(control=SimpleNamespace(value="0"), data="1")
    )

    assert manager.config["devices"]["keyboard"]["magnetic_selected_profile"] == 1
    assert manager.magnetic_profile_dropdown.value == "1"
    assert manager._selected_magnetic_profile_index() == 1
    assert ("store", 0) in calls
    assert ("apply", 1) in calls


def test_magnetic_profile_selection_loads_each_preset_values_into_controls():
    """Profile 2/3 must not render the values left over from profile 1."""
    def key_values(actuation, press, release, lower, upper):
        return {
            "actuation": actuation,
            "rapid_trigger": True,
            "rapid_press": press,
            "rapid_release": release,
            "lower_dead_zone": lower,
            "upper_dead_zone": upper,
        }

    profiles = {
        "0": {
            "key_settings": {"8": key_values(1.10, 0.11, 0.12, 0.01, 0.02)},
            "key_modes": {"8": 0x80},
            "keyboard_options": {"fn_index": 0, "anti_accidental": False, "rt_stab": 25, "wasd_swap": False, "system": "win"},
            "rt_separate": {"8": True},
            "initialized": True,
        },
        "1": {
            "key_settings": {"8": key_values(2.20, 0.21, 0.22, 0.03, 0.04)},
            "key_modes": {"8": 0x80},
            "keyboard_options": {"fn_index": 0, "anti_accidental": True, "rt_stab": 50, "wasd_swap": False, "system": "win"},
            "rt_separate": {"8": True},
            "initialized": True,
        },
        "2": {
            "key_settings": {"8": key_values(3.10, 0.31, 0.32, 0.05, 0.06)},
            "key_modes": {"8": 0x80},
            "keyboard_options": {"fn_index": 0, "anti_accidental": False, "rt_stab": 75, "wasd_swap": False, "system": "win"},
            "rt_separate": {"8": True},
            "initialized": True,
        },
        "3": {"key_settings": {}, "key_modes": {}, "keyboard_options": {}, "rt_separate": {}, "initialized": False},
    }
    entry = {
        "keyboard_type": "magnetic",
        "magnetic_selected_profile": 0,
        "magnetic_key_settings": {"8": key_values(1.10, 0.11, 0.12, 0.01, 0.02)},
        "magnetic_key_modes": {"8": 0x80},
        "magnetic_keyboard_options": {"fn_index": 0, "anti_accidental": False, "rt_stab": 25, "wasd_swap": False, "system": "win"},
        "magnetic_profiles": profiles,
    }
    manager = QMKManager.__new__(QMKManager)
    manager.config = {"active_device": "keyboard", "devices": {"keyboard": entry}}
    manager.magnetic_profile_index = 0
    manager.magnetic_profile_dropdown = SimpleNamespace(value="0")
    manager.magnetic_selected_slot = 8
    manager.magnetic_rt_switch = SimpleNamespace(value=True, update=lambda: None)
    manager.magnetic_rt_separate_switch = SimpleNamespace(value=True, label="", update=lambda: None)
    manager.magnetic_rt_stab_dropdown = SimpleNamespace(value="25")
    manager.magnetic_anti_accidental_switch = SimpleNamespace(value=False)
    for name, value in (
        ("actuation", 110),
        ("rt_press", 11),
        ("rt_release", 12),
        ("lower_dead_zone", 1),
        ("upper_dead_zone", 2),
    ):
        setattr(manager, f"magnetic_{name}_slider", SimpleNamespace(value=value))
    manager.magnetic_rt_press_control = SimpleNamespace(visible=True, update=lambda: None)
    manager._set_vertical_magnetic_value = (
        lambda state, value, **_kwargs: setattr(state, "value", value)
    )
    manager._update_magnetic_rt_label = lambda update=True: None
    manager._cancel_pending_magnetic_writes = lambda: None
    manager._refresh_sk75_keyboard_picker = lambda: None
    manager._set_magnetic_status = lambda *args: None
    manager._schedule_magnetic_profile_apply = lambda index: None
    manager.save_config = lambda **_kwargs: None

    manager._on_magnetic_profile_changed(SimpleNamespace(control=SimpleNamespace(value="1")))

    assert manager.magnetic_profile_index == 1
    assert round(manager.magnetic_actuation_slider.value) == 220
    assert manager.magnetic_rt_press_slider.value == 21
    assert manager.magnetic_rt_release_slider.value == 22
    assert manager.magnetic_lower_dead_zone_slider.value == 3
    assert manager.magnetic_upper_dead_zone_slider.value == 4
    assert manager.magnetic_rt_stab_dropdown.value == "50"
    assert manager.magnetic_anti_accidental_switch.value is True

    manager._on_magnetic_profile_changed(SimpleNamespace(control=SimpleNamespace(value="2")))

    assert manager.magnetic_profile_index == 2
    assert round(manager.magnetic_actuation_slider.value) == 310
    assert manager.magnetic_rt_press_slider.value == 31
    assert manager.magnetic_rt_release_slider.value == 32
    assert manager.magnetic_lower_dead_zone_slider.value == 5
    assert manager.magnetic_upper_dead_zone_slider.value == 6
    assert manager.magnetic_rt_stab_dropdown.value == "75"
    assert manager.magnetic_anti_accidental_switch.value is False


def test_populated_magnetic_profile_is_not_reseeded_when_old_initialized_flag_is_false():
    """An old stale flag must not destroy a saved profile at startup."""
    manager = QMKManager.__new__(QMKManager)
    entry = {
        "vid": 1,
        "pid": 2,
        "usage_page": 3,
        "keyboard_type": "magnetic",
        "payloads": {f"P{slot}": {} for slot in range(4)},
        "bindings": [],
        "battery": {},
        "magnetic_profiles": {
            "0": {"key_settings": {"8": {"actuation": 1.0}}, "initialized": True},
            "1": {"key_settings": {"8": {"actuation": 2.0}}, "initialized": False},
        },
    }

    manager._normalize_device_entry(entry)

    assert entry["magnetic_profiles"]["0"]["initialized"] is True
    assert entry["magnetic_profiles"]["1"]["initialized"] is True
    assert entry["magnetic_profiles"]["1"]["key_settings"]["8"]["actuation"] == 2.0


def test_regular_magnetic_profile_switch_selects_matching_local_preset():
    """The automatic/profile-payload path must not leave preset 1 selected."""
    manager = QMKManager.__new__(QMKManager)
    entry = {
        "keyboard_type": "magnetic",
        "payloads": {
            "typing": {"data": [4, 0]},
            "gaming": {"data": [4, 1]},
            "work": {"data": [4, 2]},
            "media": {"data": [4, 3]},
        },
        "battery": {},
    }
    manager.config = {
        "active_device": "keyboard",
        "devices": {"keyboard": entry},
        "payloads": entry["payloads"],
    }
    manager.usb_lock = threading.Lock()
    manager._send_hid_payload = lambda payload, label: "hid-path"
    selected = []
    manager._select_magnetic_preset_for_keyboard_profile = lambda index: selected.append(index)

    manager.apply_payload("work", [4, 2])

    assert selected == [2]
    assert manager.current_binding == "work"


def test_selected_magnetic_profile_applies_its_values_to_live_cache(monkeypatch):
    """A local preset must update the actual keyboard cache, not just the UI."""
    settings = {
        "actuation": 2.0,
        "rapid_trigger": True,
        "rapid_press": 0.2,
        "rapid_release": 0.2,
        "lower_dead_zone": 0.05,
        "upper_dead_zone": 0.1,
    }
    live_settings = {**settings, "actuation": 1.0}
    manager = QMKManager.__new__(QMKManager)
    entry = {
        "keyboard_type": "magnetic",
        "magnetic_selected_profile": 1,
        # The app cache is deliberately stale in the dangerous direction: it
        # already claims the target 2.00 mm value while the keyboard readback
        # below still reports 1.00 mm.  Profile selection must write based on
        # the physical matrix, not skip the HID transaction from this cache.
        "magnetic_key_settings": {"8": settings},
        "magnetic_key_modes": {"8": 0x80},
        "magnetic_keyboard_options": {},
        "magnetic_profiles": {
            "0": {"key_settings": {"8": live_settings}, "key_modes": {"8": 0x80}, "keyboard_options": {}, "rt_separate": {}, "initialized": True},
            "1": {"key_settings": {"8": settings}, "key_modes": {"8": 0x80}, "keyboard_options": {}, "rt_separate": {}, "initialized": True},
            "2": {"key_settings": {}, "key_modes": {}, "keyboard_options": {}, "rt_separate": {}, "initialized": False},
            "3": {"key_settings": {}, "key_modes": {}, "keyboard_options": {}, "rt_separate": {}, "initialized": False},
        },
    }
    manager.config = {"active_device": "keyboard", "devices": {"keyboard": entry}}
    manager.magnetic_profile_index = 1
    manager.magnetic_profile_dropdown = SimpleNamespace(value="1")
    manager._magnetic_profile_switch_lock = threading.Lock()
    manager._magnetic_profile_switch_revision = 4
    manager._magnetic_profile_switch_timer = None
    manager.usb_lock = threading.Lock()
    manager.magnetic_selected_slot = 8
    manager.magnetic_status = SimpleNamespace(value="", color=None)
    manager.page = SimpleNamespace(update=lambda: None)
    manager._ui_call = lambda callback: callback()
    manager.save_config = lambda **_kwargs: None
    manager._load_magnetic_controls = lambda slot: None
    manager._refresh_sk75_keyboard_picker = lambda: None
    sent = []
    manager._send_lighting_packets_locked = lambda packets, label, inter_packet_delay=0.0: sent.extend(packets)
    physical_reads = []
    before = KeyMagneticSettings(**live_settings)
    after = KeyMagneticSettings(**settings)

    def read_physical(_label, *, include_keyboard_options=False):
        physical_reads.append(include_keyboard_options)
        # The first read is the actual keyboard state; the post-write read is
        # the firmware acknowledgement which a profile selection now requires.
        return (
            ({8: before}, {8: 0x80}, None)
            if len(physical_reads) == 1
            else ({8: after}, {8: 0x80}, None)
        )

    manager._read_magnetic_matrix_locked = read_physical
    commit_delays = []
    monkeypatch.setattr(app_module.time, "sleep", lambda seconds: commit_delays.append(seconds))

    class ImmediateThread:
        def __init__(self, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(app_module.threading, "Thread", ImmediateThread)
    manager._apply_selected_magnetic_profile_automatically(1, 4)

    # Normal deactivation is Womier's independent ``liftTravel`` operation
    # and is committed alongside actuation, RT and dead-zone values.
    assert len(sent) == 7
    assert sent[2][1] == MagneticProtocol.OP_DEACTIVATION
    assert physical_reads == [False, False]
    assert commit_delays == [app_module.WOMIER_MAGNETIC_SIMPLE_COMMIT_DELAY_SEC]
    assert entry["magnetic_key_settings"]["8"]["actuation"] == 2.0
    assert "подтверждён клавиатурой" in manager.magnetic_status.value


def test_magnetic_wheel_keeps_native_page_scrolling():
    """The parent scroll position follows wheel input over a magnetic scale."""
    manager = QMKManager.__new__(QMKManager)
    manager._main_scroll_position = 742.0

    asyncio.run(manager._on_main_scroll(type("Scroll", (), {"pixels": 794.0})()))

    assert manager._main_scroll_position == 794.0
