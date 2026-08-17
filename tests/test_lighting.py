from types import SimpleNamespace

import flet as ft
import pytest
from PIL import Image

import app_flet as app_module
from app_flet import QMKManager
from lighting import (
    LightingProtocolError,
    LightingSettings,
    NEUTRAL_LIGHTING_BRIGHTNESS,
    NEUTRAL_LIGHTING_COLOR,
    NEUTRAL_LIGHTING_COLOR_HEX,
    NEUTRAL_LIGHTING_EFFECT,
    NEUTRAL_LIGHTING_SPEED,
    RongyuanLightingProtocol,
    hsv_degrees_to_rgb,
    parse_hex_color,
    picker_position_to_sv,
    rgb_to_hsv_degrees,
)


def test_fresh_lighting_defaults_are_neutral_ui_fallbacks():
    """A public clean install must never start from the publisher's RGB choice."""
    settings = LightingSettings()

    assert settings.effect == NEUTRAL_LIGHTING_EFFECT == 1
    assert settings.color == NEUTRAL_LIGHTING_COLOR == (255, 255, 255)
    assert settings.to_config()["color"] == NEUTRAL_LIGHTING_COLOR_HEX == "#FFFFFF"
    assert settings.brightness == NEUTRAL_LIGHTING_BRIGHTNESS == 2
    assert settings.speed == NEUTRAL_LIGHTING_SPEED == 2


def test_empty_or_invalid_lighting_config_uses_the_same_neutral_fallback():
    """Missing legacy values cannot reintroduce a personal colour on startup."""
    assert LightingSettings.from_config({}) == LightingSettings()
    assert LightingSettings.from_config({"color": "not-a-colour"}) == LightingSettings()


def test_new_device_entry_is_neutral_and_does_not_send_a_lighting_packet():
    """Creating local state is storage-only until the user explicitly applies RGB."""
    manager = QMKManager.__new__(QMKManager)
    manager.config = {"devices": {}}
    manager._device_key_of = lambda _hid_dev: "3151:5030:ffff"
    manager._device_label_for = lambda _hid_dev: "SK75 TMR"
    manager._detect_transport = lambda _hid_dev: "wired"
    saves = []
    manager.save_config = lambda **kwargs: saves.append(kwargs)
    manager._send_lighting_packets = lambda *_args, **_kwargs: pytest.fail(
        "fresh-device creation must not write RGB to HID"
    )

    key = manager._ensure_device_entry(
        {"vendor_id": 0x3151, "product_id": 0x5030, "usage_page": 0xFFFF}
    )

    assert key == "3151:5030:ffff"
    assert manager.config["devices"][key]["lighting_lab"] == LightingSettings().to_config()
    assert saves == [{}]


def test_readback_replaces_the_neutral_preview_with_real_keyboard_state_without_write(monkeypatch):
    """A connected SK75 remains the authority for its existing RGB setting."""
    actual = LightingSettings(effect=6, color=(18, 52, 86), brightness=3, speed=1)
    manager = QMKManager.__new__(QMKManager)
    saved = []
    synchronized = []
    manager._query_lighting_settings = lambda: actual
    manager._save_lighting_lab_settings = saved.append
    manager._sync_lighting_controls_from_settings = synchronized.append
    manager._send_lighting_packets = lambda *_args, **_kwargs: pytest.fail(
        "a startup lighting readback must not write to HID"
    )

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(app_module.threading, "Thread", ImmediateThread)

    manager._read_lighting_settings_from_keyboard(silent=True)

    assert saved == [actual]
    assert synchronized == [actual]


def test_settings_packet_uses_bit8_checksum_and_selected_rgb():
    packet = RongyuanLightingProtocol.settings_packet(
        LightingSettings(effect=4, color=(18, 52, 86), brightness=3, speed=2, option=1)
    )

    assert packet[:8] == [0x07, 4, 2, 3, 0x17, 18, 52, 86]
    assert packet[8] == (255 - sum(packet[:8])) & 0xFF
    assert packet[9:] == [0] * 55


def test_visual_colour_picker_maps_mouse_coordinates_and_hsv_without_overflow():
    hue, saturation, value = rgb_to_hsv_degrees((18, 52, 86))

    assert round(hue) == 210
    assert round(saturation, 3) == 0.791
    assert round(value, 3) == 0.337
    assert hsv_degrees_to_rgb(hue, saturation, value) == (18, 52, 86)
    assert picker_position_to_sv(0, 0, 200, 100) == (0.0, 1.0)
    assert picker_position_to_sv(200, 100, 200, 100) == (1.0, 0.0)
    # Pointer coordinates slightly outside the square are normal while the
    # user drags across the edge; the picker must remain bounded.
    assert picker_position_to_sv(-20, 130, 200, 100) == (0.0, 0.0)


