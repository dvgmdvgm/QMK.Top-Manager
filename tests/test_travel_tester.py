"""Non-UI safety tests for the magnetic travel visualiser."""
import inspect
import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app_flet import (
    TRAVEL_TESTER_ACTIVE_RENDER_HOLD_SEC,
    TRAVEL_TESTER_HID_SAMPLE_INTERVAL_SEC,
    TRAVEL_TESTER_IDLE_RENDER_INTERVAL_SEC,
    TRAVEL_TESTER_MAX_FRAME_RATE,
    TRAVEL_TESTER_RENDER_INTERVAL_SEC,
    QMKManager,
)
from magnetic import MagneticProtocol, SK75_KEY_BY_SLOT


def _slot_for_label(label):
    return next(slot for slot, key in SK75_KEY_BY_SLOT.items() if key.label == label)


def test_travel_tester_uses_only_normal_windows_key_events():
    assert QMKManager._travel_tester_key_name(_slot_for_label("Q")) == "q"
    assert QMKManager._travel_tester_key_name(_slot_for_label("Space")) == "space"
    assert QMKManager._travel_tester_key_name(_slot_for_label("F8")) == "f8"


def test_travel_tester_does_not_try_to_listen_for_internal_fn_key():
    assert QMKManager._travel_tester_key_name(_slot_for_label("Fn")) is None


def test_travel_tester_reads_win32_key_state_without_registering_a_hook():
    assert QMKManager._travel_tester_virtual_key(_slot_for_label("Q")) == ord("Q")
    assert QMKManager._travel_tester_virtual_key(_slot_for_label("Space")) == 0x20
    assert QMKManager._travel_tester_virtual_key(_slot_for_label("F8")) == 0x77
    assert QMKManager._travel_tester_virtual_key(_slot_for_label("Fn")) is None


def test_travel_tester_identifies_the_any_pressed_physical_key_without_a_hook():
    """The live depth stream has no slot, so the tester may only poll Win32."""
    manager = QMKManager.__new__(QMKManager)
    q_slot = _slot_for_label("Q")
    w_slot = _slot_for_label("W")
    pressed = {QMKManager._travel_tester_virtual_key(w_slot)}
    manager._is_windows_virtual_key_pressed = lambda virtual_key: virtual_key in pressed

    assert QMKManager._travel_tester_pressed_slot(manager) == w_slot

    # Holding Q while W is also briefly down should not make the active-key
    # label jump away from the key that remains held.
    pressed.add(QMKManager._travel_tester_virtual_key(q_slot))
    assert QMKManager._travel_tester_pressed_slot(manager, w_slot) == w_slot

    # Once W is released, the same read-only poll switches to the next key.
    pressed.remove(QMKManager._travel_tester_virtual_key(w_slot))
    assert QMKManager._travel_tester_pressed_slot(manager, w_slot) == q_slot


def test_travel_tester_uses_fast_batched_local_visual_updates():
    source = inspect.getsource(QMKManager._start_magnetic_travel_tester)
    paint_source = inspect.getsource(QMKManager._paint_magnetic_travel_tester)

    assert "TRAVEL_TESTER_RENDER_INTERVAL_SEC" in source
    assert "TRAVEL_TESTER_HID_DRAIN_LIMIT" in source
    assert "TRAVEL_TESTER_HID_SAMPLE_INTERVAL_SEC" in source
    assert "_drain_magnetic_travel_samples" in source
    assert "dynamic_overlay.update()" in paint_source
    assert "CYAN_ACCENT_400" in paint_source
    assert "marker_activation_mm" not in source
    assert "meter_marker" not in paint_source


def test_travel_tester_caps_hid_sampling_and_live_overlay_latency():
    """The reader must never busy-loop a native endpoint in the EXE build."""
    source = inspect.getsource(QMKManager._start_magnetic_travel_tester)

    # Motion may follow a 144 Hz display, but idle firmware reports are
    # deduplicated and the UI queue poll backs off to 60 Hz.
    assert TRAVEL_TESTER_MAX_FRAME_RATE == 144
    assert 1 / 165 <= TRAVEL_TESTER_HID_SAMPLE_INTERVAL_SEC <= 1 / 120
    assert TRAVEL_TESTER_RENDER_INTERVAL_SEC == 1 / 144
    assert TRAVEL_TESTER_IDLE_RENDER_INTERVAL_SEC == 1 / 60
    assert TRAVEL_TESTER_ACTIVE_RENDER_HOLD_SEC >= 0.08
    assert "next_sample_at = time.monotonic()" in source
    assert "wait_seconds = next_sample_at - time.monotonic()" in source
    assert "visible_mm != last_published_visible_mm" in source
    assert "interval - (now - frame_started_at)" in source
    assert "TRAVEL_TESTER_HID_REPORT_WAIT_SEC" not in source
    assert "TRAVEL_TESTER_HID_BACKLOG_YIELD_SEC" not in source


