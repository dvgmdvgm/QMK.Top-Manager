"""Focused safety coverage for the official SK75 calibration workflow."""
import inspect
import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app_flet
from app_flet import QMKManager, SK75_OFFICIAL_VISUAL_LAYOUT, _sk75_visual_deck_geometry
from magnetic import MagneticProtocol


def _calibration_reports():
    values = [0] * 128
    values[8] = 150
    values[65] = 300
    raw = []
    for value in values:
        raw.extend([value & 0xFF, value >> 8])
    return {
        chunk_index: [0] + raw[chunk_index * 64:(chunk_index + 1) * 64]
        for chunk_index in range(MagneticProtocol.CALIBRATION_PROGRESS_CHUNKS)
    }


def test_calibration_reader_only_issues_official_progress_reads_under_its_caller_lock():
    manager = QMKManager.__new__(QMKManager)
    source_reports = _calibration_reports()
    calls = []

    def query(packet, label):
        calls.append((packet, label))
        return source_reports[packet[3]]

    manager._query_magnetic_packet_locked = query
    levels = QMKManager._read_magnetic_calibration_progress_locked(manager, "cal-test")

    assert levels[8] == 150
    assert levels[65] == 300
    assert [packet[:4] for packet, _label in calls] == [
        [0xE5, 0xFE, 1, 0],
        [0xE5, 0xFE, 1, 1],
        [0xE5, 0xFE, 1, 2],
        [0xE5, 0xFE, 1, 3],
    ]
    assert all(label.startswith("cal-test_") for _packet, label in calls)


def test_calibration_reusable_feature_handle_avoids_generic_hid_reopen_per_chunk():
    """The live calibration path can keep one read-only handle for all chunks."""

    source_reports = _calibration_reports()

    class ReusableDevice:
        def __init__(self):
            self.sent = []

        def send_feature_report(self, packet):
            self.sent.append(list(packet))
            return len(packet)

        def get_feature_report(self, _report_id, _size):
            # HID reports have a leading report ID, so chunk is packet[4].
            return source_reports[self.sent[-1][4]]

    manager = QMKManager.__new__(QMKManager)
    manager._query_magnetic_packet_locked = lambda *_args: (_ for _ in ()).throw(
        AssertionError("the generic per-query reader must not run")
    )
    device = ReusableDevice()

    with patch.object(app_flet.time, "sleep") as pause:
        levels = QMKManager._read_magnetic_calibration_progress_locked(
            manager, "fast-cal", feature_device=device
        )

    assert levels[8] == 150
    assert levels[65] == 300
    assert [packet[1:5] for packet in device.sent] == [
        [0xE5, 0xFE, 1, 0],
        [0xE5, 0xFE, 1, 1],
        [0xE5, 0xFE, 1, 2],
        [0xE5, 0xFE, 1, 3],
    ]
    assert pause.call_count == MagneticProtocol.CALIBRATION_PROGRESS_CHUNKS
    assert all(
        call.args == (app_flet.CALIBRATION_FEATURE_REPORT_SETTLE_SECONDS,)
        for call in pause.call_args_list
    )


def test_calibration_stop_marks_the_session_and_restores_only_local_buttons():
    updates = []
    session = SimpleNamespace(
        stop_event=threading.Event(),
        dialog_token=7,
        ui=SimpleNamespace(
            start_button=SimpleNamespace(disabled=True),
            stop_button=SimpleNamespace(disabled=False),
            status_text=SimpleNamespace(value="", color=None),
            control_region=SimpleNamespace(update=lambda: updates.append(True)),
            keycaps={8: object(), 65: object(), 99: object()},
        ),
        completed_slots={8, 65},
        completion_lock=threading.Lock(),
    )
    manager = SimpleNamespace(
        _magnetic_calibration=session,
        _magnetic_calibration_token=11,
        _magnetic_calibration_lifecycle_lock=threading.RLock(),
    )

    first = QMKManager._stop_magnetic_calibration(
        manager, reset_ui=True, expected_dialog_token=7
    )
    # A Close button and the modal's later on_dismiss can arrive together.
    # The second callback must only observe an already-detached session.
    second = QMKManager._stop_magnetic_calibration(
        manager, reset_ui=True, expected_dialog_token=7
    )

    assert first is session
    assert second is None
    assert session.stop_event.is_set() is True
    assert manager._magnetic_calibration is None
    assert manager._magnetic_calibration_token == 12
    assert session.ui.start_button.disabled is False
    assert session.ui.stop_button.disabled is True
    assert "2/3" in session.ui.status_text.value
    assert "только остальные" in session.ui.status_text.value
    assert updates == [True]


