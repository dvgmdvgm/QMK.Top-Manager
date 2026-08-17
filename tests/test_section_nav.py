"""Behaviour tests for the right-side workspace section switcher."""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app_flet import QMKManager


class _Panel:
    def __init__(self, visible=False):
        self.visible = visible
        self.opacity = 1.0 if visible else 0.0
        self.offset = None
        self.update_count = 0

    def update(self):
        self.update_count += 1


class _SectionContent:
    def __init__(self):
        self.update_count = 0

    def update(self):
        self.update_count += 1


class _MainScroll:
    def __init__(self):
        self.calls = []

    async def scroll_to(self, **kwargs):
        self.calls.append(kwargs)


class _NavButton:
    def __init__(self):
        self.focus_count = 0
        self.style = None

    async def focus(self):
        self.focus_count += 1


def _manager_with_sections():
    manager = QMKManager.__new__(QMKManager)
    manager.section_nav_order = ("keyboard", "lighting", "magnetic")
    manager.section_nav_active = "keyboard"
    manager._visible_section = "keyboard"
    manager._section_switch_revision = 0
    manager.section_panels = {
        "keyboard": _Panel(visible=True),
        "lighting": _Panel(),
        "magnetic": _Panel(),
    }
    manager.section_nav_buttons = {}
    manager.section_content = _SectionContent()
    manager.main_scroll = _MainScroll()
    manager._main_scroll_position = 64.0
    return manager


def test_section_switcher_keeps_only_the_requested_workspace_visible():
    manager = _manager_with_sections()

    asyncio.run(manager._show_section("lighting", animated=False))

    assert manager.section_nav_active == "lighting"
    assert manager._visible_section == "lighting"
    assert manager.section_panels["keyboard"].visible is False
    assert manager.section_panels["lighting"].visible is True
    assert manager.section_panels["magnetic"].visible is False
    assert manager.main_scroll.calls == [{"offset": 0, "duration": 0}]
    assert manager._main_scroll_position == 0.0


def test_scrolling_inside_one_workspace_does_not_change_the_selected_menu_item():
    manager = _manager_with_sections()
    manager.section_nav_active = "magnetic"
    manager._visible_section = "magnetic"

    asyncio.run(manager._on_main_scroll(SimpleNamespace(pixels=155.0)))

    assert manager._main_scroll_position == 155.0
    assert manager.section_nav_active == "magnetic"


def test_tab_focus_handler_switches_to_a_different_workspace_only_once():
    manager = _manager_with_sections()

    handler = manager._make_section_nav_focus_handler("magnetic")
    asyncio.run(handler(SimpleNamespace()))

    assert manager._visible_section == "magnetic"
    calls_after_first_focus = list(manager.main_scroll.calls)

    # Click/focus can both arrive for the same control.  The focus path must
    # not reset or reanimate the already-open workspace a second time.
    asyncio.run(handler(SimpleNamespace()))
    assert manager.main_scroll.calls == calls_after_first_focus


def test_tab_cycles_only_workspace_sections_and_wraps_from_last_to_first():
    manager = _manager_with_sections()
    manager.section_nav_buttons = {
        section: _NavButton() for section in manager.section_nav_order
    }

    tab = lambda **kwargs: SimpleNamespace(
        key="Tab", shift=False, ctrl=False, alt=False, meta=False, **kwargs
    )

    asyncio.run(manager._on_page_keyboard_event(tab()))
    assert manager._visible_section == "lighting"
    assert manager.section_nav_buttons["lighting"].focus_count == 1

    asyncio.run(manager._on_page_keyboard_event(tab()))
    assert manager._visible_section == "magnetic"
    assert manager.section_nav_buttons["magnetic"].focus_count == 1

    # The last section must wrap back to the first instead of leaving focus in
    # a profile or another form control.
    asyncio.run(manager._on_page_keyboard_event(tab()))
    assert manager._visible_section == "keyboard"
    assert manager.section_nav_buttons["keyboard"].focus_count == 1


def test_shift_tab_wraps_from_first_section_to_last():
    manager = _manager_with_sections()
    manager.section_nav_buttons = {
        section: _NavButton() for section in manager.section_nav_order
    }

    asyncio.run(
        manager._on_page_keyboard_event(
            SimpleNamespace(key="Tab", shift=True, ctrl=False, alt=False, meta=False)
        )
    )

    assert manager._visible_section == "magnetic"
    assert manager.section_nav_buttons["magnetic"].focus_count == 1


def test_modified_tab_does_not_steal_platform_shortcuts():
    manager = _manager_with_sections()
    manager.section_nav_buttons = {
        section: _NavButton() for section in manager.section_nav_order
    }

    asyncio.run(
        manager._on_page_keyboard_event(
            SimpleNamespace(key="Tab", shift=False, ctrl=True, alt=False, meta=False)
        )
    )

    assert manager._visible_section == "keyboard"
    assert all(button.focus_count == 0 for button in manager.section_nav_buttons.values())