def test_visual_colour_picker_constructs_with_flet_085_controls():
    """Protect the click-to-pick flow against unsupported Flet APIs."""
    class Page:
        dialog = None

        def show_dialog(self, dialog):
            self.dialog = dialog

        def pop_dialog(self):
            self.dialog = None

        def update(self):
            pass

    manager = QMKManager.__new__(QMKManager)
    manager.page = Page()
    manager.lighting_color_format_dropdown = SimpleNamespace(value="hex")
    manager.lighting_primary_color = ft.TextField(value="#123456")
    manager.lighting_primary_rgb_fields = [
        ft.TextField(value=str(channel)) for channel in (18, 52, 86)
    ]
    manager.lighting_color_preview = ft.Container()
    manager.lighting_color_value = ft.Text()

    manager._open_lighting_color_picker()

    assert manager.page.dialog is not None
    assert manager.page.dialog.title.value == "Выбор цвета"
    assert [action.content for action in manager.page.dialog.actions] == ["Отмена", "Готово"]


def test_picker_defers_parent_colour_update_until_confirmed():
    """Dragging hue must not echo changes into the page behind the dialog."""
    class Page:
        dialog = None

        def show_dialog(self, dialog):
            self.dialog = dialog

        def pop_dialog(self):
            self.dialog = None

        def update(self):
            raise AssertionError("the HSV drag must not request a full-page update")

    manager = QMKManager.__new__(QMKManager)
    manager.page = Page()
    manager.lighting_color_format_dropdown = SimpleNamespace(value="hex")
    manager.lighting_primary_color = ft.TextField(value="#123456")
    manager.lighting_primary_rgb_fields = [
        ft.TextField(value=str(channel)) for channel in (18, 52, 86)
    ]
    manager.lighting_color_preview = ft.Container()
    manager.lighting_color_value = ft.Text()

    manager._open_lighting_color_picker()
    dialog = manager.page.dialog
    hue_slider = dialog.content.content.controls[1]
    hue_slider.on_change(SimpleNamespace(control=SimpleNamespace(value="10")))

    # The dialog preview changes locally, but the main Lighting Lab does not
    # churn controls until the user explicitly accepts the choice.
    assert manager.lighting_primary_color.value == "#123456"
    dialog.actions[1].on_click(SimpleNamespace())
    assert manager.lighting_primary_color.value != "#123456"


def test_picker_throttles_drag_events_and_cancels_late_callbacks():
    """A busy colour plane must not keep Cancel waiting behind stale events."""
    class Page:
        dialog = None
        pop_calls = 0

        def show_dialog(self, dialog):
            self.dialog = dialog

        def pop_dialog(self):
            self.pop_calls += 1
            self.dialog = None

    manager = QMKManager.__new__(QMKManager)
    manager.page = Page()
    manager.lighting_color_format_dropdown = SimpleNamespace(value="hex")
    manager.lighting_primary_color = ft.TextField(value="#123456")
    manager.lighting_primary_rgb_fields = [
        ft.TextField(value=str(channel)) for channel in (18, 52, 86)
    ]
    manager.lighting_color_preview = ft.Container()
    manager.lighting_color_value = ft.Text()

    manager._open_lighting_color_picker()
    dialog = manager.page.dialog
    picker_live_region, hue_slider = dialog.content.content.controls
    sv_plane = picker_live_region.controls[1]

    # The native gesture detector is the first line of defence against a
    # backlog of drag events. The final position is still rendered on pan end.
    assert sv_plane.drag_interval == 72
    assert hue_slider.on_change is not None
    assert sv_plane.on_pan_update is not None

    dialog.actions[0].on_click(SimpleNamespace())

    assert manager.page.pop_calls == 1
    assert hue_slider.on_change is None
    assert hue_slider.on_change_end is None
    assert sv_plane.on_pan_update is None
    assert sv_plane.on_pan_end is None


def test_opening_new_picker_retires_the_previous_dialog_first():
    """Rapid repeated clicks cannot accumulate hidden colour-picker dialogs."""
    class Page:
        dialog = None
        pop_calls = 0

        def show_dialog(self, dialog):
            self.dialog = dialog

        def pop_dialog(self):
            self.pop_calls += 1
            self.dialog = None

    manager = QMKManager.__new__(QMKManager)
    manager.page = Page()
    manager.lighting_color_format_dropdown = SimpleNamespace(value="hex")
    manager.lighting_primary_color = ft.TextField(value="#123456")
    manager.lighting_primary_rgb_fields = [
        ft.TextField(value=str(channel)) for channel in (18, 52, 86)
    ]
    manager.lighting_color_preview = ft.Container()
    manager.lighting_color_value = ft.Text()

    manager._open_lighting_color_picker()
    first_dialog = manager.page.dialog
    manager._open_lighting_color_picker()

    assert manager.page.pop_calls == 1
    assert manager.page.dialog is not first_dialog
    assert manager._lighting_color_picker_close is not None