def test_calibration_completion_marks_only_womiers_finished_visible_keys():
    completed = QMKManager._calibration_completed_slots(
        {8: 300, 65: 299, 99: 999, 127: 300},
        firmware_version=0x0308,
        visible_slots={8, 65, 99},
    )

    assert completed == {8, 99}
    assert QMKManager._calibration_completed_slots(
        {8: 1, 65: 0}, firmware_version=767, visible_slots={8, 65}
    ) == {8}


def test_calibration_ignores_held_startup_key_until_a_fresh_release_then_press():
    """A full first 0xFE value is not proof that the user meant to calibrate."""
    armed = {8}
    completed = set()

    # Slot 65 is still held at startup, so only the released slot 8 can
    # become complete on this first edge.
    armed, newly_completed = QMKManager._calibration_new_completion_edges(
        {8: 0, 65: 300},
        {8: 300, 65: 300},
        armed,
        completed,
        firmware_version=0x0308,
        visible_slots={8, 65},
    )
    assert newly_completed == {8}
    assert 65 not in armed

    completed.update(newly_completed)
    # Releasing the held key arms it but does not complete it.
    armed, newly_completed = QMKManager._calibration_new_completion_edges(
        {8: 300, 65: 300},
        {8: 300, 65: 0},
        armed,
        completed,
        firmware_version=0x0308,
        visible_slots={8, 65},
    )
    assert newly_completed == set()
    assert 65 in armed

    # It is counted only after the deliberate new press.
    armed, newly_completed = QMKManager._calibration_new_completion_edges(
        {8: 300, 65: 0},
        {8: 300, 65: 300},
        armed,
        completed,
        firmware_version=0x0308,
        visible_slots={8, 65},
    )
    assert newly_completed == {65}


def test_calibration_fresh_gate_requires_stable_release_and_the_official_full_hold():
    """Startup full values cannot become green until a real fresh press."""
    visible = {8, 65}
    streaks = {}

    # Slot 65 is already full while the firmware is settling.  Slot 8 has a
    # quiet low level for four polls, so only 8 can be armed at the guard
    # boundary.
    for _ in range(4):
        streaks, stable = QMKManager._calibration_stable_released_slots(
            {8: 0, 65: 300},
            streaks,
            firmware_version=0x0308,
            visible_slots=visible,
        )
    assert stable == {8}

    armed = set(stable)
    completed = set()
    full_since = {}

    # The stale full key is not armed and must never be claimed just because
    # it remains at 300 after the guarded startup phase.
    armed, newly, streaks, full_since = QMKManager._calibration_confirmed_completion_edges(
        {8: 0, 65: 300},
        {8: 0, 65: 300},
        armed,
        completed,
        release_streaks=streaks,
        full_since=full_since,
        now=1.00,
        firmware_version=0x0308,
        visible_slots=visible,
    )
    assert newly == set()
    assert 65 not in armed

    # A deliberate switch starts its full dwell, but one snapshot is still
    # insufficient: transient 0xFE spikes must not become a completion.
    armed, newly, streaks, full_since = QMKManager._calibration_confirmed_completion_edges(
        {8: 0, 65: 300},
        {8: 300, 65: 300},
        armed,
        completed,
        release_streaks=streaks,
        full_since=full_since,
        now=1.10,
        firmware_version=0x0308,
        visible_slots=visible,
    )
    assert newly == set()

    armed, newly, streaks, full_since = QMKManager._calibration_confirmed_completion_edges(
        {8: 300, 65: 300},
        {8: 300, 65: 300},
        armed,
        completed,
        release_streaks=streaks,
        full_since=full_since,
        now=2.11,
        firmware_version=0x0308,
        visible_slots=visible,
    )
    assert newly == {8}

    # A previously held key needs its own stable release cycle before it can
    # ever be considered, even if its old full value persists.
    completed.update(newly)
    for moment in (2.15, 2.20, 2.25, 2.30):
        armed, newly, streaks, full_since = QMKManager._calibration_confirmed_completion_edges(
            {8: 300, 65: 300},
            {8: 300, 65: 0},
            armed,
            completed,
            release_streaks=streaks,
            full_since=full_since,
            now=moment,
            firmware_version=0x0308,
            visible_slots=visible,
        )
        assert newly == set()
    assert 65 in armed