def test_travel_tester_keeps_static_base_outside_the_live_overlay():
    """Ruler/switch chrome is built once; only dynamic controls are patched."""
    source = inspect.getsource(QMKManager._open_magnetic_travel_tester)
    paint_source = inspect.getsource(QMKManager._paint_magnetic_travel_tester)

    assert "tester_static_base = ft.Stack(" in source
    assert "dynamic_overlay = ft.Stack(" in source
    assert "dynamic_overlay=dynamic_overlay" in source
    assert "dynamic_overlay.update()" in paint_source


def test_travel_tester_paint_failure_is_reported_to_the_live_session(caplog):
    """A persistent Flet failure stops the worker instead of being swallowed."""
    class BrokenOverlay:
        def update(self):
            raise RuntimeError("detached Flet control")

    ui = SimpleNamespace(
        full_travel_mm=3.30,
        meter_inner_height=100,
        meter_top=0,
        meter_rail_height=100,
        meter_fill=SimpleNamespace(height=0),
        meter_cursor=SimpleNamespace(height=2),
        meter_track=SimpleNamespace(),
        switch_fill=SimpleNamespace(),
        switch_stem=SimpleNamespace(),
        switch_chamber=SimpleNamespace(),
        value_text=SimpleNamespace(),
        state_text=SimpleNamespace(),
        dynamic_overlay=BrokenOverlay(),
    )
    tester = SimpleNamespace(ui=ui, stop_event=threading.Event())
    manager = QMKManager.__new__(QMKManager)
    manager._magnetic_travel_tester = tester

    painted = QMKManager._paint_magnetic_travel_tester(manager, ui, 1.20, "down")
    # A second physically different value can fail too, but must not flood the
    # log or keep silently retrying a broken Flet control every frame.
    QMKManager._paint_magnetic_travel_tester(manager, ui, 1.21, "down")

    assert painted is False
    assert tester.paint_error == "Не удалось обновить индикатор проверки хода."
    assert ui._travel_paint_error_logged is True
    assert sum(
        "magnetic travel tester paint failed" in record.getMessage()
        for record in caplog.records
    ) == 1


def test_travel_tester_dialog_is_not_seeded_from_the_selected_q_key():
    source = inspect.getsource(QMKManager._open_magnetic_travel_tester)
    assert "magnetic_selected_slot" not in source
    assert "meter_marker" not in source
    assert "линия активации" not in source


def test_travel_tester_keeps_its_live_status_in_a_fixed_visual_region():
    """A longer detected key name must not move the meter after the first press."""
    source = inspect.getsource(QMKManager._open_magnetic_travel_tester)

    # The visual row has one stable width and its dynamic key/status text is
    # constrained to a fixed left column.  ``_paint_*`` only changes values,
    # so no later update can change the row geometry.
    assert "tester_visual_width = 380" in source
    assert "tester_key_column = ft.Container(" in source
    assert "left=0," in source
    assert "width=190" in source
    assert "width=470" in source
    assert "overflow=ft.TextOverflow.ELLIPSIS" in source
    assert '"Любая клавиша"' not in source


def test_travel_tester_confines_live_fill_and_glow_to_the_meter_rail():
    """Full travel must not paint a halo past either physical meter cap."""
    dialog_source = inspect.getsource(QMKManager._open_magnetic_travel_tester)
    paint_source = inspect.getsource(QMKManager._paint_magnetic_travel_tester)

    assert "meter_rail_layers = ft.Stack(" in dialog_source
    assert "meter_fill_glow" in dialog_source
    assert "meter_cursor_glow" in dialog_source
    assert "clip_behavior=ft.ClipBehavior.HARD_EDGE" in dialog_source
    assert "switch_motion_clip = ft.Container(" in dialog_source
    assert "bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST" in dialog_source
    # The painter changes bounded, flat in-rail halo layers rather than
    # gradients or shadows that make the packaged Flutter renderer stutter.
    assert "meter_fill_glow.bgcolor" in paint_source
    assert "meter_cursor_glow.bgcolor" in paint_source
    assert "ui.meter_fill.shadow" not in paint_source
    assert "ui.meter_cursor.shadow" not in paint_source
    assert "ft.BoxShadow" not in paint_source


