"""Focused regression checks for the compact magnetic/UI cleanup."""
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

import flet as ft

from app_flet import QMKManager


def test_ui_call_marshals_callbacks_via_page_run_task_not_worker_thread():
    """Tray/HID callbacks must return to Flet's UI event loop before mutating UI."""
    calls = []

    class Page:
        def run_thread(self, _handler):
            raise AssertionError("UI callbacks must not be sent to run_thread()")

        def run_task(self, handler):
            assert inspect.iscoroutinefunction(handler)
            calls.append("scheduled")
            return asyncio.run(handler())

    manager = QMKManager.__new__(QMKManager)
    manager.page = Page()

    manager._ui_call(lambda: calls.append("mutated"))

    assert calls == ["scheduled", "mutated"]


def test_ui_call_drops_callbacks_queued_before_quit_but_allows_final_shell_teardown():
    """Late worker UI callbacks must not mutate Flet after the app is gone."""

    queued = []

    class Page:
        def run_task(self, handler):
            queued.append(handler)

    manager = QMKManager.__new__(QMKManager)
    manager.page = Page()
    manager.app_alive = True
    calls = []

    manager._ui_call(lambda: calls.append("late worker"))
    manager.app_alive = False
    asyncio.run(queued.pop()())

    # The callback was already accepted by Flet before Quit, but its body is
    # checked again on the page loop and therefore does nothing.
    assert calls == []

    # The one explicit shutdown task is intentionally still allowed to close
    # the native shell after app_alive has been cleared.
    manager._ui_call(lambda: calls.append("destroy shell"), allow_shutdown=True)
    asyncio.run(queued.pop()())
    assert calls == ["destroy shell"]


def test_late_window_close_event_is_ignored_after_shutdown_has_started():
    manager = QMKManager.__new__(QMKManager)
    manager.app_alive = False
    calls = []
    manager._hide_window = lambda: calls.append("hide")

    manager._handle_window_event(SimpleNamespace(type="close"))

    assert calls == []


def test_rt_switches_keep_labels_in_the_material3_surface_not_native_switches():
    manager = QMKManager.__new__(QMKManager)
    manager.magnetic_rt_switch = SimpleNamespace(value=False, label="")

    manager._update_magnetic_rt_label()

    assert manager.magnetic_rt_switch.label is None

    manager.magnetic_rt_switch.value = True
    manager.magnetic_rt_separate_switch = SimpleNamespace(value=True, label="")
    manager.magnetic_rt_press_control = SimpleNamespace(visible=False)

    manager._update_magnetic_rt_separation_ui()

    assert manager.magnetic_rt_separate_switch.label is None
    assert manager.magnetic_rt_press_control.visible is True


def test_normal_rt_uses_release_threshold_and_hides_optional_repeat_down_bar():
    """The extra downstroke setting must not replace the ordinary RT scale."""
    manager = QMKManager.__new__(QMKManager)
    manager.magnetic_rt_separate_switch = SimpleNamespace(value=False, label="")
    manager.magnetic_rt_release_slider = SimpleNamespace(value=22)
    manager.magnetic_rt_press_slider = SimpleNamespace(value=71)
    manager.magnetic_rt_press_control = SimpleNamespace(visible=True)
    manager._set_vertical_magnetic_value = lambda slider, value, **_: setattr(slider, "value", value)

    manager._update_magnetic_rt_separation_ui()

    assert manager.magnetic_rt_press_control.visible is False
    assert manager.magnetic_rt_press_slider.value == 22


def test_extra_repeat_down_rt_does_not_swap_protocol_fields():
    """Changing the visual order must retain the firmware's press/release mapping."""
    manager = QMKManager.__new__(QMKManager)
    manager.magnetic_actuation_slider = SimpleNamespace(value=120)
    manager.magnetic_rt_release_slider = SimpleNamespace(value=45)
    manager.magnetic_rt_press_slider = SimpleNamespace(value=30)
    manager.magnetic_lower_dead_zone_slider = SimpleNamespace(value=10)
    manager.magnetic_upper_dead_zone_slider = SimpleNamespace(value=15)
    manager.magnetic_rt_switch = SimpleNamespace(value=True)

    manager.magnetic_rt_separate_switch = SimpleNamespace(value=False)
    shared = manager._magnetic_settings_from_controls()
    assert shared.rapid_release == 0.45
    assert shared.rapid_press == 0.45

    manager.magnetic_rt_separate_switch.value = True
    separate = manager._magnetic_settings_from_controls()
    assert separate.rapid_release == 0.45
    assert separate.rapid_press == 0.30