def test_calibration_paint_keeps_the_dialog_completion_set_for_stop_and_resume():
    """The reader and dialog must retain one shared partial-completion set."""
    owned_slots = {8}
    ui = SimpleNamespace(
        keycaps={},
        completed_slots=owned_slots,
        rendered_levels={},
        rendered_baseline_levels={},
        rendered_complete_count=None,
        progress_text=SimpleNamespace(value="", color=None, update=lambda: None),
    )

    QMKManager._paint_magnetic_calibration(
        QMKManager.__new__(QMKManager),
        ui,
        {},
        completed_slots={8},
    )

    assert ui.completed_slots is owned_slots
    # This mimics the HID worker observing the second key after the UI paint.
    owned_slots.add(65)
    assert ui.completed_slots == {8, 65}


def test_calibration_open_preread_is_read_only_and_never_promotes_old_progress():
    """Old 0xFE=300 values must not look like fresh accidental presses."""
    updates = []
    ui = SimpleNamespace(
        keycaps={8: object(), 65: object(), 99: object()},
        completed_slots=set(),
        pre_read_cancel_event=threading.Event(),
        status_text=SimpleNamespace(value="", color=None),
        control_region=SimpleNamespace(update=lambda: updates.append(True)),
    )
    manager = QMKManager.__new__(QMKManager)
    manager._magnetic_calibration_dialog_token = 7
    manager._magnetic_calibration = None
    manager._magnetic_calibration_session_lock = threading.Lock()
    manager.usb_lock = threading.Lock()
    manager._query_magnetic_packet_locked = lambda _packet, _label: [0] * 65
    manager._read_magnetic_calibration_progress_locked = lambda _label: {
        8: 300,
        65: 0,
        99: 300,
    }
    manager._ui_call = lambda callback: callback()
    manager._send_lighting_packets_locked = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("open pre-read must not write a calibration command")
    )
    QMKManager._prime_magnetic_calibration_progress(manager, ui, dialog_token=7)
    ui.pre_read_thread.join(timeout=1.0)

    assert ui.pre_read_thread.is_alive() is False
    assert ui.completed_slots == set()
    assert ui.pre_start_levels == {8: 300, 65: 0, 99: 300}
    assert "не запущена" in ui.status_text.value
    assert updates == [True]


def test_calibration_ui_uses_its_own_board_and_exact_cleanup_protocol():
    open_source = inspect.getsource(QMKManager._open_magnetic_calibration)
    start_source = inspect.getsource(QMKManager._start_magnetic_calibration)
    build_source = inspect.getsource(QMKManager._build_sk75_calibration_layout)
    preread_source = inspect.getsource(QMKManager._prime_magnetic_calibration_progress)

    assert '"Калибровка магнитных клавиш"' in open_source
    assert "_build_sk75_calibration_layout" in open_source
    assert "calibration_start_packets" in start_source
    assert "calibration_stop_packet" in start_source
    assert "_magnetic_calibration_session_lock" in start_source
    assert "finally:" in start_source
    assert "CALIBRATION_START_SETTLE_SECONDS" in start_source
    assert "CALIBRATION_POST_START_GUARD_SECONDS" in start_source
    assert start_source.index("CALIBRATION_START_SETTLE_SECONDS") < start_source.index(
        "calibration_start_packets()"
    )
    # Existing completion must not be recovered from 0xFE before Start; the
    # live start baseline must not turn a held raw value into a completed key.
    assert "_calibration_completed_slots(" not in start_source
    assert "await asyncio.sleep(0.025)" in start_source
    assert start_source.count("calibration_stop_packet") == 1
    assert "stop_packet_attempted" in start_source
    assert "cleanup_complete" in start_source
    assert "baseline" not in build_source
    assert "fill" in build_source
    assert "_prime_magnetic_calibration_progress" not in open_source
    assert "calibration_start_packets" not in preread_source
    assert "calibration_stop_packet" not in preread_source