def test_travel_tester_has_no_neutral_protruding_fill_at_zero():
    """At rest neither switch illustration may retain the old 5 px strip."""
    dialog_source = inspect.getsource(QMKManager._open_magnetic_travel_tester)
    paint_source = inspect.getsource(QMKManager._paint_magnetic_travel_tester)

    assert "switch_fill_height = round(70 * progress)" in paint_source
    assert "switch_fill_height = max(5" not in paint_source
    assert "height=0," in dialog_source

    ui = SimpleNamespace(
        full_travel_mm=3.30,
        meter_inner_height=220,
        meter_top=7,
        meter_rail_height=234,
        meter_fill_glow=SimpleNamespace(height=99, bgcolor=None),
        meter_fill=SimpleNamespace(height=99, bgcolor=None),
        meter_cursor_glow=SimpleNamespace(
            height=10, top=99, opacity=1, bgcolor=None
        ),
        meter_cursor=SimpleNamespace(height=2, bgcolor=None, opacity=1),
        meter_track=SimpleNamespace(),
        switch_fill=SimpleNamespace(height=99, bgcolor=None),
        switch_stem=SimpleNamespace(top=99, bgcolor=None, border=None),
        switch_chamber=SimpleNamespace(border=None),
        value_text=SimpleNamespace(),
        state_text=SimpleNamespace(),
        visual_region=None,
    )

    QMKManager._paint_magnetic_travel_tester(
        QMKManager.__new__(QMKManager), ui, 0.0, None
    )

    assert ui.switch_fill.height == 0
    assert ui.switch_stem.top == 10
    assert ui.meter_cursor.opacity == 0
    assert ui.meter_cursor_glow.opacity == 0


def test_magnetic_travel_threshold_helper_remains_backward_compatible():
    assert QMKManager._magnetic_travel_is_pressed(0.00) is False
    assert QMKManager._magnetic_travel_is_pressed(0.01) is False
    assert QMKManager._magnetic_travel_is_pressed(0.02) is True


def test_travel_tester_colours_the_physical_direction_not_pressed_depth():
    """A reversal must be visible before a key returns to its rest position."""
    assert QMKManager._magnetic_travel_direction(0.00, 0.01) == "down"
    assert QMKManager._magnetic_travel_direction(2.40, 2.39) == "up"
    assert QMKManager._magnetic_travel_direction(1.25, 1.25) is None


def test_travel_tester_stabilises_direction_before_recolouring_the_visuals():
    """Hundredth-mm sensor jitter must not alternate green/blue every frame."""
    assert QMKManager._magnetic_travel_stable_direction(1.20, 1.21) == (None, 1.20)
    assert QMKManager._magnetic_travel_stable_direction(1.20, 1.22) == ("down", 1.22)
    assert QMKManager._magnetic_travel_stable_direction(1.22, 1.21) == (None, 1.22)
    assert QMKManager._magnetic_travel_stable_direction(1.22, 1.20) == ("up", 1.20)


def test_travel_tester_drains_hid_backlog_and_keeps_only_the_newest_depth():
    """A high-rate endpoint must not make the meter replay stale movement."""
    reports = iter(
        [
            [5, 0x1B, 10, 0],
            [5, 0x00, 0, 0],  # unrelated report is ignored
            [5, 0x1B, 80, 0],
            [],
        ]
    )
    newest, reports_read = QMKManager._drain_magnetic_travel_samples(
        lambda: next(reports), step=100, max_reports=64
    )

    assert reports_read == 3
    assert newest == 0.80


def test_travel_tester_hid_drain_has_a_finite_fairness_bound():
    reports = iter([[5, 0x1B, 50, 0]] * 20)
    newest, reports_read = QMKManager._drain_magnetic_travel_samples(
        lambda: next(reports), step=100, max_reports=3
    )

    assert reports_read == 3
    assert newest == 0.50


