"""Regression coverage for complete, portable configuration transfer."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

import app_flet as app_module
from app_flet import (
    CONFIG_TRANSFER_FORMAT,
    CONFIG_TRANSFER_VERSION,
    CONFIG_TRANSFER_SECTIONS,
    CONFIG_TRANSFER_WOMIER_GUARD_KEY,
    LEGACY_CONFIG_TRANSFER_FORMAT,
    LEGACY_CONFIG_TRANSFER_VERSION,
    MAX_CONFIG_IMPORT_CHARS,
    QMKManager,
    TRANSFER_SECTION_LIGHTING_LAB,
    TRANSFER_SECTION_MAGNETIC_LAB,
    TRANSFER_SECTION_PROCESS_BINDINGS,
    TRANSFER_SECTION_PROFILE_NAMES,
    _normalise_imported_configuration,
    _parse_config_transfer_text,
    _parse_profile_rules_transfer_text,
    _parse_imported_configuration_text,
    _write_json_atomically,
)
from lighting import LightingSettings
from magnetic import MagneticProtocol, SK75_KEY_BY_SLOT


def _magnetic_values(slot: int, profile: int) -> dict:
    """Return valid, distinct values so a lost profile is obvious."""
    return {
        "actuation": 0.40 + profile * 0.25 + (slot % 7) * 0.01,
        "rapid_trigger": True,
        "rapid_press": 0.10 + profile * 0.01,
        "rapid_release": 0.20 + profile * 0.01,
        "lower_dead_zone": 0.05,
        "upper_dead_zone": 0.10,
        # The ordinary liftTravel/deactivation threshold is independent from
        # Rapid Trigger and must survive every transfer route too.
        "deactivation": 0.70 + profile * 0.20 + (slot % 5) * 0.01,
    }


def _complete_config(*, binding_count: int = 8) -> dict:
    """A realistic SK75 payload containing every persisted current section."""
    magnetic_profiles = {}
    for profile in range(4):
        key_settings = {
            str(slot): _magnetic_values(slot, profile) for slot in range(90)
        }
        first_snap_slot = (8, 9, 12, 13)[profile]
        second_snap_slot = (20, 21, 22, 23)[profile]
        key_modes = {str(slot): 0x80 for slot in range(90)}
        key_modes[str(first_snap_slot)] = MagneticProtocol.MODE_SNAP
        key_modes[str(second_snap_slot)] = MagneticProtocol.MODE_SNAP
        magnetic_profiles[str(profile)] = {
            "key_settings": key_settings,
            "key_modes": key_modes,
            "keyboard_options": {
                "rt_stab": (profile + 1) * 25,
                "anti_accidental": profile % 2 == 0,
                "fn_index": profile,
                "wasd_swap": False,
                "system": "win",
            },
            "rt_separate": {str(slot): slot % 3 == 0 for slot in range(90)},
            "snap_pairs": [[first_snap_slot, second_snap_slot]],
            "initialized": True,
        }

    active_profile = magnetic_profiles["2"]
    device = {
        "vid": 0x3151,
        "pid": 0x5030,
        "usage_page": 0xFFFF,
        "label": "WOMIER SK75 TMR",
        "transport": "wired",
        "keyboard_type": "magnetic",
        "cooldown_ms": 100,
        "profile_switch_delay_ms": 150,
        "payloads": {
            "typing": {"data": [4, 0], "polling_rate": 1000, "lighting_profile": 0},
            "gaming": {"data": [4, 1], "polling_rate": 8000, "lighting_profile": 1},
            "work": {"data": [4, 2], "polling_rate": 2000, "lighting_profile": 2},
            "media": {"data": [4, 3], "polling_rate": 500, "lighting_profile": 3},
        },
        "bindings": [
            {
                "process": f"game-{index:04}.exe",
                "profile_index": index % 4,
                "enabled": index % 5 != 0,
            }
            for index in range(binding_count)
        ],
        "default_profile_index": 1,
        "lighting_lab": LightingSettings(
            effect=4,
            color=(0x12, 0xAB, 0xE4),
            brightness=3,
            speed=1,
            option=2,
            rainbow=False,
        ).to_config(),
        "magnetic_selected_profile": 2,
        "magnetic_key_settings": deepcopy(active_profile["key_settings"]),
        "magnetic_key_modes": deepcopy(active_profile["key_modes"]),
        "magnetic_keyboard_options": deepcopy(active_profile["keyboard_options"]),
        "magnetic_rt_separate": deepcopy(active_profile["rt_separate"]),
        "magnetic_snap_pairs": deepcopy(active_profile["snap_pairs"]),
        "magnetic_profiles": magnetic_profiles,
        # A deferred Womier cache mirror is deliberately persisted so it is
        # not lost when a full config moves to another installation.
        "womier_cache_sync_pending": {
            "2": {
                "key_settings": {"8": _magnetic_values(8, 2)},
                "key_modes": {"8": 0x80},
                "rt_stab": 75,
            }
        },
        "magnetic_profiles_before_womier_import": deepcopy(magnetic_profiles),
        "battery": {
            "query": [0xF7] + [0] * 63,
            "report_id": 0,
            "response_length": 65,
            "response_offset": 2,
            "response_scale": 1,
            "charging_offset": None,
            "charging_mask": 0,
        },
        # Unknown future data must remain portable as long as it is JSON.
        "future_driver_cache": {"revision": 7, "values": ["one", "two"]},
    }
    return {
        "mode": "auto",
        "settings": {
            "start_minimized": False,
            "autostart_service": True,
            "autostart": False,
            "startup_delay_sec": 5,
            "browser_path": "",
            "debug": False,
            "womier_magnetic_import": {"source": "portable-test", "profiles_imported": 4},
            "future_setting": {"kept": True},
        },
        "devices": {"3151:5030:ffff": device},
        "active_device": "3151:5030:ffff",
        # These aliases are intentionally runtime-only and must never be
        # exported as a second stale copy of the active device.
        "payloads": device["payloads"],
        "bindings": device["bindings"],
        "battery": device["battery"],
        "device": {"vid": device["vid"], "pid": device["pid"], "usage_page": device["usage_page"]},
    }


class _NativeClipboard:
    CF_UNICODETEXT = 13

    def __init__(self, *, truncate_reads: bool = False):
        self.value = ""
        self.truncate_reads = truncate_reads
        self.opened = False

    def OpenClipboard(self):
        assert not self.opened
        self.opened = True

    def EmptyClipboard(self):
        assert self.opened
        self.value = ""

    def SetClipboardText(self, text, _format):
        assert self.opened
        self.value = text

    def GetClipboardData(self, _format):
        assert self.opened
        return "{" if self.truncate_reads else self.value

    def IsClipboardFormatAvailable(self, _format):
        return True

    def CloseClipboard(self):
        self.opened = False


class _AsyncClipboard:
    def __init__(self):
        self.value = ""

    async def set(self, text):
        self.value = text

    async def get(self):
        return self.value


class _TaskPage:
    def run_task(self, task):
        asyncio.run(task())


def _manager_with_config(*, binding_count: int = 8) -> QMKManager:
    manager = QMKManager.__new__(QMKManager)
    manager.config = _complete_config(binding_count=binding_count)
    return manager


def test_large_native_clipboard_export_is_complete_and_parseable(monkeypatch):
    """Native copy must not silently degrade a large document to ``{"`."""
    manager = _manager_with_config(binding_count=2_000)
    text = json.dumps(manager._exportable_config(), indent=2, ensure_ascii=False)
    native = _NativeClipboard()
    monkeypatch.setattr(app_module, "win32clipboard", native)

    assert len(text) > 100_000
    assert manager._set_system_clipboard_text(text) is True
    assert native.value == text
    assert json.loads(native.value)["config"]["devices"]["3151:5030:ffff"]["womier_cache_sync_pending"]["2"]["rt_stab"] == 75


def test_compact_rules_export_falls_back_to_flet_clipboard_without_truncation(monkeypatch):
    """Rules-only export stays compact even with a huge magnetic local config."""
    manager = _manager_with_config(binding_count=2_000)
    native = _NativeClipboard(truncate_reads=True)
    clipboard = _AsyncClipboard()
    notices = []
    manager.clipboard = clipboard
    manager.page = _TaskPage()
    manager._snack = notices.append
    monkeypatch.setattr(app_module, "win32clipboard", native)

    manager._copy_configuration()

    parsed = json.loads(clipboard.value)
    legacy_size = len(json.dumps(manager._exportable_config(), ensure_ascii=False))
    # 2,000 actual process rules still take space, but all duplicated keyboard
    # state is gone.  The public transfer must be materially smaller than the
    # old full configuration, not pretend that real user bindings do not exist.
    assert len(clipboard.value) < legacy_size / 2
    assert parsed["format"] == CONFIG_TRANSFER_FORMAT
    assert parsed["version"] == CONFIG_TRANSFER_VERSION
    assert parsed["devices"][0]["bindings"][-1]["process"] == "game-1999.exe"
    assert "magnetic_profiles" not in clipboard.value
    assert "lighting_lab" not in clipboard.value
    assert notices and notices[-1].startswith("Скопировано:")


def test_cfg_dialog_keeps_large_clipboard_json_out_of_flet_and_requires_a_choice():
    """CFG uses local category cards; it never mounts clipboard JSON in Flet."""
    source = inspect.getsource(QMKManager._open_config_import_dialog)

    assert "ft.TextField(" not in source
    assert "pending_import" in source
    assert "selected_sections" in source
    assert '"profile_names": False' in source
    assert '"lighting_lab": False' in source
    assert '"magnetic_lab": False' in source
    assert '"process_bindings": False' in source
    assert "Скопировать CFG" in source
    assert "Вставить CFG" in source
    assert "Применить CFG" in source
    # All actions are initially inactive until the user checks a category.
    assert source.count("disabled=True") >= 3


def test_settings_expose_one_cfg_entry_point_instead_of_copy_and_paste_icons():
    """The settings header opens the selected-section dialog through one CFG button."""
    source = inspect.getsource(QMKManager._build_ui)
    settings_area = source.split("configuration_transfer =", 1)[1].split(
        "def settings_item", 1
    )[0]

    assert "ft.FilledTonalButton(" in settings_area
    assert '"CFG"' in settings_area
    assert "SETTINGS_BACKUP_RESTORE_ROUNDED" in settings_area
    assert "self._open_config_import_dialog()" in settings_area
    assert "ft.IconButton(" not in settings_area


def test_cfg_dialog_starts_with_categories_and_actions_disabled():
    """No transfer can happen until the current dialog explicitly selects data."""
    shown = []
    manager = QMKManager.__new__(QMKManager)
    manager.page = SimpleNamespace(
        show_dialog=shown.append,
        pop_dialog=lambda: None,
    )
    manager._snack = lambda _message: None

    manager._open_config_import_dialog()

    assert len(shown) == 1
    dialog = shown[0]

    def walk(control):
        yield control
        for attr in ("content", "title", "actions", "controls"):
            child = getattr(control, attr, None)
            if isinstance(child, (list, tuple)):
                for item in child:
                    yield from walk(item)
            elif child is not None:
                yield from walk(child)

    controls = list(walk(dialog))
    actions = [
        control
        for control in controls
        if getattr(control, "content", None)
        in {"Скопировать CFG", "Вставить CFG", "Применить CFG"}
    ]
    checks = [
        control for control in controls if control.__class__.__name__ == "Checkbox"
    ]

    assert len(actions) == 3
    assert all(action.disabled for action in actions)
    assert len(checks) == 4
    assert all(not check.value for check in checks)

    # A checked category enables copy/paste in this dialog only; Apply still
    # waits for an actual clipboard receipt.
    checks[0].value = True
    checks[0].on_change(SimpleNamespace(control=checks[0]))
    refreshed_actions = [
        control
        for control in controls
        if getattr(control, "content", None)
        in {"Скопировать CFG", "Вставить CFG", "Применить CFG"}
    ]
    assert not refreshed_actions[0].disabled
    assert not refreshed_actions[1].disabled
    assert refreshed_actions[2].disabled


def test_complete_round_trip_preserves_womier_cache_and_skips_external_imports(tmp_path, monkeypatch):
    """Explicit paste must retain every current section rather than reimport Womier."""
    source = _manager_with_config(binding_count=64)
    # Make the exact source canonical first, just as it is after a normal app
    # launch.  The export can then be compared byte-for-byte structurally.
    source._normalize_device_entry(source.config["devices"]["3151:5030:ffff"])
    document = source._exportable_config()
    restored = _normalise_imported_configuration(json.loads(json.dumps(document)))
    config_path = tmp_path / "profiles_config.json"
    _write_json_atomically(config_path, restored)
    monkeypatch.setattr(app_module, "CONFIG_FILE", config_path)
    monkeypatch.setattr(
        app_module,
        "find_womier_magnetic_import",
        lambda: pytest.fail("explicit config import must not query external Womier cache"),
    )

    loaded = QMKManager.__new__(QMKManager)
    loaded.config = loaded.load_config(include_external_migrations=False)

    assert loaded._exportable_config() == document
    entry = loaded.config["devices"]["3151:5030:ffff"]
    assert entry["magnetic_profiles"]["3"]["key_settings"]["80"]["actuation"] == 1.18
    assert entry["magnetic_profiles"]["3"]["key_settings"]["80"]["deactivation"] == pytest.approx(1.30)
    assert entry["womier_cache_sync_pending"]["2"]["rt_stab"] == 75
    assert entry["womier_cache_sync_pending"]["2"]["key_settings"]["8"]["deactivation"] == pytest.approx(1.13)
    assert entry["future_driver_cache"] == {"revision": 7, "values": ["one", "two"]}


def test_fresh_public_install_never_imports_another_manager_configuration(tmp_path, monkeypatch):
    """A clean release must not inherit the publisher's old profiles/rules."""
    legacy = _complete_config(binding_count=3)
    legacy_path = tmp_path / "old-manager" / "profiles_config.json"
    _write_json_atomically(legacy_path, legacy)
    public_config = tmp_path / "public" / "profiles_config.json"
    monkeypatch.setattr(app_module, "CONFIG_FILE", public_config)
    monkeypatch.setattr(app_module, "LEGACY_CONFIG_FILE", legacy_path)
    monkeypatch.setattr(app_module, "find_womier_magnetic_import", lambda: None)

    manager = QMKManager.__new__(QMKManager)
    fresh = manager.load_config()

    assert fresh["devices"] == {}
    assert fresh["active_device"] is None
    assert "legacy_config_migration" not in fresh["settings"]
    assert json.loads(public_config.read_text(encoding="utf-8"))["devices"] == {}


def test_native_clipboard_import_remains_authoritative_after_a_restart(tmp_path, monkeypatch):
    """A complete copy must not be replaced by target-PC Womier data later.

    This covers the actual native clipboard helper as well as parse, atomic
    import and a later normal ``load_config()``.  The latter is important:
    skipping external migrations only during the first paste was not enough,
    because the following app start could otherwise silently import a
    different official-driver cache.
    """
    source = _manager_with_config(binding_count=64)
    source._normalize_device_entry(source.config["devices"]["3151:5030:ffff"])
    document = source._exportable_config()
    text = json.dumps(document, ensure_ascii=False)
    native = _NativeClipboard()
    monkeypatch.setattr(app_module, "win32clipboard", native)

    assert source._set_system_clipboard_text(text) is True
    candidate = _parse_imported_configuration_text(source._get_system_clipboard_text())
    assert candidate == document["config"]
    assert candidate["settings"][CONFIG_TRANSFER_WOMIER_GUARD_KEY] is True

    config_path = tmp_path / "profiles_config.json"
    _write_json_atomically(config_path, candidate)
    monkeypatch.setattr(app_module, "CONFIG_FILE", config_path)
    # This test isolates the Womier cache boundary.  A real legacy manager
    # installation on the developer machine must not add unrelated devices to
    # the transfer fixture during the simulated restart.
    monkeypatch.setattr(app_module, "LEGACY_CONFIG_FILE", tmp_path / "no-legacy-config.json")
    monkeypatch.setattr(
        app_module,
        "womier_storage_fingerprint",
        lambda: pytest.fail("a portable import must not inspect the Womier cache"),
    )
    monkeypatch.setattr(
        app_module,
        "find_womier_magnetic_import",
        lambda: pytest.fail("a portable import must not query the Womier cache"),
    )

    loaded = QMKManager.__new__(QMKManager)
    loaded.config = loaded.load_config(include_external_migrations=False)
    assert loaded._exportable_config() == document

    # Simulate the next ordinary launch, when automatic migrations would
    # normally be enabled.  The transfer guard must preserve all four presets
    # and the deferred Womier delta without even reading the foreign cache.
    restarted = QMKManager.__new__(QMKManager)
    restarted.config = restarted.load_config()
    assert restarted._exportable_config() == document
    entry = restarted.config["devices"]["3151:5030:ffff"]
    assert entry["magnetic_profiles"]["0"]["key_settings"]["8"]["deactivation"] == pytest.approx(0.73)
    assert entry["magnetic_profiles"]["3"]["key_settings"]["80"]["deactivation"] == pytest.approx(1.30)
    assert entry["womier_cache_sync_pending"]["2"]["key_settings"]["8"]["deactivation"] == pytest.approx(1.13)


def test_import_text_rejects_oversized_ambiguous_and_nonstandard_json():
    """Malformed clipboard payloads fail before config replacement begins."""
    with pytest.raises(ValueError, match="слишком большая"):
        _parse_imported_configuration_text(" " * (MAX_CONFIG_IMPORT_CHARS + 1))

    duplicate_devices = json.dumps(
        {
            "format": CONFIG_TRANSFER_FORMAT,
            "version": CONFIG_TRANSFER_VERSION,
            "config": {"devices": {}},
        },
        ensure_ascii=False,
    ).replace('"devices": {}', '"devices": {}, "devices": {}')
    with pytest.raises(ValueError, match="повторяющийся ключ"):
        _parse_imported_configuration_text(duplicate_devices)

    nonstandard_number = (
        '{"format": "qmk-top-manager-config", "version": 1, '
        '"config": {"devices": {}, "settings": {"bad": NaN}}}'
    )
    with pytest.raises(ValueError, match="недопустимое JSON-значение"):
        _parse_imported_configuration_text(nonstandard_number)


def test_import_validation_rejects_bad_wrapper_and_device_without_mutating_source():
    """Malformed clipboard input never partially normalizes the parsed object."""
    valid = _manager_with_config()._exportable_config()
    before = deepcopy(valid)

    with pytest.raises(ValueError, match="неподдерживаемая версия"):
        _normalise_imported_configuration({
            "format": LEGACY_CONFIG_TRANSFER_FORMAT,
            "version": LEGACY_CONFIG_TRANSFER_VERSION + 1,
            "config": valid["config"],
        })
    # ``True == 1`` in Python, but a JSON boolean must never be accepted as a
    # transfer-format version.
    with pytest.raises(ValueError, match="неподдерживаемая версия"):
        _normalise_imported_configuration({
            "format": LEGACY_CONFIG_TRANSFER_FORMAT,
            "version": True,
            "config": valid["config"],
        })
    assert valid == before

    malformed = deepcopy(valid)
    malformed["config"]["devices"]["3151:5030:ffff"]["battery"] = []
    before_malformed = deepcopy(malformed)
    with pytest.raises(ValueError, match="battery"):
        _normalise_imported_configuration(malformed)
    assert malformed == before_malformed


@pytest.mark.parametrize(
    "bad_payload",
    [
        [4, "not-a-byte"],
        [4, True],
        [4, -1],
        [4, 256],
        [4] * 65,
        "04 01",
    ],
)
def test_import_rejects_unsafe_profile_bytes_before_any_config_write(bad_payload):
    """A pasted payload cannot become a persistent later HID/UI crash."""
    document = _manager_with_config()._exportable_config()
    document["config"]["devices"]["3151:5030:ffff"]["payloads"]["typing"][
        "data"
    ] = bad_payload

    with pytest.raises(ValueError, match="data профиля"):
        _parse_imported_configuration_text(json.dumps(document, ensure_ascii=False))


def test_selected_cfg_rejects_hidden_malformed_profile_bytes_before_apply(
    tmp_path, monkeypatch
):
    """A manual CFG cannot smuggle invalid HID bytes through ignored fields."""
    manager = _prepare_selected_transfer_target(tmp_path, monkeypatch)
    document = manager._exportable_config_transfer({TRANSFER_SECTION_PROFILE_NAMES})
    document["devices"][0]["payloads"] = {
        "typing": {"data": [4, "not-a-byte"]}
    }
    before = deepcopy(manager.config)

    # The normal clipboard parser rejects it before the merger is entered.
    with pytest.raises(ValueError, match="data профиля"):
        _parse_config_transfer_text(json.dumps(document, ensure_ascii=False))

    # Keep the same guarantee for a direct/compatibility caller which skips
    # the dialog parser: it must not cancel state or persist a partial import.
    with pytest.raises(ValueError, match="data профиля"):
        manager._apply_config_transfer(
            document, sections={TRANSFER_SECTION_PROFILE_NAMES}
        )
    assert manager.config == before


def test_load_config_preserves_broken_json_instead_of_overwriting_it(tmp_path, monkeypatch):
    """A corrupt/partially-synced file remains recoverable after startup."""
    config_path = tmp_path / "profiles_config.json"
    broken = '{"devices": [this is not JSON'
    config_path.write_text(broken, encoding="utf-8")
    monkeypatch.setattr(app_module, "CONFIG_FILE", config_path)

    manager = QMKManager.__new__(QMKManager)
    loaded = manager.load_config(include_external_migrations=False)

    assert loaded["devices"] == {}
    assert config_path.read_text(encoding="utf-8") == broken
    backup = manager._config_recovery_backup
    assert backup is not None
    assert backup.read_text(encoding="utf-8") == broken
    assert manager._config_recovery_write_blocked is False

    # The first explicit save is now safe: the exact broken source was kept.
    manager.config = loaded
    manager.is_running = False
    assert manager.save_config(reload_runtime=False) is True
    assert json.loads(config_path.read_text(encoding="utf-8"))["mode"] == "auto"
    assert backup.read_text(encoding="utf-8") == broken


def test_load_config_refuses_to_overwrite_when_recovery_copy_fails(tmp_path, monkeypatch):
    """A sharing error cannot turn an unreadable source into empty defaults."""
    config_path = tmp_path / "profiles_config.json"
    broken = "not-json"
    config_path.write_text(broken, encoding="utf-8")
    monkeypatch.setattr(app_module, "CONFIG_FILE", config_path)
    monkeypatch.setattr(app_module, "_preserve_unreadable_config_file", lambda _path: None)

    manager = QMKManager.__new__(QMKManager)
    manager.config = manager.load_config(include_external_migrations=False)
    manager.is_running = False

    assert manager._config_recovery_write_blocked is True
    assert manager.save_config(reload_runtime=False) is False
    assert config_path.read_text(encoding="utf-8") == broken


def test_existing_bad_profile_payload_is_sanitized_before_ui_or_hid_use(tmp_path, monkeypatch):
    """Hand-edited legacy config cannot crash profile preview on launch."""
    config = _complete_config()
    config["devices"]["3151:5030:ffff"]["payloads"]["typing"]["data"] = [4, "bad"]
    config_path = tmp_path / "profiles_config.json"
    _write_json_atomically(config_path, config)
    monkeypatch.setattr(app_module, "CONFIG_FILE", config_path)

    manager = QMKManager.__new__(QMKManager)
    manager.config = manager.load_config(include_external_migrations=False)

    assert "data" not in manager.config["devices"]["3151:5030:ffff"]["payloads"]["typing"]
    fallback = manager._profile_payload_at(0)
    assert len(fallback) == 64
    assert fallback[:2] == [4, 0]


def test_config_import_fence_cancels_old_magnetic_timers_before_replacement():
    """An old slider/cache write cannot land in a newly pasted document."""
    class Timer:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return True

    manager = QMKManager.__new__(QMKManager)
    manager.config = _complete_config()
    manager.is_running = False
    manager.app_alive = True
    manager.usb_lock = threading.Lock()
    manager._magnetic_write_lock = threading.Lock()
    key_timer = Timer()
    option_timer = Timer()
    manager._magnetic_write_timers = {8: key_timer}
    manager._magnetic_write_revisions = {8: 3}
    manager._magnetic_pending_key_writes = {8: (object(), [], 3, 0)}
    manager._magnetic_inflight_key_writes = {}
    manager._magnetic_options_timer = option_timer
    manager._magnetic_options_revision = 2
    manager._magnetic_pending_options_write = (25, False, 2, 0)
    manager._magnetic_options_inflight = None
    profile_timer = Timer()
    manager._magnetic_profile_switch_lock = threading.Lock()
    manager._magnetic_profile_switch_revision = 4
    manager._magnetic_profile_switch_timer = profile_timer
    cache_timer = Timer()
    manager._womier_cache_sync_lock = threading.Lock()
    manager._womier_cache_sync_revision = 7
    manager._womier_cache_sync_timer = cache_timer
    manager._womier_cache_sync_pending = {("3151:5030:ffff", 0): {"key_settings": {}}}
    persist_timer = Timer()
    manager._magnetic_persistence_lock = threading.RLock()
    manager._magnetic_persistence_timer = persist_timer
    manager._magnetic_persistence_revision = 5
    manager._magnetic_persistence_pending = True

    manager._prepare_configuration_replacement()

    assert key_timer.cancelled and option_timer.cancelled and profile_timer.cancelled
    assert cache_timer.cancelled and persist_timer.cancelled
    assert manager._magnetic_pending_key_writes == {}
    assert manager._magnetic_pending_options_write is None
    assert manager._magnetic_write_revisions[8] == 4
    assert manager._magnetic_profile_switch_revision == 5
    assert manager._womier_cache_sync_pending == {}
    assert manager._womier_cache_sync_revision == 8
    assert manager._magnetic_persistence_pending is False


def test_public_rules_transfer_contains_only_names_and_process_rules():
    """Clipboard transfer must not leak or apply keyboard/RGB/magnetic state."""
    manager = _manager_with_config(binding_count=3)

    document = manager._exportable_profile_rules()
    encoded = json.dumps(document, ensure_ascii=False)
    parsed = _parse_profile_rules_transfer_text(encoded)
    rule = parsed["devices"][0]

    assert document["format"] == CONFIG_TRANSFER_FORMAT
    assert document["version"] == CONFIG_TRANSFER_VERSION
    assert rule["profile_names"] == ["typing", "gaming", "work", "media"]
    assert rule["bindings"][1]["process"] == "game-0001.exe"
    assert "lighting_lab" not in encoded
    assert "magnetic_profiles" not in encoded
    assert "womier_cache_sync_pending" not in encoded
    assert '"data"' not in encoded


def test_legacy_full_clipboard_is_reduced_to_rules_only():
    """A previously copied 250k legacy document remains importable safely."""
    manager = _manager_with_config(binding_count=2)

    rules = _parse_profile_rules_transfer_text(
        json.dumps(manager._exportable_config(), ensure_ascii=False)
    )

    assert rules["devices"][0]["profile_names"] == ["typing", "gaming", "work", "media"]
    assert rules["devices"][0]["bindings"][0]["process"] == "game-0000.exe"


def test_rules_import_renames_profiles_and_replaces_bindings_without_hardware_state(
    tmp_path, monkeypatch
):
    """Import must not overwrite RGB, HID payloads or magnetic configuration."""
    manager = _manager_with_config(binding_count=2)
    manager.is_running = False
    manager._ensure_active_device_aliases()
    monkeypatch.setattr(app_module, "CONFIG_FILE", tmp_path / "profiles_config.json")
    entry = manager.config["devices"]["3151:5030:ffff"]
    before_lighting = deepcopy(entry["lighting_lab"])
    before_magnetic = deepcopy(entry["magnetic_profiles"])
    before_payload_data = [profile["data"][:] for profile in entry["payloads"].values()]

    applied, skipped = manager._apply_profile_rules_transfer(
        {
            "devices": [
                {
                    "device_key": "3151:5030:ffff",
                    "identity": {"vid": 0x3151, "pid": 0x5030, "usage_page": 0xFFFF},
                    "profile_names": ["one", "two", "three", "four"],
                    "bindings": [
                        {"process": "new-game.exe", "profile_index": 2, "enabled": False}
                    ],
                }
            ]
        }
    )

    assert (applied, skipped) == (1, 0)
    entry = manager.config["devices"]["3151:5030:ffff"]
    assert list(entry["payloads"]) == ["one", "two", "three", "four"]
    assert [profile["data"] for profile in entry["payloads"].values()] == before_payload_data
    assert entry["bindings"] == [
        {"process": "new-game.exe", "profile_index": 2, "enabled": False}
    ]
    assert entry["lighting_lab"] == before_lighting
    assert entry["magnetic_profiles"] == before_magnetic


def _prepare_selected_transfer_target(tmp_path, monkeypatch):
    manager = _manager_with_config(binding_count=1)
    manager.is_running = False
    manager._ensure_active_device_aliases()
    monkeypatch.setattr(app_module, "CONFIG_FILE", tmp_path / "profiles_config.json")
    return manager


def test_magnetic_cfg_import_waits_for_an_inflight_old_slider_packet(
    tmp_path, monkeypatch
):
    """An import must not commit before an already-claimed old HID write exits.

    Cancelling a Timer is best-effort: it may be midway through a feature
    report when a user clicks CFG import.  The USB barrier makes that packet
    finish (and observe its invalidated revision) before the new local preset
    is committed, so no stale write can land after the import boundary.
    """
    manager = _prepare_selected_transfer_target(tmp_path, monkeypatch)
    manager.app_alive = True
    manager.usb_lock = threading.Lock()
    manager._magnetic_write_lock = threading.RLock()
    manager._magnetic_write_timers = {}
    manager._magnetic_write_revisions = {8: 1}
    manager._magnetic_pending_key_writes = {8: (object(), [[0x65]], 1, 0)}
    manager._magnetic_inflight_key_writes = {}
    manager._magnetic_options_timer = None
    manager._magnetic_options_revision = 0
    manager._magnetic_pending_options_write = None
    manager._magnetic_options_inflight = None

    entered_hid = threading.Event()
    allow_old_write_to_finish = threading.Event()
    import_done = threading.Event()
    events = []
    outcome = []
    failures = []

    def send_old_packet(_packets, _label, inter_packet_delay=0.0):
        del inter_packet_delay
        entered_hid.set()
        assert allow_old_write_to_finish.wait(2.0)
        events.append("old-hid")
        return True

    def should_not_cache(*_args, **_kwargs):
        pytest.fail("cancelled old slider write cached into imported configuration")

    original_save = manager.save_config

    def record_import_save(*args, **kwargs):
        events.append("import-save")
        return original_save(*args, **kwargs)

    manager._send_lighting_packets_locked = send_old_packet
    manager._cache_magnetic_settings = should_not_cache
    manager.save_config = record_import_save

    old_worker = threading.Thread(
        target=manager._write_magnetic_key_automatically,
        args=(8, object(), [[0x65]], 1, 0),
        daemon=True,
    )
    old_worker.start()
    assert entered_hid.wait(1.0)

    transfer = _parse_config_transfer_text(
        json.dumps(
            manager._exportable_config_transfer({TRANSFER_SECTION_MAGNETIC_LAB}),
            ensure_ascii=False,
        )
    )

    def import_cfg():
        try:
            outcome.append(
                manager._apply_config_transfer(
                    transfer, sections={TRANSFER_SECTION_MAGNETIC_LAB}
                )
            )
        except Exception as exc:  # pragma: no cover - assertion below reports it
            failures.append(exc)
        finally:
            import_done.set()

    import_worker = threading.Thread(target=import_cfg, daemon=True)
    import_worker.start()

    # The replacement invalidates the old revision, then waits for the HID
    # owner.  Without the barrier `import-save` would appear while the old
    # sender remains blocked above.
    deadline = time.monotonic() + 1.0
    while manager._magnetic_write_revisions.get(8) == 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert manager._magnetic_write_revisions[8] == 2
    assert not import_done.wait(0.08)

    allow_old_write_to_finish.set()
    old_worker.join(timeout=1.0)
    import_worker.join(timeout=1.0)

    assert not old_worker.is_alive()
    assert not import_worker.is_alive()
    assert failures == []
    assert outcome == [(1, 0)]
    assert events.index("old-hid") < events.index("import-save")


def test_selected_export_copies_default_profile_with_profile_names_only():
    source = _manager_with_config(binding_count=2)

    document = source._exportable_config_transfer({TRANSFER_SECTION_PROFILE_NAMES})
    parsed = _parse_config_transfer_text(json.dumps(document, ensure_ascii=False))
    rule = parsed["devices"][0]

    assert parsed["categories"] == [TRANSFER_SECTION_PROFILE_NAMES]
    assert rule["profile_names"] == ["typing", "gaming", "work", "media"]
    assert rule["default_profile_index"] == 1
    assert "bindings" not in rule
    assert "lighting_lab" not in rule
    assert "magnetic_lab" not in rule
    assert '"data"' not in json.dumps(document, ensure_ascii=False)


def test_selected_import_applies_profile_names_and_default_together(tmp_path, monkeypatch):
    source = _manager_with_config(binding_count=2)
    target = _prepare_selected_transfer_target(tmp_path, monkeypatch)
    target_entry = target.config["devices"]["3151:5030:ffff"]
    target_entry["default_profile_index"] = 3
    before_bindings = deepcopy(target_entry["bindings"])
    before_lighting = deepcopy(target_entry["lighting_lab"])
    before_magnetic = deepcopy(target_entry["magnetic_profiles"])
    document = _parse_config_transfer_text(
        json.dumps(
            source._exportable_config_transfer({TRANSFER_SECTION_PROFILE_NAMES}),
            ensure_ascii=False,
        )
    )

    assert target._apply_config_transfer(
        document, sections={TRANSFER_SECTION_PROFILE_NAMES}
    ) == (1, 0)
    target_entry = target.config["devices"]["3151:5030:ffff"]
    assert list(target_entry["payloads"]) == ["typing", "gaming", "work", "media"]
    assert target_entry["default_profile_index"] == 1
    assert target_entry["bindings"] == before_bindings
    assert target_entry["lighting_lab"] == before_lighting
    assert target_entry["magnetic_profiles"] == before_magnetic


@pytest.mark.parametrize(
    "section",
    [
        TRANSFER_SECTION_PROCESS_BINDINGS,
        TRANSFER_SECTION_LIGHTING_LAB,
        TRANSFER_SECTION_MAGNETIC_LAB,
    ],
)
def test_selected_import_keeps_every_unselected_section_unchanged(
    section, tmp_path, monkeypatch
):
    source = _manager_with_config(binding_count=3)
    target = _prepare_selected_transfer_target(tmp_path, monkeypatch)
    source_entry = source.config["devices"]["3151:5030:ffff"]
    target_entry = target.config["devices"]["3151:5030:ffff"]
    # Make every source/target section visibly distinct.
    target_entry["bindings"] = [{"process": "old.exe", "profile_index": 0, "enabled": True}]
    target_entry["lighting_lab"] = LightingSettings(
        effect=0, color=(1, 2, 3), brightness=0, speed=4, option=0, rainbow=False
    ).to_config()
    target_entry["magnetic_selected_profile"] = 0
    target_entry["magnetic_profiles"]["0"]["key_settings"]["4"]["actuation"] = 0.11
    before_names = list(target_entry["payloads"])
    before_default = target_entry["default_profile_index"]
    before_bindings = deepcopy(target_entry["bindings"])
    before_lighting = deepcopy(target_entry["lighting_lab"])
    before_magnetic = deepcopy(target_entry["magnetic_profiles"])
    all_sections = set(CONFIG_TRANSFER_SECTIONS)
    transfer = _parse_config_transfer_text(
        json.dumps(source._exportable_config_transfer(all_sections), ensure_ascii=False)
    )

    assert target._apply_config_transfer(transfer, sections={section}) == (1, 0)
    target_entry = target.config["devices"]["3151:5030:ffff"]
    assert list(target_entry["payloads"]) == before_names
    assert target_entry["default_profile_index"] == before_default
    if section == TRANSFER_SECTION_PROCESS_BINDINGS:
        assert target_entry["bindings"] == source_entry["bindings"]
        assert target_entry["lighting_lab"] == before_lighting
        assert target_entry["magnetic_profiles"] == before_magnetic
    elif section == TRANSFER_SECTION_LIGHTING_LAB:
        assert target_entry["bindings"] == before_bindings
        assert target_entry["lighting_lab"] == source_entry["lighting_lab"]
        assert target_entry["magnetic_profiles"] == before_magnetic
    else:
        assert target_entry["bindings"] == before_bindings
        assert target_entry["lighting_lab"] == before_lighting
        # The portable format intentionally strips unused firmware matrix
        # slots, so compare against the canonical document rather than the
        # intentionally broad 81-slot fixture.
        assert target_entry["magnetic_profiles"] == transfer["devices"][0]["magnetic_lab"]["profiles"]
        assert target_entry["magnetic_selected_profile"] == transfer["devices"][0]["magnetic_lab"]["selected_profile"]
        assert target.config["settings"][CONFIG_TRANSFER_WOMIER_GUARD_KEY] is True


def test_magnetic_lab_cfg_round_trip_keeps_rtstab_snap_pairs_and_every_profile(
    tmp_path, monkeypatch
):
    """Magnetic Lab means the complete safe editor state, not just sliders."""
    source = _manager_with_config(binding_count=2)
    document = source._exportable_config_transfer(
        {TRANSFER_SECTION_MAGNETIC_LAB}
    )
    encoded = json.dumps(document, ensure_ascii=False)
    parsed = _parse_config_transfer_text(encoded)
    magnetic = parsed["devices"][0]["magnetic_lab"]

    assert parsed["categories"] == [TRANSFER_SECTION_MAGNETIC_LAB]
    assert magnetic["selected_profile"] == 2
    assert set(magnetic["profiles"]) == {"0", "1", "2", "3"}
    for profile_index in range(4):
        profile = magnetic["profiles"][str(profile_index)]
        pair = [(8, 20), (9, 21), (12, 22), (13, 23)][profile_index]
        assert profile["keyboard_options"] == {
            "fn_index": profile_index,
            "anti_accidental": profile_index % 2 == 0,
            "rt_stab": (profile_index + 1) * 25,
            "wasd_swap": False,
            "system": "win",
        }
        assert profile["snap_pairs"] == [list(pair)]
        assert profile["key_modes"][str(pair[0])] == MagneticProtocol.MODE_SNAP
        assert profile["key_modes"][str(pair[1])] == MagneticProtocol.MODE_SNAP
        assert set(profile["key_settings"]) == {
            str(slot) for slot in SK75_KEY_BY_SLOT
        }
        assert set(profile["rt_separate"]) == {
            str(slot) for slot in SK75_KEY_BY_SLOT
        }

    # Portable Magnetic Lab state contains validated values only.  Raw HID
    # reports, process/profile payloads and machine-local recovery journals
    # never hitch a ride with the selected category.
    for forbidden in (
        '"payloads"',
        '"data"',
        "womier_cache_sync_pending",
        "magnetic_profiles_before_womier_import",
        "future_driver_cache",
        "raw_hid_payload",
    ):
        assert forbidden not in encoded

    target = _prepare_selected_transfer_target(tmp_path, monkeypatch)
    target_entry = target.config["devices"]["3151:5030:ffff"]
    target_entry["magnetic_snap_pairs"] = []
    target_entry["magnetic_profiles"]["2"]["snap_pairs"] = []
    target_entry["magnetic_profiles"]["2"]["keyboard_options"]["rt_stab"] = 0
    target._send_magnetic_packets = lambda *_args, **_kwargs: pytest.fail(
        "CFG import must not send HID packets"
    )
    target._send_lighting_packets_locked = lambda *_args, **_kwargs: pytest.fail(
        "CFG import must not send HID packets"
    )

    assert target._apply_config_transfer(
        parsed, sections={TRANSFER_SECTION_MAGNETIC_LAB}
    ) == (1, 0)
    target_entry = target.config["devices"]["3151:5030:ffff"]
    assert target_entry["magnetic_profiles"] == magnetic["profiles"]
    assert target_entry["magnetic_snap_pairs"] == [[12, 22]]
    assert target_entry["magnetic_keyboard_options"]["rt_stab"] == 75
    assert target_entry["magnetic_keyboard_options"]["anti_accidental"] is True
    assert target_entry["magnetic_key_modes"]["12"] == MagneticProtocol.MODE_SNAP
    assert target_entry["magnetic_key_modes"]["22"] == MagneticProtocol.MODE_SNAP


@pytest.mark.parametrize(
    "mutate",
    [
        lambda profile: profile.update(snap_pairs=[[8, 8]]),
        lambda profile: profile.update(snap_pairs=[[8, 20], [20, 21]]),
        lambda profile: profile.update(snap_pairs=[[8, 20]]),
    ],
)
def test_magnetic_cfg_rejects_invalid_or_mode_mismatched_snap_pairs(mutate):
    manager = _manager_with_config(binding_count=1)
    document = manager._exportable_config_transfer({TRANSFER_SECTION_MAGNETIC_LAB})
    profile = document["devices"][0]["magnetic_lab"]["profiles"]["0"]
    mutate(profile)
    if profile["snap_pairs"] == [[8, 20]]:
        profile["key_modes"]["8"] = MagneticProtocol.MODE_NORMAL

    with pytest.raises(ValueError, match="Snap Key"):
        _parse_config_transfer_text(json.dumps(document, ensure_ascii=False))


def test_magnetic_cfg_recovers_one_unambiguous_pair_from_legacy_snap_modes():
    manager = _manager_with_config(binding_count=1)
    entry = manager.config["devices"]["3151:5030:ffff"]
    for profile in entry["magnetic_profiles"].values():
        profile.pop("snap_pairs", None)
    entry.pop("magnetic_snap_pairs", None)

    document = manager._exportable_config_transfer({TRANSFER_SECTION_MAGNETIC_LAB})
    profiles = document["devices"][0]["magnetic_lab"]["profiles"]

    assert profiles["0"]["snap_pairs"] == [[8, 20]]
    assert profiles["1"]["snap_pairs"] == [[9, 21]]
    assert profiles["2"]["snap_pairs"] == [[12, 22]]
    assert profiles["3"]["snap_pairs"] == [[13, 23]]


def test_process_bindings_only_do_not_copy_profile_labels_or_default():
    source = _manager_with_config(binding_count=2)
    document = source._exportable_config_transfer({TRANSFER_SECTION_PROCESS_BINDINGS})
    rule = document["devices"][0]

    assert document["categories"] == [TRANSFER_SECTION_PROCESS_BINDINGS]
    assert rule["profile_count"] == 4
    assert "profile_names" not in rule
    assert "default_profile_index" not in rule


def test_selected_transfer_rejects_empty_selection_and_ignores_unknown_fields(tmp_path, monkeypatch):
    manager = _prepare_selected_transfer_target(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="хотя бы один"):
        manager._exportable_config_transfer(set())
    with pytest.raises(ValueError, match="хотя бы один"):
        manager._apply_config_transfer({"categories": [], "devices": []}, sections=set())
    with pytest.raises(ValueError, match="хотя бы один"):
        _parse_config_transfer_text(
            json.dumps(
                {
                    "format": CONFIG_TRANSFER_FORMAT,
                    "version": CONFIG_TRANSFER_VERSION,
                    "categories": [],
                    "devices": [],
                }
            )
        )

    document = manager._exportable_config_transfer({TRANSFER_SECTION_LIGHTING_LAB})
    document["devices"][0]["lighting_lab"]["raw_hid_payload"] = [7, 13, 255]
    parsed = _parse_config_transfer_text(json.dumps(document, ensure_ascii=False))
    assert "raw_hid_payload" not in parsed["devices"][0]["lighting_lab"]

    with pytest.raises(ValueError, match="нет выбранных разделов"):
        manager._apply_config_transfer(
            parsed,
            sections={TRANSFER_SECTION_LIGHTING_LAB, TRANSFER_SECTION_MAGNETIC_LAB},
        )


def test_v2_rules_document_remains_importable_without_hardware_sections():
    legacy_v2 = {
        "format": "qmk-top-manager-profile-rules",
        "version": 2,
        "devices": [
            {
                "device_key": "3151:5030:ffff",
                "identity": {"vid": 0x3151, "pid": 0x5030, "usage_page": 0xFFFF},
                "profile_names": ["one", "two", "three", "four"],
                "bindings": [{"process": "old-game.exe", "profile_index": 1, "enabled": True}],
            }
        ],
    }
    parsed = _parse_config_transfer_text(json.dumps(legacy_v2, ensure_ascii=False))

    assert parsed["categories"] == [
        TRANSFER_SECTION_PROFILE_NAMES,
        TRANSFER_SECTION_PROCESS_BINDINGS,
    ]
    assert parsed["devices"][0]["default_profile_index"] is None
    assert "lighting_lab" not in parsed["devices"][0]
    assert "magnetic_lab" not in parsed["devices"][0]


def test_selected_parser_rejects_unknown_versioned_format():
    with pytest.raises(ValueError, match="неизвестный формат"):
        _parse_config_transfer_text(
            json.dumps({"format": "someone-elses-config", "devices": {}})
        )