def test_calibration_is_not_exposed_in_the_left_rail():
    source = inspect.getsource(QMKManager._build_ui)
    assert "magnetic_calibration_nav_button" not in source
    assert "_open_magnetic_calibration()" not in source
    assert "self.magnetic_tester_nav_button" in source


def test_calibration_indicator_selects_the_latest_changed_visible_key_only():
    slot, raw = QMKManager._calibration_live_change(
        {8: 150, 65: 100, 99: 50},
        {8: 154, 65: 220, 99: 300},
        visible_slots={8, 65},
    )

    assert slot == 65
    assert raw == 220
    assert QMKManager._calibration_live_change({8: 1}, {8: 1}, {8}) == (None, 0)


def test_calibration_indicator_is_read_only_and_throttled_with_local_trace():
    start_source = inspect.getsource(QMKManager._start_magnetic_calibration)
    paint_source = inspect.getsource(QMKManager._paint_magnetic_calibration_telemetry)
    open_source = inspect.getsource(QMKManager._open_magnetic_calibration)

    assert "_calibration_live_change" in start_source
    assert "_paint_magnetic_calibration_telemetry" in start_source
    assert "live_history" in start_source
    assert "magnetism_report_packet" not in start_source
    assert "await asyncio.sleep(0.025)" in start_source
    assert "live_trace_bars" in paint_source
    assert "live_meter_layers" in paint_source
    assert "не измеренный ход клавиши" in open_source


def test_calibration_deck_keeps_the_complete_75_percent_case_inside_its_padded_board():
    """The calibration modal must not clip Home/End/PgUp/PgDn/right arrows."""
    manager = QMKManager.__new__(QMKManager)
    board, _keycaps, fill_height = manager._build_sk75_calibration_layout()
    geometry = _sk75_visual_deck_geometry(compact=True, deck_width=850)

    assert fill_height == 30
    # Container width includes the 8 px padding on both sides; the positioned
    # case itself remains the 850 px geometry it was measured with.
    assert board.width == geometry.deck_width + 16
    assert board.padding == 8
    assert len(board.content.controls) == len(SK75_OFFICIAL_VISUAL_LAYOUT)
    for row, widths in zip(board.content.controls, geometry.key_widths):
        assert row.width == geometry.row_width
        assert all(
            key.left + width <= row.width
            for key, width in zip(row.controls, widths)
        )


def test_calibration_painter_coalesces_unchanged_caps_into_one_changed_row_patch():
    """A repeated 81-slot report must not redraw the whole modal/deck."""

    class Counter:
        def __init__(self):
            self.calls = 0

        def update(self):
            self.calls += 1

    def reference(row):
        return SimpleNamespace(
            fill=SimpleNamespace(height=0, bgcolor=None, opacity=None),
            keycap=SimpleNamespace(border=None),
            label_text=SimpleNamespace(color=None),
            row=row,
        )

    first_row = Counter()
    second_row = Counter()
    progress = Counter()
    ui = SimpleNamespace(
        keycaps={8: reference(first_row), 65: reference(second_row)},
        fill_height=30,
        progress_text=SimpleNamespace(value="", color=None, update=progress.update),
        completed_slots=set(),
    )
    manager = QMKManager.__new__(QMKManager)

    QMKManager._paint_magnetic_calibration(
        manager, ui, {8: 150, 65: 0}, firmware_version=0x0308
    )
    assert first_row.calls == 1
    assert second_row.calls == 0
    # Same raw packet has no visible-pixel diff and queues no row update.
    QMKManager._paint_magnetic_calibration(
        manager, ui, {8: 150, 65: 0}, firmware_version=0x0308
    )
    assert first_row.calls == 1
    assert second_row.calls == 0
    # A different physical slot patches only its own row.
    QMKManager._paint_magnetic_calibration(
        manager, ui, {8: 150, 65: 150}, firmware_version=0x0308
    )
    assert first_row.calls == 1
    assert second_row.calls == 1