def test_travel_tester_uses_the_current_official_sk75_3_30_mm_scale():
    """Ticks and endpoint come from MagneticProtocol, never a 3.50 literal."""
    full_travel, ticks = QMKManager._magnetic_travel_tester_scale()

    assert full_travel == MagneticProtocol.OFFICIAL_SK75_ACTUATION_MAX_MM == 3.30
    assert ticks == (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.30)


def test_travel_tester_clamps_live_sample_to_its_official_scale_maximum():
    """An over-range input report cannot animate beyond the 3.30 mm rail."""
    full_travel, _ticks = QMKManager._magnetic_travel_tester_scale()
    ui = SimpleNamespace(
        full_travel_mm=full_travel,
        meter_inner_height=100,
        meter_top=0,
        meter_rail_height=100,
        meter_fill=SimpleNamespace(height=0),
        meter_cursor=SimpleNamespace(height=2),
        meter_track=SimpleNamespace(),
        switch_fill=SimpleNamespace(),
        switch_stem=SimpleNamespace(),
        switch_chamber=SimpleNamespace(),
        value_text=SimpleNamespace(),
        state_text=SimpleNamespace(),
        visual_region=None,
    )

    QMKManager._paint_magnetic_travel_tester(
        QMKManager.__new__(QMKManager), ui, 3.50, "down"
    )

    assert ui.value_text.value == "3.30 мм"
    assert ui.meter_fill.height == 100
    assert ui.meter_cursor.top <= ui.meter_rail_height - ui.meter_cursor.height


def test_travel_tester_skips_subpixel_and_sub_hundredth_duplicate_patches():
    """The UI label is 0.01 mm, so finer samples must not redraw Flet."""
    class Region:
        def __init__(self):
            self.calls = 0

        def update(self):
            self.calls += 1

    region = Region()
    ui = SimpleNamespace(
        full_travel_mm=3.30,
        meter_inner_height=100,
        meter_top=0,
        meter_rail_height=100,
        meter_fill=SimpleNamespace(height=0),
        meter_cursor=SimpleNamespace(height=2),
        meter_track=SimpleNamespace(),
        switch_fill=SimpleNamespace(),
        switch_stem=SimpleNamespace(),
        switch_chamber=SimpleNamespace(),
        value_text=SimpleNamespace(),
        state_text=SimpleNamespace(),
        visual_region=region,
    )
    manager = QMKManager.__new__(QMKManager)

    QMKManager._paint_magnetic_travel_tester(manager, ui, 1.2001, "down")
    QMKManager._paint_magnetic_travel_tester(manager, ui, 1.2004, "down")

    assert region.calls == 1
    assert ui.value_text.value == "1.20 мм"


def test_auto_magnetic_write_does_not_rebuild_the_keyboard_while_dragging():
    """A debounced slider write must leave the current page/visual deck alone."""
    calls = []
    manager = SimpleNamespace(
        _magnetic_write_lock=threading.Lock(),
        usb_lock=threading.Lock(),
        _magnetic_write_revisions={8: 3},
        _send_lighting_packets_locked=lambda packets, label, inter_packet_delay=0.0: calls.append(
            (packets, label, inter_packet_delay)
        ),
        _cache_magnetic_settings=lambda slot, settings, profile_index: calls.append(
            (slot, settings, profile_index)
        ),
        save_config=lambda **_kwargs: calls.append("save"),
        _refresh_sk75_keyboard_picker=lambda: (_ for _ in ()).throw(
            AssertionError("slider write rebuilt the full keyboard")
        ),
    )

    QMKManager._write_magnetic_key_automatically(
        manager, 8, object(), [[1, 2, 3]], 3, 0
    )

    assert calls[-1] == "save"


def test_late_dismiss_of_old_tester_cannot_stop_reopened_tester():
    current = SimpleNamespace(
        dialog_token=2,
        stop_event=threading.Event(),
        ui=SimpleNamespace(),
    )
    manager = SimpleNamespace(
        _magnetic_travel_tester=current,
        _magnetic_travel_tester_token=10,
    )

    # Flet may dispatch the dismiss callback from dialog #1 after dialog #2
    # already opened.  It must leave dialog #2's reader alone.
    QMKManager._stop_magnetic_travel_tester(
        manager, reset_ui=False, expected_dialog_token=1
    )

    assert manager._magnetic_travel_tester is current
    assert current.stop_event.is_set() is False