def test_notifications_use_a_compact_dark_floating_toast():
    shown = []
    manager = QMKManager.__new__(QMKManager)
    manager.page = SimpleNamespace(show_dialog=shown.append)

    manager._snack("HEX скопирован")

    assert len(shown) == 1
    toast = shown[0]
    assert isinstance(toast, ft.SnackBar)
    assert toast.behavior == ft.SnackBarBehavior.FLOATING
    assert toast.bgcolor == ft.Colors.SURFACE_CONTAINER_HIGHEST
    assert toast.width == 420
    assert isinstance(toast.shape, ft.RoundedRectangleBorder)


def test_snap_pair_remove_action_is_gone_but_clear_all_remains():
    source = Path(__file__).resolve().parents[1].joinpath("app_flet.py").read_text(
        encoding="utf-8"
    )

    assert "Убрать выбранную пару" not in source
    assert not hasattr(QMKManager, "_magnetic_clear_snap_pair")
    assert "Убрать все Snap Key" in source


def test_left_sections_header_uses_native_non_maximizing_drag_area():
    source = Path(__file__).resolve().parents[1].joinpath("app_flet.py").read_text(
        encoding="utf-8"
    )

    assert "ft.WindowDragArea(" in source
    assert "maximizable=False" in source
    # Scrolling is painted natively; position bookkeeping must not flood the
    # Python event loop once per wheel packet.
    assert "scroll_interval=64" in source


def test_magnetic_header_cards_share_one_height_and_keyboard_settings_fill_the_workspace():
    source = Path(__file__).resolve().parents[1].joinpath("app_flet.py").read_text(
        encoding="utf-8"
    )

    # Profile, RTStab, protection and Snap Key are one visual row, not cards
    # with four independently changing heights.
    assert source.count("height=magnetic_header_card_height") == 4
    # Their content uses one common three-baseline template too: title,
    # explanation and control do not drift merely because a dropdown has a
    # different intrinsic height than a switch or a button.
    assert "def magnetic_header_body(" in source
    assert source.count("content=magnetic_header_body(") == 4
    assert "magnetic_header_copy_height = 34" in source
    assert "magnetic_header_control_height = 52" in source
    # Settings deliberately use a two-column responsive grid on desktop and
    # fall back to one column on a narrower non-maximized window.
    assert "ft.ResponsiveRow(" in source
    assert 'col={"sm": 12, "md": 6}' in source
    # The Settings card must inherit the section width instead of ending at a
    # fixed keyboard-deck-sized pixel value.
    assert "settings_card.width =" not in source
    workspace_source = source.split("keyboard_workspace = ft.Column(", 1)[1].split(
        "self.sniff_log", 1
    )[0]
    assert "horizontal_alignment=ft.CrossAxisAlignment.STRETCH" in workspace_source
    # Each preference tile has a fixed visual height, so the right-hand
    # dropdown cannot make only its row taller than the neighbouring switch.
    assert "height=82" in source
    assert 'label="Задержка, мс"' in source


def test_main_window_disables_native_maximize_and_full_screen():
    source = Path(__file__).resolve().parents[1].joinpath("app_flet.py").read_text(
        encoding="utf-8"
    )

    assert "self.page.window.maximizable = False" in source
    assert "self.page.window.maximized = False" in source
    assert "self.page.window.full_screen = False" in source
    assert "self.page.window.resizable = False" in source
    assert "self.page.window.min_width = 1360" in source
    assert "self.page.window.max_width = 1360" in source
    assert "self.page.window.min_height = 820" in source
    assert "self.page.window.max_height = 820" in source
    assert 'self.page.title = "QMK.Top Manager for SK75 TMR"' in source


def test_dropdowns_use_the_bounded_shared_native_material_menu_style():
    source = Path(__file__).resolve().parents[1].joinpath("app_flet.py").read_text(
        encoding="utf-8"
    )

    assert "def _app_dropdown" in source
    assert 'kwargs.setdefault("menu_height", 280)' in source
    assert "def _dropdown_menu_style" in source
    assert "return ft.Dropdown(**kwargs)" in source

    manager = QMKManager.__new__(QMKManager)
    dropdown = manager._app_dropdown(label="Проверка")
    assert dropdown.menu_height == 280
    assert isinstance(dropdown.menu_style, ft.MenuStyle)