def test_live_calibration_deck_paints_only_confirmed_completion_rows():
    """The meter must not be blocked by raw animation across all 81 caps."""

    class Counter:
        def __init__(self):
            self.calls = 0

        def update(self):
            self.calls += 1

    def reference(row):
        return SimpleNamespace(
            fill=SimpleNamespace(height=0, bgcolor=None, opacity=None),
            keycap=SimpleNamespace(border=None),
            label_text=SimpleNamespace(color=None),
            row=row,
            rendered_complete=False,
        )

    first_row = Counter()
    second_row = Counter()
    ui = SimpleNamespace(
        keycaps={8: reference(first_row), 65: reference(second_row)},
        fill_height=30,
        progress_text=SimpleNamespace(value="", color=None, update=lambda: None),
        completed_slots=set(),
        rendered_levels={8: 0, 65: 0},
        rendered_baseline_levels={8: 0, 65: 0},
        rendered_complete_count=0,
    )
    manager = QMKManager.__new__(QMKManager)

    # Raw 0xFE motion alone does not repaint either row during a live run.
    QMKManager._paint_magnetic_calibration(
        manager,
        ui,
        {8: 150, 65: 120},
        firmware_version=0x0308,
        completed_slots=set(),
        allow_raw_completion=False,
        completed_only=True,
    )
    assert first_row.calls == second_row.calls == 0

    # Only the freshly confirmed completion row is patched.
    QMKManager._paint_magnetic_calibration(
        manager,
        ui,
        {8: 300, 65: 120},
        firmware_version=0x0308,
        completed_slots={8},
        allow_raw_completion=False,
        completed_only=True,
    )
    assert first_row.calls == 1
    assert second_row.calls == 0


def test_calibration_telemetry_uses_mm_range_without_claiming_live_physical_travel():
    class Counter:
        def __init__(self):
            self.calls = 0

        def update(self):
            self.calls += 1

    region = Counter()
    ui = SimpleNamespace(
        calibrated_range_mm=3.30,
        live_meter_inner_height=156,
        live_meter_inner_top=5,
        live_meter_rail_height=166,
        live_meter_fill=SimpleNamespace(height=0),
        live_meter_fill_glow=SimpleNamespace(height=0),
        live_meter_cursor=SimpleNamespace(height=2, top=0),
        live_key_text=SimpleNamespace(value=""),
        live_value_text=SimpleNamespace(value=""),
        live_trace_bars=[],
        live_trace_height=42,
        live_region=region,
    )
    manager = QMKManager.__new__(QMKManager)
    manager._sk75_key_name = lambda _slot: "F1"

    QMKManager._paint_magnetic_calibration_telemetry(manager, ui, 8, 0.5, [0.5])
    assert ui.live_value_text.value == "1.65 / 3.30 мм"
    assert ui.live_meter_inner_top <= ui.live_meter_cursor.top <= 159
    assert region.calls == 1
    # An identical visible frame does not schedule another local Flet patch.
    QMKManager._paint_magnetic_calibration_telemetry(manager, ui, 8, 0.5, [0.5])
    assert region.calls == 1


def test_calibration_telemetry_updates_only_one_small_local_subtree_per_tick():
    class Counter:
        def __init__(self):
            self.calls = 0

        def update(self):
            self.calls += 1

    meter = Counter()
    details = Counter()
    whole_region = Counter()
    ui = SimpleNamespace(
        calibrated_range_mm=3.30,
        live_meter_inner_height=156,
        live_meter_inner_top=5,
        live_meter_rail_height=166,
        live_meter_fill=SimpleNamespace(height=0),
        live_meter_fill_glow=SimpleNamespace(height=0),
        live_meter_cursor=SimpleNamespace(height=2, top=0),
        live_key_text=SimpleNamespace(value=""),
        live_value_text=SimpleNamespace(value=""),
        live_trace_bars=[],
        live_trace_height=42,
        live_meter_layers=meter,
        live_summary_region=details,
        live_region=whole_region,
        # Make the second call choose the rail after the first detail frame.
        calibration_detail_interval=999.0,
    )
    manager = QMKManager.__new__(QMKManager)
    manager._sk75_key_name = lambda _slot: "F1"

    QMKManager._paint_magnetic_calibration_telemetry(manager, ui, 8, 0.5, [0.5])
    assert (meter.calls, details.calls, whole_region.calls) == (0, 1, 0)
    QMKManager._paint_magnetic_calibration_telemetry(manager, ui, 8, 0.5, [0.5])
    assert (meter.calls, details.calls, whole_region.calls) == (1, 1, 0)


