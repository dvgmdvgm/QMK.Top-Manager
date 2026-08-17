"""Rendering-cost regression tests for the Magnetic Lab controls."""

import os
import sys
import inspect

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import flet as ft

from app_flet import QMKManager


def test_vertical_m3_ruler_has_no_outer_implicit_animation_during_drag():
    """A ruler patch must not enqueue a long compositor animation every frame.

    The exact-step buttons retain their short Material state feedback, but the
    outer container is updated for each coalesced drag paint.  Giving that
    container a 160-ms implicit animation made several animations overlap while
    moving a ruler and caused unnecessary GPU activity.
    """
    manager = QMKManager.__new__(QMKManager)

    _value, state, control = manager._make_m3_vertical_magnetic_parameter_control(
        "Активация",
        "Точка срабатывания клавиши",
        120,
        10,
        330,
        320,
        "#40C4FF",
        ft.Icons.ADS_CLICK_ROUNDED,
        fills_from_top=True,
    )

    assert control.animate is None
    assert control.animate_opacity is None
    # Button feedback remains intentionally Material-like and local.
    assert state.decrease_button.style.animation_duration == 140
    assert state.increase_button.style.animation_duration == 140


def test_idle_magnetic_lab_does_not_start_a_render_loop_or_full_page_patch():
    """Opening Magnetic Lab must not itself schedule a repeating frame task.

    The live travel-test and calibration painters are allowed only after their
    respective explicit Start actions.  The normal Magnetic Lab and foreground
    scanner must stay idle from Flet's perspective, otherwise static ruler
    gradients are needlessly composited over and over.
    """
    magnetic_lab_source = inspect.getsource(QMKManager._build_magnetic_lab_card)
    scanner_source = inspect.getsource(QMKManager.background_task)

    assert "page.run_task" not in magnetic_lab_source
    assert "page.update" not in scanner_source


def test_active_vertical_ruler_factory_has_no_outer_implicit_animation():
    """The currently mounted compact rulers are compositor-idle at rest."""
    source = inspect.getsource(QMKManager._make_vertical_magnetic_control)

    assert "animate=ft.Animation" not in source
    assert "animate_opacity=ft.Animation" not in source