def test_lighting_card_uses_the_wide_desktop_area_for_a_preview():
    manager = QMKManager.__new__(QMKManager)
    manager._active_device = lambda: {"lighting_lab": {}}

    card = manager._build_lighting_lab_card()

    # The controls and the visual preview share a ResponsiveRow instead of
    # leaving the desktop card's right half empty.
    assert isinstance(card.content.controls[-1].content, ft.ResponsiveRow)
    assert len(manager.lighting_preview_tiles) == 31
    assert manager.lighting_preview_surface.height == 282
    assert manager.lighting_hex_copy_button.tooltip == "Скопировать HEX"
    assert manager.lighting_rgb_copy_button.tooltip == "Скопировать RGB"
    # RGB copy stays directly beside the three compact channels, not wrapped
    # onto a second line below them.
    assert manager.lighting_rgb_colors_row.wrap is False
    assert manager.lighting_rgb_colors_row.width == 254
    assert manager.lighting_rgb_colors_row.controls[-1] is manager.lighting_rgb_copy_halo
    # HEX and RGB share one fixed value/copy slot.  Switching formats must
    # never make the R/G/B inputs drop under the format dropdown or move the
    # copy action to a new row.
    assert manager.lighting_primary_color.width == 202
    assert manager.lighting_rgb_colors_row.controls[0] is manager.lighting_rgb_fields_row
    assert manager.lighting_color_header_row.width == 444
    assert manager.lighting_color_header_row.wrap is False
    assert [field.width for field in manager.lighting_primary_rgb_fields] == [64, 64, 64]
    assert manager.lighting_color_preview.shadow is not None
    assert manager.lighting_apply_glow.shadow is not None
    # The swatch now precedes the switch, making the colour/action grouping
    # read left-to-right without a duplicated colour summary.
    assert manager.lighting_custom_color_mode_row.controls[:2] == [
        manager.lighting_color_preview,
        manager.lighting_custom_color_switch,
    ]
    # The actual editor is one compact row: format -> value -> copy.  Both
    # representations remain mounted so changing the format cannot discard
    # the current colour or move the copy affordance elsewhere.
    assert manager.lighting_color_header_row.controls == [
        manager.lighting_color_format_dropdown,
        manager.lighting_hex_colors_row,
        manager.lighting_rgb_colors_row,
    ]
    assert manager.lighting_hex_colors_row.controls == [
        manager.lighting_primary_color,
        manager.lighting_hex_copy_halo,
    ]

    manager.lighting_custom_color_switch.value = False
    manager._refresh_lighting_custom_color_swatch(update=False)
    assert manager.lighting_color_preview.bgcolor == ft.Colors.SURFACE_CONTAINER_HIGHEST
    assert manager.lighting_color_preview.shadow is None
    assert manager.lighting_color_preview.on_click is None


def test_lighting_shows_only_the_selected_hex_or_rgb_editor_after_effect_refresh():
    """Changing an effect must not reveal the inactive colour editor again."""
    manager = QMKManager.__new__(QMKManager)
    manager._active_device = lambda: {"lighting_lab": {}}

    manager._build_lighting_lab_card()

    assert manager.lighting_hex_colors_row.visible is True
    assert manager.lighting_rgb_colors_row.visible is False

    manager.lighting_color_format_dropdown.value = "rgb"
    manager.lighting_color_format_dropdown.on_select(
        SimpleNamespace(control=manager.lighting_color_format_dropdown)
    )
    assert manager.lighting_hex_colors_row.visible is False
    assert manager.lighting_rgb_colors_row.visible is True
    assert manager.lighting_primary_color.value == "#FFFFFF"
    assert [field.value for field in manager.lighting_primary_rgb_fields] == ["255", "255", "255"]

    # Effect selection calls the same capability refresh that previously
    # forced both rows visible. The RGB preference must be preserved.
    manager._refresh_lighting_color_capability(
        int(manager.lighting_effect_dropdown.value), update_controls=False
    )
    assert manager.lighting_hex_colors_row.visible is False
    assert manager.lighting_rgb_colors_row.visible is True

    manager.lighting_color_format_dropdown.value = "hex"
    manager.lighting_color_format_dropdown.on_select(
        SimpleNamespace(control=manager.lighting_color_format_dropdown)
    )
    assert manager.lighting_hex_colors_row.visible is True
    assert manager.lighting_rgb_colors_row.visible is False