def test_calibration_cleanup_wait_is_bounded_and_tray_quit_uses_worker_result():
    session = SimpleNamespace(cleanup_complete=threading.Event())

    assert QMKManager._wait_for_magnetic_calibration_cleanup(session, timeout=0) is False
    session.cleanup_complete.set()
    assert QMKManager._wait_for_magnetic_calibration_cleanup(session, timeout=0) is True
    assert QMKManager._wait_for_magnetic_calibration_cleanup(SimpleNamespace(), timeout=0) is True

    quit_source = inspect.getsource(QMKManager._tray_quit)
    assert "_wait_for_magnetic_calibration_cleanup" in quit_source
    assert "calibration_session" in quit_source


def test_calibration_stop_is_wired_to_close_dismiss_hide_and_quit_paths():
    open_source = inspect.getsource(QMKManager._open_magnetic_calibration)
    hide_source = inspect.getsource(QMKManager._hide_window)
    quit_source = inspect.getsource(QMKManager._tray_quit)

    assert "def close" in open_source
    assert "on_dismiss=" in open_source
    assert open_source.count("_stop_magnetic_calibration") >= 3
    assert "_stop_magnetic_calibration(reset_ui=False)" in hide_source
    assert "_stop_magnetic_calibration(reset_ui=False)" in quit_source


def test_calibration_has_explicit_safe_cancel_without_a_rollback_write():
    open_source = inspect.getsource(QMKManager._open_magnetic_calibration)

    assert "def cancel_calibration" in open_source
    assert '"Отменить калибровку"' in open_source
    assert "Уже завершённые клавиши Womier не откатывает" in open_source
    # The only stop write stays worker-owned; Cancel only requests it and
    # closes the modal, so it cannot invent a destructive reset protocol.
    assert "calibration_stop_packet" not in open_source


def test_calibration_cancel_closes_its_own_dialog_before_feedback_snackbar():
    """Cancel must not pop the snackbar and leave a ghost calibration modal."""

    class Page:
        def __init__(self):
            self.dialogs = []
            self.pop_calls = 0

        def show_dialog(self, dialog):
            dialog.open = True
            self.dialogs.append(dialog)

        def pop_dialog(self):
            self.pop_calls += 1

    events = []
    page = Page()
    manager = QMKManager.__new__(QMKManager)
    manager.page = page
    manager._magnetic_calibration_dialog_token = 0
    manager._stop_magnetic_travel_tester = lambda **_kwargs: None
    manager._stop_magnetic_calibration = lambda **kwargs: events.append(
        ("stop", kwargs)
    )
    manager._snack = lambda message: events.append(("snack", message))

    QMKManager._open_magnetic_calibration(manager)
    dialog = page.dialogs[-1]
    cancel = dialog.actions[0]
    cancel.on_click(None)

    assert dialog.open is False
    # SnackBar itself is also a DialogControl in Flet.  The former order
    # showed it first and pop_dialog() therefore closed the wrong control.
    assert page.pop_calls == 0
    assert ("stop", {"reset_ui": False, "expected_dialog_token": 1}) in events
    assert events[-1][0] == "snack"
    # A fresh panel can now be opened immediately; the old dialog is closed
    # rather than remaining as an invisible click-blocking modal in Flet's
    # dialog stack.
    QMKManager._open_magnetic_calibration(manager)
    assert page.dialogs[-1] is not dialog
    assert page.dialogs[-1].open is True


