"""Regression coverage for rapid Magnetic Lab UI events and background cache writes.

These tests deliberately use detached Flet-like controls.  The error reported
by users is raised while Flet reconciles a control tree or Python serialises a
mutable configuration, so exercising the same event handlers without a HID
device gives us a deterministic, fast guard for both paths.
"""

import os
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app_flet as app_module
from app_flet import MAGNETIC_PROFILE_COUNT, QMKManager
from magnetic import KeyMagneticSettings, KeyboardOptions, MagneticProtocol


def _magnetic_entry():
    return {
        "keyboard_type": "magnetic",
        "magnetic_key_settings": {},
        "magnetic_key_modes": {},
        "magnetic_keyboard_options": {},
        "magnetic_rt_separate": {},
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


class _Panel:
    """Stable parent stand-in used by the Flet 0.85 detached-control path."""

    def __init__(self):
        self.page = None
        self.updates = 0

    def update(self):
        self.updates += 1


def _rapid_toggle_manager():
    """Return the smallest complete selected-key panel for repeated events."""
    manager = QMKManager.__new__(QMKManager)
    entry = _magnetic_entry()
    manager.config = {"active_device": "sk75", "devices": {"sk75": entry}}
    manager.magnetic_profile_index = 0
    manager.magnetic_selected_slot = 8
    manager.magnetic_visual_selected_slot = 8
    manager.magnetic_rt_switch = SimpleNamespace(value=True, label=None)
    manager.magnetic_rt_separate_switch = SimpleNamespace(value=False, label=None)
    manager.magnetic_deactivation_separate_switch = SimpleNamespace(value=False, label=None)
    manager.magnetic_actuation_slider = SimpleNamespace(value=120)
    manager.magnetic_deactivation_slider = SimpleNamespace(value=120)
    manager.magnetic_rt_release_slider = SimpleNamespace(value=30)
    manager.magnetic_rt_press_slider = SimpleNamespace(value=30)
    manager.magnetic_lower_dead_zone_slider = SimpleNamespace(value=10)
    manager.magnetic_upper_dead_zone_slider = SimpleNamespace(value=0)

    def control():
        return SimpleNamespace(visible=True, opacity=1.0)

    manager.magnetic_rt_release_control = control()
    manager.magnetic_rt_press_control = control()
    manager.magnetic_lower_dead_zone_control = control()
    manager.magnetic_upper_dead_zone_control = control()
    manager.magnetic_deactivation_control = control()
    manager.magnetic_rt_separate_surface = control()
    manager.magnetic_deactivation_separate_surface = control()
    manager.magnetic_dead_zone_spacer = control()
    manager.magnetic_parameter_mode_title = SimpleNamespace(value=None)
    manager.magnetic_parameter_mode_description = SimpleNamespace(value=None)
    manager.magnetic_parameter_mode_badge_text = SimpleNamespace(value=None, color=None)
    manager.magnetic_parameter_mode_badge = SimpleNamespace(bgcolor=None)
    manager.magnetic_parameter_mode_surface = SimpleNamespace(bgcolor=None)
    manager.magnetic_parameter_panel = _Panel()
    manager._set_vertical_magnetic_value = (
        lambda slider, value, **_kwargs: setattr(slider, "value", value)
    )
    manager._magnetic_key_is_advanced = lambda _slot: False
    manager._patch_magnetic_picker_keycap = lambda *_args, **_kwargs: True
    manager._schedule_magnetic_key_write = lambda: None
    manager.save_config = lambda **_kwargs: None
    return manager


def test_rapid_toggle_and_separate_threshold_events_are_reentrant_on_one_panel():
    """Rapid ON/OFF and optional-threshold clicks must not rebuild child maps.

    In a real Flet 0.85 session each handler may arrive while the preceding
    parent patch is still queued.  Repeating the whole sequence here catches
    missing attributes/visibility mutations and ensures the persistent parent
    remains the sole update target in the detached fallback.
    """
    manager = _rapid_toggle_manager()

    for step in range(48):
        manager.magnetic_rt_switch.value = bool(step % 2)
        manager._on_magnetic_rt_changed()

        manager.magnetic_rt_separate_switch.value = bool((step // 2) % 2)
        manager._on_magnetic_rt_separation_changed()

        manager.magnetic_deactivation_separate_switch.value = bool((step // 3) % 2)
        manager._on_magnetic_deactivation_separation_changed()

        # A live slider edit in between mode changes must still derive a valid
        # key packet state rather than leaving the control pipeline inert.
        manager.magnetic_actuation_slider.value = 120 + (step % 20)
        manager._on_magnetic_control_changed()

    assert manager.magnetic_parameter_panel.updates >= 48
    assert manager.magnetic_rt_switch.label is None
    assert manager.magnetic_rt_separate_switch.label is None
    assert manager.magnetic_deactivation_separate_switch.label is None
    assert manager.magnetic_parameter_mode_badge_text.value in {"ВКЛ", "ВЫКЛ"}


def test_concurrent_rt_transition_requests_never_mutate_the_panel_at_once():
    """Queued/duplicate RT callbacks share one structural UI transaction."""

    manager = _rapid_toggle_manager()
    errors = []
    start = threading.Barrier(4)

    def flip(seed):
        try:
            start.wait(timeout=2)
            for step in range(24):
                manager.magnetic_rt_switch.value = bool((step + seed) % 2)
                manager._on_magnetic_rt_changed()
                manager.magnetic_rt_separate_switch.value = bool((step + seed) % 3)
                manager._on_magnetic_rt_separation_changed()
                manager.magnetic_deactivation_separate_switch.value = bool(
                    (step + seed) % 4
                )
                manager._on_magnetic_deactivation_separation_changed()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=flip, args=(seed,)) for seed in range(3)]
    for thread in threads:
        thread.start()
    start.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=3)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert manager._magnetic_parameter_mode_transition is False
    assert manager.magnetic_parameter_panel.updates >= 3


def test_late_magnetic_debounce_timers_do_not_touch_hid_after_quit():
    """Cancelled Timer callbacks are harmless after tray shutdown starts."""

    manager = QMKManager.__new__(QMKManager)
    manager.app_alive = False
    manager._magnetic_write_lock = threading.RLock()
    manager._magnetic_write_revisions = {8: 1}
    manager._magnetic_options_revision = 1
    manager.usb_lock = threading.RLock()
    manager._send_lighting_packets_locked = lambda *_args, **_kwargs: pytest.fail(
        "late debounce must not send HID after quit"
    )

    settings = KeyMagneticSettings(
        actuation=1.20,
        rapid_trigger=True,
        rapid_press=0.30,
        rapid_release=0.30,
        lower_dead_zone=0.10,
        upper_dead_zone=0.00,
        deactivation=0.85,
    )

    manager._write_magnetic_key_automatically(8, settings, [], 1, 0)
    manager._write_magnetic_options_automatically(25, True, 1, 0)


def test_cache_snapshot_boundary_tolerates_a_mapping_being_replaced_mid_copy():
    """Third-party/readback mappings cannot surface a dict-size exception."""

    class FlakyMapping:
        def __init__(self):
            self.calls = 0

        def items(self):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("dictionary changed size during iteration")
            return {"8": "value"}.items()

    mapping = FlakyMapping()

    assert QMKManager._mapping_items_snapshot(mapping) == [("8", "value")]
    assert mapping.calls == 3


def test_background_matrix_cache_write_waits_for_config_snapshot(monkeypatch):
    """A late full matrix read cannot resize config while save_config copies it."""

    mutation_started = threading.Event()
    worker_started = threading.Event()
    allow_worker = threading.Event()
    snapshot_started = threading.Event()
    allow_snapshot = threading.Event()
    worker_finished = threading.Event()

    class TrackingEntry(dict):
        def __setitem__(self, key, value):
            if key in {
                "magnetic_key_settings",
                "magnetic_key_modes",
                "magnetic_keyboard_options",
            }:
                mutation_started.set()
            return super().__setitem__(key, value)

    class TrackingUsbLock:
        """Prove the matrix reader holds one transport transaction."""

        def __init__(self):
            self.depth = 0
            self.enters = 0

        def __enter__(self):
            self.depth += 1
            self.enters += 1
            return self

        def __exit__(self, *_args):
            self.depth -= 1

    entry = TrackingEntry(_magnetic_entry())
    mutation_started.clear()
    manager = QMKManager.__new__(QMKManager)
    manager.is_running = False
    manager.usb_lock = TrackingUsbLock()
    manager.config = {"mode": "auto", "active_device": "sk75", "devices": {"sk75": entry}}
    manager.magnetic_profile_index = 0
    manager.magnetic_selected_slot = None
    manager._ui_call = lambda _callback: worker_finished.set()

    settings = KeyMagneticSettings(
        actuation=1.20,
        rapid_trigger=True,
        rapid_press=0.30,
        rapid_release=0.30,
        lower_dead_zone=0.10,
        upper_dead_zone=0.00,
        deactivation=0.85,
    )

    def query(*_args, **_kwargs):
        assert manager.usb_lock.depth == 1
        worker_started.set()
        assert allow_worker.wait(2)
        return b""

    original_copy = app_module._json_copy

    def held_copy(value):
        if isinstance(value, dict) and "devices" in value and not snapshot_started.is_set():
            snapshot_started.set()
            assert allow_snapshot.wait(2)
        return original_copy(value)

    # The full matrix now keeps the HID transport for one coherent snapshot,
    # so the worker uses the lock-aware low-level reader directly.
    manager._query_magnetic_packet_locked = query
    monkeypatch.setattr(app_module, "_json_copy", held_copy)
    monkeypatch.setattr(app_module, "_write_json_atomically", lambda *_args: None)
    monkeypatch.setattr(
        MagneticProtocol,
        "decode_multi_magnetism",
        staticmethod(lambda _reports: {8: settings}),
    )
    monkeypatch.setattr(
        MagneticProtocol,
        "decode_multi_magnetism_modes",
        staticmethod(lambda _reports: {8: MagneticProtocol.MODE_NORMAL | MagneticProtocol.MODE_RAPID_TRIGGER_BIT}),
    )
    monkeypatch.setattr(
        MagneticProtocol,
        "decode_keyboard_options",
        staticmethod(lambda _report: KeyboardOptions()),
    )

    manager._read_magnetic_matrix(silent=True, capture_to_profile_index=0)
    assert worker_started.wait(2)

    save_thread = threading.Thread(target=manager.save_config)
    save_thread.start()
    assert snapshot_started.wait(2)
    try:
        allow_worker.set()
        # Before the snapshot lock is released, the background worker must not
        # touch the nested live cache.  The old unlocked assignment made this
        # event fire and could crash json.dumps with a dictionary-size error.
        assert not mutation_started.wait(0.12)
    finally:
        allow_snapshot.set()

    save_thread.join(2)
    assert not save_thread.is_alive()
    assert worker_finished.wait(3)
    assert mutation_started.is_set()
    assert manager.usb_lock.enters == 1
    assert entry["magnetic_key_settings"]["8"]["actuation"] == pytest.approx(1.20)
    assert entry["magnetic_key_settings"]["8"]["deactivation"] == pytest.approx(0.85)


def test_magnetic_timer_and_rt_toggle_saves_do_not_reload_input_runtime():
    """Magnetic-only persistence must not unhook normal keyboard input.

    ``save_config()`` normally reloads the foreground-profile runtime, which
    calls ``keyboard.unhook_all()``.  Slider debounce workers and the local
    separate-RT switch don't change those foreground rules, so they must use
    the no-reload path.  This catches a regression without a HID device or a
    live Flet page.
    """
    manager = _rapid_toggle_manager()
    manager._magnetic_write_lock = threading.RLock()
    manager.usb_lock = threading.RLock()
    manager._magnetic_write_revisions = {8: 1}
    manager._magnetic_inflight_key_writes = {}
    manager._magnetic_pending_key_writes = {}
    manager._magnetic_write_timers = {}
    manager._magnetic_options_revision = 1
    manager._magnetic_options_inflight = None
    manager._magnetic_pending_options_write = None
    manager._magnetic_options_timer = None
    manager._send_lighting_packets_locked = lambda *_args, **_kwargs: None
    manager._live_magnetic_keyboard_options = lambda: KeyboardOptions()

    saves = []
    manager.save_config = lambda **kwargs: saves.append(kwargs)
    settings = KeyMagneticSettings(
        actuation=1.20,
        rapid_trigger=True,
        rapid_press=0.30,
        rapid_release=0.30,
        lower_dead_zone=0.10,
        upper_dead_zone=0.00,
        deactivation=0.85,
    )

    queued_persistence = []
    manager._schedule_magnetic_persistence = lambda: queued_persistence.append(True)

    manager._write_magnetic_key_automatically(8, settings, [], 1, 0)
    # HID/cache writes keep the foreground runtime intact and only queue one
    # quiet-period config save.  Writing the 300+ KB config immediately after
    # every 0.01-mm step was the source of visible Flet stutter.
    assert saves == []
    assert queued_persistence == [True]

    saves.clear()
    manager._write_magnetic_options_automatically(25, True, 1, 0)
    assert saves == []
    assert queued_persistence == [True, True]

    saves.clear()
    manager._store_magnetic_rt_separate(8, True)
    assert saves == [{"reload_runtime": False}]


def test_magnetic_persistence_coalesces_bursts_and_explicit_flush_keeps_latest_state(monkeypatch):
    """One quiet save must replace many slider-adjacent persistence requests."""

    class ManualTimer:
        timers = []

        def __init__(self, delay, target):
            self.delay = delay
            self.target = target
            self.daemon = False
            self.cancelled = False
            self.started = False
            self.__class__.timers.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def is_alive(self):
            return self.started and not self.cancelled

    monkeypatch.setattr(app_module.threading, "Timer", ManualTimer)
    manager = QMKManager.__new__(QMKManager)
    saves = []
    manager.save_config = lambda **kwargs: saves.append(kwargs)

    first = manager._schedule_magnetic_persistence()
    second = manager._schedule_magnetic_persistence()

    assert second == first + 1
    assert len(ManualTimer.timers) == 2
    assert ManualTimer.timers[0].cancelled is True
    assert ManualTimer.timers[1].delay == app_module.MAGNETIC_BACKGROUND_PERSIST_DEBOUNCE_SEC
    assert saves == []

    # A late cancelled callback cannot flush the newer value early.
    ManualTimer.timers[0].target()
    assert saves == []

    # The newest quiet timer commits exactly one no-runtime-reload snapshot.
    ManualTimer.timers[1].target()
    assert saves == [{"reload_runtime": False}]
    assert manager._flush_magnetic_persistence() is False

    manager._schedule_magnetic_persistence()
    assert manager._flush_magnetic_persistence() is True
    assert saves == [{"reload_runtime": False}, {"reload_runtime": False}]