def test_lighting_preview_shows_firmware_palette_not_selected_hex_in_dazzle_mode():
    """The actual Womier palette mode must not be drawn as a fake solid colour."""
    manager = QMKManager.__new__(QMKManager)
    manager._active_device = lambda: {
        "lighting_lab": LightingSettings(
            effect=3, color=(255, 148, 148), brightness=4, speed=2, rainbow=True
        ).to_config()
    }

    manager._build_lighting_lab_card()

    assert manager.lighting_custom_color_switch.value is False
    assert manager.lighting_preview_color_text.value == "Палитра прошивки"
    assert "встроенный радужный эффект" in manager.lighting_preview_effect_text.value
    assert "встроенный радужный эффект Womier" in manager.lighting_preview_hint.value
    assert manager.lighting_color_header_row.visible is False


def test_lighting_color_copy_uses_native_clipboard_and_visible_format():
    """The compact copy button must use the same reliable Windows path as configs."""
    manager = QMKManager.__new__(QMKManager)
    manager.lighting_color_format_dropdown = SimpleNamespace(value="hex")
    manager.lighting_primary_color = ft.TextField(value="#123456")
    manager.lighting_primary_rgb_fields = [
        ft.TextField(value=str(channel)) for channel in (18, 52, 86)
    ]
    copied = []
    notices = []
    manager._set_system_clipboard_text = lambda value: copied.append(value) or True
    manager._snack = notices.append

    manager._copy_lighting_color()
    manager.lighting_color_format_dropdown.value = "rgb"
    manager._copy_lighting_color()

    assert copied == ["#123456", "rgb(18, 52, 86)"]
    assert notices == [
        "HEX скопирован: #123456",
        "RGB скопирован: rgb(18, 52, 86)",
    ]


def test_background_lighting_save_is_atomic_and_does_not_reload_input_runtime():
    """A worker-side lighting read must not restart global keyboard hooks."""
    manager = QMKManager.__new__(QMKManager)
    entry = {"lighting_lab": {}}
    manager.config = {"active_device": "sk75", "devices": {"sk75": entry}}
    saves = []
    manager.save_config = lambda **kwargs: saves.append(kwargs)

    manager._save_lighting_lab_settings(
        LightingSettings(effect=4, color=(18, 52, 86), brightness=3, speed=2)
    )

    assert entry["lighting_lab"] == {
        "effect": 4,
        "color": "#123456",
        "brightness": 3,
        "speed": 2,
        "option": 0,
        "rainbow": False,
    }
    assert saves == [{"reload_runtime": False}]


def test_decode_response_and_hex_validation():
    settings = RongyuanLightingProtocol.decode_settings([0, 0x87, 4, 2, 3, 0x17, 1, 2, 3])

    assert settings == LightingSettings(effect=4, color=(1, 2, 3), brightness=3, speed=2, option=1)
    assert parse_hex_color("#FF00aa") == (255, 0, 170)


def test_invalid_hex_color_is_rejected():
    try:
        parse_hex_color("blue")
    except LightingProtocolError:
        pass
    else:
        raise AssertionError("invalid color should fail")


def test_application_icon_prefers_the_bundled_keyboard_tile(tmp_path):
    """A packaged release must not silently fall back to Flet's default icon."""
    manager = QMKManager.__new__(QMKManager)
    icon = tmp_path / "assets" / "qmk-top-manager-keyboard.ico"
    icon.parent.mkdir()
    icon.write_bytes(b"ico")
    manager._resource_path = lambda _rel_path: str(icon)

    assert manager._resolve_application_icon_path() == str(icon)


def test_application_icon_never_depends_on_another_manager_installation(tmp_path):
    """A public build must not probe a hard-coded legacy EXE for its icon."""
    manager = QMKManager.__new__(QMKManager)
    manager._resource_path = lambda _rel_path: str(tmp_path / "missing.ico")

    assert manager._resolve_application_icon_path() is None


def test_extracted_qmk_icon_is_centered_on_its_taskbar_canvas():
    """The legacy colour bitmap has a top-biased alpha box; normalize it."""
    source = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    source.paste((205, 196, 214, 255), (2, 1, 31, 21))

    centered = QMKManager._center_icon_canvas(source)

    # Preserve the 29x20 keyboard glyph, centered to x=1/y=6 in the 32px
    # taskbar resource, rather than leaving the original y=1 top padding.
    assert centered.size == (32, 32)
    assert centered.getchannel("A").getbbox() == (1, 6, 30, 26)