def test_calibration_start_ignores_stale_and_duplicate_dialog_clicks():
    """Late Flet events must not replace a live session after re-opening."""
    labels = []
    started = threading.Event()

    def send(_packets, label, inter_packet_delay=0.0):
        labels.append(label)
        if label == "magnetic_calibration_start":
            started.set()

    ui = SimpleNamespace(
        start_button=SimpleNamespace(disabled=False),
        stop_button=SimpleNamespace(disabled=True),
        status_text=SimpleNamespace(value="", color=None),
        control_region=SimpleNamespace(update=lambda: None),
        keycaps={65: object()},
        completed_slots=set(),
        pre_read_cancel_event=threading.Event(),
        dialog_closing_event=threading.Event(),
    )
    manager = QMKManager.__new__(QMKManager)
    manager._active_device = lambda: {"keyboard_type": "magnetic"}
    manager._snack = lambda _text: None
    manager._stop_magnetic_travel_tester = lambda **_kwargs: None
    manager._magnetic_calibration_lifecycle_lock = threading.RLock()
    manager._magnetic_calibration_session_lock = threading.Lock()
    manager.usb_lock = threading.Lock()
    manager._magnetic_calibration = None
    manager._magnetic_calibration_token = 0
    manager._magnetic_calibration_dialog_token = 22
    manager._query_magnetic_packet_locked = lambda _packet, _label: [0] * 65
    manager._read_magnetic_calibration_progress_locked = lambda _label: {}
    manager._send_lighting_packets_locked = send
    manager.page = SimpleNamespace(run_task=lambda _task: None)

    QMKManager._start_magnetic_calibration(manager, ui, dialog_token=22)
    session = manager._magnetic_calibration
    assert session is not None
    # A repeated click from the same dialog is ignored rather than stopping
    # and recreating the session while its worker owns the HID endpoint.
    QMKManager._start_magnetic_calibration(manager, ui, dialog_token=22)
    assert manager._magnetic_calibration is session
    # A queued click from the previous dialog is ignored before it can touch
    # either the worker or the firmware.
    QMKManager._start_magnetic_calibration(manager, ui, dialog_token=21)
    assert manager._magnetic_calibration is session

    assert started.wait(timeout=1.0)
    detached = QMKManager._stop_magnetic_calibration(
        manager, reset_ui=False, expected_dialog_token=22
    )
    assert detached is session
    assert QMKManager._wait_for_magnetic_calibration_cleanup(session, timeout=1.0)
    session.reader_thread.join(timeout=0.2)
    assert labels.count("magnetic_calibration_start") == 1
    assert labels.count("magnetic_calibration_stop") == 1


def test_close_and_dismiss_request_one_worker_owned_stop_packet_without_hid():
    """Two UI cleanup callbacks must not issue two firmware stop writes."""
    labels = []
    started = threading.Event()

    def send(packets, label, inter_packet_delay=0.0):
        labels.append((label, packets))
        if label == "magnetic_calibration_start":
            started.set()

    ui = SimpleNamespace(
        start_button=SimpleNamespace(disabled=False),
        stop_button=SimpleNamespace(disabled=True),
        status_text=SimpleNamespace(value="", color=None),
        control_region=SimpleNamespace(update=lambda: None),
        keycaps={65: object(), 99: object()},
        completed_slots={65},
        pre_read_cancel_event=threading.Event(),
    )
    manager = QMKManager.__new__(QMKManager)
    manager._active_device = lambda: {"keyboard_type": "magnetic"}
    manager._snack = lambda _text: None
    manager._stop_magnetic_travel_tester = lambda **_kwargs: None
    manager._magnetic_calibration_lifecycle_lock = threading.RLock()
    manager._magnetic_calibration_session_lock = threading.Lock()
    manager.usb_lock = threading.Lock()
    manager._magnetic_calibration = None
    manager._magnetic_calibration_token = 0
    manager._query_magnetic_packet_locked = lambda _packet, _label: [0] * 65
    manager._read_magnetic_calibration_progress_locked = lambda _label: {}
    manager._send_lighting_packets_locked = send
    manager.page = SimpleNamespace(run_task=lambda _task: None)

    QMKManager._start_magnetic_calibration(manager, ui, dialog_token=5)
    session = manager._magnetic_calibration
    assert session is not None
    # Stop/Start in one open dialog resumes from the already observed key,
    # rather than erasing partial progress back to a misleading 0/81.
    assert session.completed_slots is ui.completed_slots
    assert session.completed_slots == {65}
    assert ui.pre_read_cancel_event.is_set() is True
    assert started.wait(timeout=1.0)

    first = QMKManager._stop_magnetic_calibration(
        manager, reset_ui=False, expected_dialog_token=5
    )
    second = QMKManager._stop_magnetic_calibration(
        manager, reset_ui=False, expected_dialog_token=5
    )
    assert first is session
    assert second is None
    assert QMKManager._wait_for_magnetic_calibration_cleanup(session, timeout=1.0) is True
    session.reader_thread.join(timeout=0.2)

    assert [label for label, _packets in labels].count("magnetic_calibration_start") == 1
    assert [label for label, _packets in labels].count("magnetic_calibration_stop") == 1
