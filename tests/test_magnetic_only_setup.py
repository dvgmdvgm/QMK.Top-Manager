from types import SimpleNamespace

import flet as ft

from app_flet import QMKManager


def _walk_controls(control):
    if isinstance(control, (str, bytes)) or control is None:
        return
    if isinstance(control, (list, tuple)):
        for item in control:
            yield from _walk_controls(item)
        return

    yield control
    for attribute in ("content", "controls", "actions"):
        child = getattr(control, attribute, None)
        if child is not None:
            yield from _walk_controls(child)


def test_first_run_setup_is_magnetic_only_and_confirms_magnetic_mode():
    """New devices must not offer a mechanical mode in the SK75 companion."""

    class Page:
        dialog = None
        updates = 0

        def show_dialog(self, dialog):
            self.dialog = dialog

        def pop_dialog(self):
            self.dialog = None

        def update(self):
            self.updates += 1

    key = "0x1234:0x5678:1"
    manager = QMKManager.__new__(QMKManager)
    manager.page = Page()
    manager.config = {
        "active_device": key,
        "devices": {
            key: {
                "label": "SK75 TMR",
                "vid": 0x1234,
                "pid": 0x5678,
                "usage_page": 1,
                "keyboard_type": None,
            }
        },
    }
    configured = []
    manager._set_keyboard_type = lambda *args: configured.append(args)

    manager._show_setup_wizard(key)

    dialog = manager.page.dialog
    assert dialog is not None
    controls = list(_walk_controls(dialog))
    assert not any(isinstance(control, ft.Dropdown) for control in controls)
    text = " ".join(
        str(getattr(control, "value", "")) + " " + str(getattr(control, "label", ""))
        for control in controls
    ).casefold()
    assert "магнит" in text
    assert "механ" not in text

    confirmation = next(control for control in controls if isinstance(control, ft.Checkbox))
    save_button = next(control for control in controls if isinstance(control, ft.ElevatedButton))
    assert save_button.disabled is True

    confirmation.value = True
    confirmation.on_change(SimpleNamespace(control=confirmation))
    assert save_button.disabled is False

    save_button.on_click(SimpleNamespace())
    assert configured == [(0x1234, 0x5678, 1, "magnetic")]

