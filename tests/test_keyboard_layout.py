"""Layout invariants for the visual SK75 keyboard deck."""

import inspect
from types import SimpleNamespace

import flet as ft

from app_flet import (
    MAGNETIC_METRIC_COLORS,
    MAGNETIC_KEY_METRICS_EXPLANATION,
    MAGNETIC_SCALE_ROLE_LABELS,
    QMKManager,
    SK75_OFFICIAL_VISUAL_LAYOUT,
    SK75_VISUAL_KEY_SELECTED_BACKGROUND,
    _sk75_visual_deck_geometry,
    _sk75_visual_deck_width_for_viewport,
)
from magnetic import KeyMagneticSettings, SK75_KEYS


def _keyboard_layout_manager():
    """Provide only the magnetic read helpers needed by the pure UI builder."""
    manager = QMKManager.__new__(QMKManager)
    manager._magnetic_key_caption = lambda _slot: ("1.20", "RT 0.30/0.30")
    manager._magnetic_key_is_advanced = lambda _slot: False
    manager._magnetic_settings_for_keyboard = lambda _slot: None
    manager._magnetic_key_compact_caption = lambda _slot, _settings, _advanced: ""
    manager._magnetic_key_is_snap = lambda _slot: False
    return manager


def test_visual_sk75_geometry_keeps_all_rows_inside_one_case_width():
    geometry = _sk75_visual_deck_geometry()

    assert len(geometry.row_content_widths) == len(SK75_OFFICIAL_VISUAL_LAYOUT)
    assert all(width <= geometry.row_width for width in geometry.row_content_widths)
    # The populated navigation rows reach the right case edge.  The Up arrow
    # intentionally sits over Down instead of in the far-right PgDn column.
    assert all(edge == geometry.row_width for edge in geometry.row_right_edges[:4])
    assert geometry.row_right_edges[4] < geometry.row_width
    assert geometry.row_right_edges[5] == geometry.row_width
    # The top row has a real Delete after F12 and Home in the right column.
    assert geometry.cluster_breaks[0] == frozenset({0, 4, 8, 12, 13})


def test_visual_sk75_full_deck_scales_to_the_minimum_window_without_clipping():
    """The left rail and card insets must not cut off the right cluster."""
    deck_width = _sk75_visual_deck_width_for_viewport(1360)
    geometry = _sk75_visual_deck_geometry(deck_width=deck_width)

    # The minimum native window has less usable space than the old fixed
    # 1128 px board.  The board therefore scales down as a single physical
    # unit while the upper arrow still keeps its physical column over Down.
    assert 360 < deck_width < 1128
    assert all(edge == geometry.row_width for edge in geometry.row_right_edges[:4])
    assert geometry.row_right_edges[4] < geometry.row_width
    assert geometry.row_right_edges[5] == geometry.row_width
    assert all(
        left + width <= geometry.row_width
        for positions, widths in zip(geometry.key_x_positions, geometry.key_widths)
        for left, width in zip(positions, widths)
    )


def test_visual_sk75_resize_handler_rebuilds_only_when_the_fit_width_changes():
    manager = QMKManager.__new__(QMKManager)
    page = SimpleNamespace(width=1360, on_resize=None)
    manager.page = page
    manager.keyboard_picker_root = SimpleNamespace()
    manager._sk75_viewport_width = 1360
    manager._sk75_rendered_deck_width = _sk75_visual_deck_width_for_viewport(1360)
    refreshes = []
    manager._refresh_sk75_keyboard_picker = lambda: refreshes.append("deck")

    manager._install_sk75_keyboard_resize_handler()
    page.on_resize(SimpleNamespace(width=1360))
    assert refreshes == []

    page.on_resize(SimpleNamespace(width=1390))
    assert refreshes == ["deck"]


def test_visual_sk75_rows_use_positioned_physical_grid_instead_of_elastic_space():
    manager = _keyboard_layout_manager()
    deck = manager._build_sk75_keyboard_layout(lambda _slot: None, compact=True)
    geometry = _sk75_visual_deck_geometry(compact=True)

    rows = deck.content.controls
    assert len(rows) == len(SK75_OFFICIAL_VISUAL_LAYOUT)
    assert all(isinstance(row, ft.Stack) for row in rows)
    assert all(row.width == geometry.row_width for row in rows)
    assert all(row.height == 42 for row in rows)
    # Each first physical key starts at the left edge and each subsequent cap
    # gets its own absolute case-grid coordinate.  No row contains an
    # ``expand`` spacer that could create a giant centre void.
    for row_index, row in enumerate(rows):
        visual_keys = row.controls[:len(SK75_OFFICIAL_VISUAL_LAYOUT[row_index])]
        assert [key.left for key in visual_keys] == list(geometry.key_x_positions[row_index])
        assert all(key.expand is None for key in visual_keys)

    # The flat deck includes only physical SK75 switches: no decorative W
    # tile and no fake spacer key between F12, Delete and Home.
    assert len(rows[0].controls) == len(SK75_OFFICIAL_VISUAL_LAYOUT[0])


def test_visual_sk75_navigation_clusters_keep_up_over_down_without_overlap():
    """The 75% arrow cluster keeps Up directly above Down."""
    geometry = _sk75_visual_deck_geometry()

    assert geometry.right_cluster_starts == (14, 14, 14, 13, 12, 6)
    # ↑ shares Down's column rather than staying under PgDn, the far-right
    # navigation key on the row above it.
    assert geometry.key_x_units[4][-1] == geometry.key_x_units[5][-2] == 14.25
    assert (
        geometry.key_x_positions[4][-1]
        == geometry.key_x_positions[5][-2]
    )
    right_shift_index = next(
        index
        for index, (_slot, label, _width) in enumerate(SK75_OFFICIAL_VISUAL_LAYOUT[4])
        if label == "R Shift"
    )
    # R Shift ends before the vertically aligned ↑ cap begins.  This guards
    # against a visual overlap when the arrow moves from the PgDn column.
    assert (
        geometry.key_x_positions[4][right_shift_index]
        + geometry.key_widths[4][right_shift_index]
        <= geometry.key_x_positions[4][-1]
    )
    # The empty physical separation before the navigation cluster is at most
    # a little over one normal key pitch; it is never the large elastic void
    # from the former two-Row layout.
    assert all(
        offset - left <= round(geometry.key_pitch * 0.5)
        for left, offset in zip(geometry.left_cluster_widths, geometry.right_cluster_offsets)
    )
    # The visual selector now exposes every firmware-backed physical key.
    visual_slots = {slot for row in SK75_OFFICIAL_VISUAL_LAYOUT for slot, _label, _width in row}
    assert visual_slots == {key.slot for key in SK75_KEYS}
    assert [len(row) for row in SK75_OFFICIAL_VISUAL_LAYOUT] == [15, 15, 15, 14, 13, 9]
    assert [row[-1][1] for row in SK75_OFFICIAL_VISUAL_LAYOUT[:5]] == [
        "Home", "End", "PgUp", "PgDn", "↑"
    ]
    assert SK75_OFFICIAL_VISUAL_LAYOUT[0][-2][1] == "Del"


def test_visual_sk75_renderer_keeps_key_widths_and_positions_inside_case():
    manager = _keyboard_layout_manager()
    deck = manager._build_sk75_keyboard_layout(lambda _slot: None, compact=True)
    geometry = _sk75_visual_deck_geometry(compact=True)

    for row_index, row in enumerate(deck.content.controls):
        visual_keys = row.controls[:len(SK75_OFFICIAL_VISUAL_LAYOUT[row_index])]
        widths = geometry.key_widths[row_index]
        assert all(
            key.left + width <= geometry.row_width
            for key, width in zip(visual_keys, widths)
        )
        right_edge = max(key.left + width for key, width in zip(visual_keys, widths))
        if row_index == 4:
            # ↑ is intentionally over ↓, not in PgDn's far-right column.
            assert right_edge < geometry.row_width
        else:
            assert right_edge == geometry.row_width


def test_visual_sk75_snap_key_keeps_coloured_corner_magnetic_values():
    manager = _keyboard_layout_manager()
    settings = KeyMagneticSettings(
        actuation=1.20,
        rapid_trigger=True,
        rapid_press=0.30,
        rapid_release=0.45,
        lower_dead_zone=0.10,
        upper_dead_zone=0.10,
    )
    snap_slot = SK75_OFFICIAL_VISUAL_LAYOUT[0][0][0]
    manager._magnetic_settings_for_keyboard = lambda _slot: settings
    manager._magnetic_key_is_snap = lambda slot: slot == snap_slot

    deck = manager._build_sk75_keyboard_layout(lambda _slot: None)
    # The full layout has a legend first; the first cap remains Esc.
    esc_keycap = deck.content.controls[1].controls[0]
    corner_values = {
        (control.left, control.top, control.right, control.bottom): control.content.value
        for control in esc_keycap.content.controls
        if isinstance(control.content, ft.Text)
    }

    assert corner_values[(3, 3, None, None)] == "1.20"
    assert corner_values[(None, 3, 3, None)] == "0.45"
    assert corner_values[(None, None, 3, 3)] == "0.30"

    # Containers span a keycap's interior so long keys retain true outer
    # corners.  Their own alignment must still pin the text, otherwise the
    # top-left activation and top-right release metrics overlap in the centre.
    metric_by_value = {
        control.content.value: control
        for control in esc_keycap.content.controls
        if isinstance(control.content, ft.Text)
    }
    assert metric_by_value["1.20"].alignment == ft.Alignment.CENTER_LEFT
    assert metric_by_value["0.45"].alignment == ft.Alignment.CENTER_RIGHT
    assert metric_by_value["0.30"].alignment == ft.Alignment.CENTER_RIGHT

    metric_badges = [
        control
        for control in esc_keycap.content.controls
        if isinstance(control.content, ft.Text) and control.content.value in {"1.20", "0.30", "0.45"}
    ]
    assert all(badge.bgcolor is None and badge.border is None for badge in metric_badges)
    assert {badge.content.color for badge in metric_badges} == set(
        MAGNETIC_METRIC_COLORS.values()
    )
    assert all(badge.content.size == 9 for badge in metric_badges)
    assert all(badge.content.weight == ft.FontWeight.W_800 for badge in metric_badges)


def test_full_keycap_metrics_keep_complete_numbers_at_the_true_outer_corners():
    """Long physical keys must not pull their values toward their centre."""
    manager = _keyboard_layout_manager()
    settings = KeyMagneticSettings(
        actuation=1.20,
        rapid_trigger=True,
        rapid_press=0.30,
        rapid_release=0.45,
        lower_dead_zone=0.10,
        upper_dead_zone=0.10,
    )
    manager._magnetic_settings_for_keyboard = lambda _slot: settings

    deck = manager._build_sk75_keyboard_layout(lambda _slot: None)
    geometry = _sk75_visual_deck_geometry()
    labels_to_check = {"Back", "Enter", "Shift", "R Shift", "Space"}
    found = set()
    for row_index, layout_row in enumerate(SK75_OFFICIAL_VISUAL_LAYOUT):
        row = deck.content.controls[row_index + 1]  # legend occupies index 0
        for key_index, (_slot, label, _units) in enumerate(layout_row):
            if label not in labels_to_check:
                continue
            keycap = row.controls[key_index]
            metrics = {
                control.content.value: control
                for control in keycap.content.controls
                if isinstance(control.content, ft.Text)
            }
            key_width = geometry.key_widths[row_index][key_index]
            assert metrics["1.20"].left == 3
            assert metrics["1.20"].width == key_width - 6
            assert metrics["0.30"].right == 3
            assert metrics["0.30"].width == key_width - 6
            assert metrics["0.45"].right == 3
            assert metrics["0.45"].width == key_width - 6
            found.add(label)

    assert found == labels_to_check


def test_every_full_size_keycap_shows_exactly_three_plain_live_metrics():
    """Normal mode replaces RT values with real deactivation, not stale data."""
    manager = _keyboard_layout_manager()
    settings = KeyMagneticSettings(
        actuation=1.20,
        rapid_trigger=False,
        # A non-separate RT keeps press synchronized with release.  The
        # renderer must still expose the third corner, not hide it.
        rapid_press=0.45,
        rapid_release=0.45,
        lower_dead_zone=0.10,
        upper_dead_zone=0.10,
        deactivation=0.85,
    )
    manager._magnetic_settings_for_keyboard = lambda _slot: settings

    deck = manager._build_sk75_keyboard_layout(lambda _slot: None)
    expected_positions = {
        (3, 3, None, None),
        (None, 3, 3, None),
        (None, None, 3, 3),
    }
    for row in deck.content.controls[1:]:  # first item is the legend
        for keycap in row.controls:
            metrics = [
                control
                for control in keycap.content.controls
                if isinstance(control.content, ft.Text)
            ]
            assert len(metrics) == 3
            assert {
                (metric.left, metric.top, metric.right, metric.bottom)
                for metric in metrics
            } == expected_positions
            values_by_corner = {
                (metric.left, metric.top, metric.right, metric.bottom): metric.content
                for metric in metrics
            }
            assert values_by_corner[(3, 3, None, None)].value == "1.20"
            assert values_by_corner[(None, 3, 3, None)].value == "0.85"
            assert values_by_corner[(None, None, 3, 3)].value == "—"
            assert values_by_corner[(3, 3, None, None)].color == MAGNETIC_METRIC_COLORS["actuation"]
            assert values_by_corner[(None, 3, 3, None)].color == MAGNETIC_METRIC_COLORS["rapid_release"]
            assert values_by_corner[(None, None, 3, 3)].color == ft.Colors.OUTLINE_VARIANT


def test_magnetic_corner_metrics_keep_leading_zero_and_no_actuation_prefix():
    settings = KeyMagneticSettings(
        actuation=0.20,
        rapid_trigger=True,
        rapid_press=0.30,
        rapid_release=0.05,
        lower_dead_zone=0.10,
        upper_dead_zone=0.10,
    )

    assert QMKManager._magnetic_key_corner_metrics(settings) == (
        ("top_left", "0.20", MAGNETIC_METRIC_COLORS["actuation"]),
        ("top_right", "0.05", MAGNETIC_METRIC_COLORS["rapid_release"]),
        ("bottom_right", "0.30", MAGNETIC_METRIC_COLORS["rapid_press"]),
    )


def test_normal_mode_corner_metrics_show_actual_deactivation_and_neutral_no_rt_marker():
    settings = KeyMagneticSettings(
        actuation=1.20,
        rapid_trigger=False,
        rapid_press=0.30,
        rapid_release=0.45,
        lower_dead_zone=0.10,
        upper_dead_zone=0.10,
        deactivation=0.85,
    )

    assert QMKManager._magnetic_key_corner_metrics(settings) == (
        ("top_left", "1.20", MAGNETIC_METRIC_COLORS["actuation"]),
        ("top_right", "0.85", MAGNETIC_METRIC_COLORS["rapid_release"]),
        ("bottom_right", "—", ft.Colors.OUTLINE_VARIANT),
    )
    assert QMKManager._magnetic_key_tooltip("Esc", settings) == (
        "Esc: точка активации 1.20 мм; точка деактивации 0.85 мм; "
        "Rapid Trigger выключен"
    )


def test_keycap_patch_replaces_rt_values_with_normal_deactivation_live():
    """Turning RT off updates a mounted keycap without rebuilding the deck."""
    manager = _keyboard_layout_manager()
    rapid_settings = KeyMagneticSettings(
        actuation=1.20,
        rapid_trigger=True,
        rapid_press=0.30,
        rapid_release=0.45,
        lower_dead_zone=0.10,
        upper_dead_zone=0.10,
        deactivation=0.85,
    )
    normal_settings = KeyMagneticSettings(
        actuation=1.20,
        rapid_trigger=False,
        rapid_press=0.30,
        rapid_release=0.45,
        lower_dead_zone=0.10,
        upper_dead_zone=0.10,
        deactivation=0.85,
    )
    slot = SK75_OFFICIAL_VISUAL_LAYOUT[0][0][0]
    manager._magnetic_settings_for_keyboard = lambda _slot: rapid_settings
    manager._build_sk75_keyboard_layout(
        lambda _slot: None, capture_magnetic_keycaps=True
    )

    assert manager._patch_magnetic_picker_keycap(
        slot, selected=False, settings=normal_settings
    )

    reference = manager._magnetic_picker_keycaps[slot]
    assert reference.metric_controls["top_left"][1].value == "1.20"
    assert reference.metric_controls["top_right"][1].value == "0.85"
    assert reference.metric_controls["bottom_right"][1].value == "—"
    assert reference.metric_controls["top_right"][1].color == MAGNETIC_METRIC_COLORS["rapid_release"]
    assert reference.metric_controls["bottom_right"][1].color == ft.Colors.OUTLINE_VARIANT
    assert "точка деактивации 0.85 мм" in reference.keycap.tooltip
    assert "Rapid Trigger выключен" in reference.keycap.tooltip


def test_keycap_metric_edit_patches_only_the_changed_corner_text_leaf():
    """A 0.01-mm ruler step must not diff the entire selected keycap."""
    manager = _keyboard_layout_manager()
    original = KeyMagneticSettings(
        actuation=1.20,
        rapid_trigger=True,
        rapid_press=0.30,
        rapid_release=0.45,
        lower_dead_zone=0.10,
        upper_dead_zone=0.10,
    )
    changed = KeyMagneticSettings(
        actuation=1.21,
        rapid_trigger=True,
        rapid_press=0.30,
        rapid_release=0.45,
        lower_dead_zone=0.10,
        upper_dead_zone=0.10,
    )
    slot = SK75_OFFICIAL_VISUAL_LAYOUT[0][0][0]
    manager._magnetic_settings_for_keyboard = lambda _slot: original
    manager._build_sk75_keyboard_layout(
        lambda _slot: None, capture_magnetic_keycaps=True
    )
    reference = manager._magnetic_picker_keycaps[slot]
    calls = []
    reference.keycap.update = lambda: calls.append("outer")
    for corner, (_badge, value_text) in reference.metric_controls.items():
        value_text.update = lambda corner=corner: calls.append(corner)

    assert manager._patch_magnetic_picker_keycap(
        slot, selected=False, settings=changed
    )

    assert reference.metric_controls["top_left"][1].value == "1.21"
    assert calls == ["top_left"]


def test_keycap_metric_leaf_has_outer_cap_fallback_when_flet_is_settling():
    """A failed metric-leaf patch cannot leave a live value visually stale."""
    manager = _keyboard_layout_manager()
    original = KeyMagneticSettings(
        actuation=1.20,
        rapid_trigger=True,
        rapid_press=0.30,
        rapid_release=0.45,
        lower_dead_zone=0.10,
        upper_dead_zone=0.10,
    )
    changed = KeyMagneticSettings(
        actuation=1.21,
        rapid_trigger=True,
        rapid_press=0.30,
        rapid_release=0.45,
        lower_dead_zone=0.10,
        upper_dead_zone=0.10,
    )
    slot = SK75_OFFICIAL_VISUAL_LAYOUT[0][0][0]
    manager._magnetic_settings_for_keyboard = lambda _slot: original
    manager._build_sk75_keyboard_layout(
        lambda _slot: None, capture_magnetic_keycaps=True
    )
    reference = manager._magnetic_picker_keycaps[slot]
    calls = []
    reference.metric_controls["top_left"][1].update = lambda: (_ for _ in ()).throw(
        RuntimeError("leaf is settling")
    )
    reference.keycap.update = lambda: calls.append("outer")

    assert manager._patch_magnetic_picker_keycap(
        slot, selected=False, settings=changed
    )

    assert calls == ["outer"]


def test_magnetic_key_metrics_guide_explains_every_corner_and_dead_zones():
    """The compact guide below the deck makes every coloured number clear."""
    assert "слева сверху — точка активации (голубой)" in MAGNETIC_KEY_METRICS_EXPLANATION
    assert "Rapid Trigger справа сверху — RT при отпускании (жёлтый)" in MAGNETIC_KEY_METRICS_EXPLANATION
    assert "Rapid Trigger справа сверху — точка деактивации (жёлтый)" in MAGNETIC_KEY_METRICS_EXPLANATION
    assert "справа снизу — прочерк: второго RT-порога нет" in MAGNETIC_KEY_METRICS_EXPLANATION
    assert "шкала при отпускании задаёт сброс" in MAGNETIC_KEY_METRICS_EXPLANATION
    assert "Дез-зоны сверху и снизу — запас" in MAGNETIC_KEY_METRICS_EXPLANATION
    assert "Калибров" not in MAGNETIC_KEY_METRICS_EXPLANATION


def test_magnetic_scale_headers_use_the_same_unambiguous_motion_terms():
    """The controls below the deck name the exact matching corner metric."""
    assert MAGNETIC_SCALE_ROLE_LABELS == {
        "actuation": "Активация",
        "rapid_release": "RT при\nотпускании",
        "rapid_press": "RT при повторном\nнажатии вниз",
        "lower_dead_zone": "Дез-зона\nснизу",
        "upper_dead_zone": "Дез-зона\nсверху",
    }


def test_visual_sk75_uses_flat_womier_driver_caps_without_a_case_or_bands():
    """The selector mirrors the flat official driver, not a 3D mock keyboard."""
    manager = _keyboard_layout_manager()
    settings = KeyMagneticSettings(
        actuation=0.30,
        rapid_trigger=True,
        rapid_press=0.20,
        rapid_release=0.20,
        lower_dead_zone=0.10,
        upper_dead_zone=0.10,
    )
    manager._magnetic_settings_for_keyboard = lambda _slot: settings

    deck = manager._build_sk75_keyboard_layout(lambda _slot: None)
    assert deck.gradient is None
    assert deck.border is None
    assert deck.shadow is None

    # The first row follows the one-line legend in the full deck.  There is no
    # lower activation band or lower-left status dot behind the real values.
    esc_keycap = deck.content.controls[1].controls[0]
    assert esc_keycap.gradient is None
    assert esc_keycap.shadow is None
    assert all(
        not (
            isinstance(control, ft.Container)
            and control.left == 4
            and control.bottom == 4
        )
        for control in esc_keycap.content.controls
    )


def test_magnetic_picker_captures_keycaps_for_a_two_key_selection_patch():
    manager = _keyboard_layout_manager()
    selected_slot = SK75_OFFICIAL_VISUAL_LAYOUT[0][0][0]
    next_slot = SK75_OFFICIAL_VISUAL_LAYOUT[0][1][0]

    manager._build_sk75_keyboard_layout(
        lambda _slot: None,
        {selected_slot: (ft.Colors.PRIMARY_CONTAINER, ft.Colors.ON_PRIMARY_CONTAINER)},
        capture_magnetic_keycaps=True,
    )

    assert set(manager._magnetic_picker_keycaps) == {key.slot for key in SK75_KEYS}
    assert manager._patch_magnetic_picker_keycap(selected_slot, selected=False)
    assert manager._patch_magnetic_picker_keycap(next_slot, selected=True)
    assert (
        manager._magnetic_picker_keycaps[next_slot].keycap.bgcolor
        == SK75_VISUAL_KEY_SELECTED_BACKGROUND
    )


def test_magnetic_picker_starts_with_no_visually_selected_key():
    """The cached Q target must not look like a user selection on first paint."""
    manager = QMKManager.__new__(QMKManager)
    manager.magnetic_selected_slot = next(key.slot for key in SK75_KEYS if key.hid == 20)
    manager.magnetic_visual_selected_slot = None
    manager._sk75_deck_width_for_current_viewport = lambda: 900
    manager.keyboard_picker_root = SimpleNamespace(content=None, update=lambda: None)
    captured = {}
    manager._build_sk75_keyboard_layout = lambda _click, selected_colors, **_kwargs: captured.setdefault(
        "selected_colors", selected_colors
    )
    manager._update_magnetic_rt_label = lambda: None
    manager._refresh_snap_key_summary = lambda update=False: None

    manager._refresh_sk75_keyboard_picker()

    assert captured["selected_colors"] == {}


def test_first_click_on_cached_q_target_creates_visual_selection():
    """No initial highlight must not make Q impossible to select afterwards."""
    manager = QMKManager.__new__(QMKManager)
    q_slot = next(key.slot for key in SK75_KEYS if key.hid == 20)
    manager.magnetic_selected_slot = q_slot
    manager.magnetic_visual_selected_slot = None
    manager._magnetic_settings_from_controls = lambda: None
    manager._load_magnetic_controls = lambda _slot, update=True: None
    manager._magnetic_settings_for_keyboard = lambda _slot: None
    paints = []
    manager._patch_magnetic_picker_keycap = lambda slot, **kwargs: (
        paints.append((slot, kwargs)) or True
    )
    manager._refresh_sk75_keyboard_picker = lambda: (_ for _ in ()).throw(
        AssertionError("two local repaint patches should be enough")
    )

    manager._select_sk75_key(q_slot)

    assert manager.magnetic_visual_selected_slot == q_slot
    assert paints[-1] == (q_slot, {"selected": True, "settings": None})


def test_second_click_on_selected_key_clears_the_target_without_a_hid_write():
    """A selected cap is a toggle: the second click must lock the editor."""
    manager = QMKManager.__new__(QMKManager)
    q_slot = next(key.slot for key in SK75_KEYS if key.hid == 20)
    settings = SimpleNamespace(actuation=1.2)
    manager.magnetic_selected_slot = q_slot
    manager.magnetic_visual_selected_slot = q_slot
    manager._magnetic_settings_from_controls = lambda: settings
    loads = []
    manager._load_magnetic_controls = lambda slot, update=True: loads.append((slot, update))
    paints = []
    manager._patch_magnetic_picker_keycap = lambda slot, **kwargs: (
        paints.append((slot, kwargs)) or True
    )
    manager._schedule_magnetic_key_write = lambda: (_ for _ in ()).throw(
        AssertionError("deselecting a key must not schedule HID")
    )
    manager._refresh_sk75_keyboard_picker = lambda: (_ for _ in ()).throw(
        AssertionError("a mounted keycap should be patched locally")
    )

    manager._select_sk75_key(q_slot)

    assert manager.magnetic_selected_slot is None
    assert manager.magnetic_visual_selected_slot is None
    assert loads == [(None, True)]
    assert paints == [(q_slot, {"selected": False, "settings": settings})]


def test_magnetic_key_selection_uses_two_local_keycap_patches_not_a_full_rebuild():
    source = inspect.getsource(QMKManager._select_sk75_key)

    assert "_patch_magnetic_picker_keycap" in source
    # A full deck render remains only as a first-mount fallback, after both
    # direct keycap patches have been attempted.
    assert "if not (old_painted and new_painted)" in source
    assert source.index("_patch_magnetic_picker_keycap") < source.index(
        "_refresh_sk75_keyboard_picker()"
    )


def test_top_down_scales_are_semantically_top_down():
    assert QMKManager._vertical_magnetic_pointer_fraction(
        0, 164, fills_from_top=True
    ) == 0.0
    assert QMKManager._vertical_magnetic_pointer_fraction(
        164, 164, fills_from_top=True
    ) == 1.0
    assert QMKManager._vertical_magnetic_pointer_fraction(
        0, 164, fills_from_top=False
    ) == 1.0
    assert QMKManager._vertical_magnetic_pointer_fraction(
        164, 164, fills_from_top=False
    ) == 0.0


def test_selected_key_panel_restores_compact_vertical_rulers_and_normal_deactivation():
    source = inspect.getsource(QMKManager._build_magnetic_lab_card)

    assert "_make_vertical_magnetic_control" in source
    assert "magnetic_deactivation_control" in source
    # The experimental combined green travel meter is intentionally no
    # longer part of the selected-key panel; every setting has its own
    # compact travel-test ruler again.
    assert "_build_magnetic_travel_visualization" not in source
    assert "magnetic_parameter_cards" in source


def test_dead_zones_live_in_a_permanent_right_hand_group():
    """Visibility changes in the primary group must not reflow dead zones."""
    source = inspect.getsource(QMKManager._build_magnetic_lab_card)

    primary_start = source.index("self.magnetic_primary_parameter_cards = ft.Row(")
    zones_start = source.index("self.magnetic_dead_zone_cards = ft.Row(")
    cards_start = source.index("self.magnetic_parameter_cards = ft.Row(")
    primary_source = source[primary_start:zones_start]
    cards_source = source[cards_start:]

    assert "self.magnetic_deactivation_control," in primary_source
    assert "self.magnetic_lower_dead_zone_control" not in primary_source
    assert "self.magnetic_lower_dead_zone_control," in source[zones_start:cards_start]
    assert "self.magnetic_upper_dead_zone_control," in source[zones_start:cards_start]
    assert "self.magnetic_primary_parameter_cards," in cards_source
    assert "self.magnetic_dead_zone_spacer," in cards_source
    assert "self.magnetic_dead_zone_cards," in cards_source


def test_compact_rt_controls_preserve_existing_release_and_repeat_ranges():
    source = inspect.getsource(QMKManager._build_magnetic_lab_card)

    # The restored compact rulers still feed the same
    # hundredths-of-a-millimetre values into the unchanged device model.
    assert 'MAGNETIC_SCALE_ROLE_LABELS["rapid_release"], 20, 1, 200, 199' in source
    assert 'MAGNETIC_SCALE_ROLE_LABELS["rapid_press"], 15, 1, 200, 199' in source
    assert '"Точка\\nдеактивации", 30, 10, 330, 320' in source


def test_top_down_rt_ruler_has_minimum_at_top_and_maximum_at_bottom():
    """The inverted RT visual stays consistent for both drag and captions."""
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None

    _label, state, _control = manager._make_vertical_magnetic_control(
        "RT при отпускании", 20, 1, 100, 99, ft.Colors.AMBER, fills_from_top=True
    )

    assert state.fill.top == 0
    assert state.fill.bottom is None
    assert state.top_endpoint.value == "MIN 0.01 мм"
    assert state.bottom_endpoint.value == "MAX 1.00 мм"
    manager._set_vertical_magnetic_value(state, 100)
    assert state.thumb.top == state.track_height - state.thumb_height


def test_upper_dead_zone_visual_fill_and_thumb_grow_down_from_the_top():
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None

    _label, state, _control = manager._make_vertical_magnetic_control(
        "Дез-зона сверху", 10, 0, 100, 100, ft.Colors.PINK, fills_from_top=True
    )

    assert state.fills_from_top is True
    assert state.fill.top == 0
    assert state.fill.bottom is None
    manager._set_vertical_magnetic_value(state, 100)
    assert state.fill.height == state.track_height
    assert state.thumb.top == state.track_height - state.thumb_height


def test_magnetic_scale_uses_drag_but_leaves_mouse_wheel_for_page_scrolling():
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None

    _label, _state, control = manager._make_vertical_magnetic_control(
        "Активация", 150, 10, 350, 340, ft.Colors.CYAN, fills_from_top=True
    )

    drag_surface = control.content.controls[2]
    assert drag_surface.on_vertical_drag_update is not None
    assert drag_surface.on_scroll is None


def test_magnetic_scale_plus_minus_move_one_exact_division_and_live_save():
    """The precision controls share the drag path, including its debounce."""
    manager = QMKManager.__new__(QMKManager)
    writes = []
    manager._on_magnetic_control_changed = lambda: writes.append("save")

    _label, state, control = manager._make_vertical_magnetic_control(
        "Активация", 120, 10, 330, 320, ft.Colors.CYAN, fills_from_top=True
    )

    assert state.step == 1
    value_row = control.content.controls[1]
    assert value_row.controls[0].content is state.decrease_button
    assert value_row.controls[-1].content is state.increase_button

    state.increase_button.on_click(None)
    assert state.value == 121
    state.decrease_button.on_click(None)
    assert state.value == 120
    assert writes == ["save", "save"]

    manager._set_vertical_magnetic_value(state, state.minimum)
    assert state.decrease_button.disabled is True
    manager._set_vertical_magnetic_value(state, state.maximum)
    assert state.increase_button.disabled is True


def test_magnetic_scale_exact_step_mirrors_keycap_metrics_immediately():
    """One +/- click sends the changed cap text before deferred HID work."""
    manager = QMKManager.__new__(QMKManager)
    slot = SK75_KEYS[0].slot
    manager.magnetic_selected_slot = slot
    manager.magnetic_visual_selected_slot = slot
    manager.magnetic_rt_switch = SimpleNamespace(value=True)
    manager.magnetic_rt_separate_switch = SimpleNamespace(value=True)
    manager.magnetic_deactivation_separate_switch = SimpleNamespace(value=False)
    manager.magnetic_rt_release_slider = SimpleNamespace(value=30)
    manager.magnetic_rt_press_slider = SimpleNamespace(value=45)
    manager.magnetic_lower_dead_zone_slider = SimpleNamespace(value=10)
    manager.magnetic_upper_dead_zone_slider = SimpleNamespace(value=0)
    manager.magnetic_deactivation_slider = SimpleNamespace(value=120)
    manager._magnetic_key_is_advanced = lambda _slot: False
    commits = []
    patches = []
    manager._on_magnetic_control_changed = lambda: commits.append("hid")
    manager._patch_magnetic_picker_keycap = lambda value, **kwargs: patches.append(
        (value, kwargs["settings"].actuation)
    )

    _label, state, _control = manager._make_vertical_magnetic_control(
        "Активация", 120, 10, 330, 320, ft.Colors.CYAN, fills_from_top=True
    )
    manager.magnetic_actuation_slider = state

    state.increase_button.on_click(None)

    assert state.value == 121
    assert state.value_text.value == "1.21 мм"
    # The normal write path still follows the exact click, while the current
    # keycap gets its new corner metric without waiting for a second callback.
    assert commits == ["hid"]
    assert patches == [(slot, 1.21)]


def test_after_frame_keycap_refresh_discards_stale_clicks_before_painting():
    """Rapid +/- input gets one latest keycap mirror, never a timer thread."""
    manager = QMKManager.__new__(QMKManager)
    slot = SK75_KEYS[0].slot
    manager.magnetic_selected_slot = slot
    manager.magnetic_visual_selected_slot = slot
    manager._magnetic_parameter_mode_transition = False
    callbacks = []
    paints = []
    manager._ui_call = lambda callback: callbacks.append(callback)
    manager._magnetic_settings_from_controls = lambda: object()
    manager._patch_magnetic_picker_keycap = lambda *args, **kwargs: paints.append(
        (args, kwargs)
    )

    manager._schedule_magnetic_keycap_refresh(slot)
    manager._schedule_magnetic_keycap_refresh(slot)

    assert len(callbacks) == 2
    assert paints == []
    callbacks[0]()
    assert paints == []
    callbacks[1]()
    assert len(paints) == 1


def test_exact_step_defers_packet_scheduling_until_after_input_handler():
    """A live-page +/- callback must not wait on config/HID work itself."""
    manager = QMKManager.__new__(QMKManager)
    slot = SK75_KEYS[0].slot
    manager.page = SimpleNamespace()
    manager.magnetic_selected_slot = slot
    manager.magnetic_visual_selected_slot = slot
    callbacks = []
    writes = []
    manager._ui_call = lambda callback: callbacks.append(callback)
    manager._on_magnetic_control_changed = lambda: writes.append(True)

    _label, state, _control = manager._make_vertical_magnetic_control(
        "Активация", 120, 10, 330, 320, ft.Colors.CYAN, fills_from_top=True
    )
    state.increase_button.on_click(None)

    assert state.value == 121
    assert writes == []
    assert len(callbacks) == 1

    callbacks[0]()
    assert writes == [True]


def test_vertical_drag_consumes_every_raw_event_before_flet_auto_page_update(monkeypatch):
    """Skipped drag samples must never fall through to Flet's page auto-update."""
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None
    manager._schedule_magnetic_keycap_refresh = lambda _slot: None
    consumed = []
    monkeypatch.setattr(
        ft.context,
        "mark_update_called",
        lambda: consumed.append(True),
    )

    _label, state, control = manager._make_vertical_magnetic_control(
        "Активация", 120, 10, 330, 320, ft.Colors.CYAN, fills_from_top=True
    )
    # Force the second pointer event into the visual throttle window.  It
    # still needs to mark the Flet event handled even though it emits no
    # control patch itself.
    state.visual_update_interval = 60.0
    drag_surface = control.content.controls[2]
    event = lambda y: SimpleNamespace(local_position=SimpleNamespace(y=y))

    drag_surface.on_tap_down(event(0))
    drag_surface.on_vertical_drag_update(event(24))

    assert len(consumed) >= 2


def test_magnetic_scale_exact_step_falls_back_to_stable_panel_if_leaf_is_busy():
    """A lost leaf patch must not leave +/- values stale until an RT toggle."""
    manager = QMKManager.__new__(QMKManager)
    saved = []
    parent_patches = []
    manager._on_magnetic_control_changed = lambda: saved.append(True)
    manager._patch_magnetic_parameter_panel = lambda: parent_patches.append(True) or True

    _label, state, _control = manager._make_vertical_magnetic_control(
        "Активация", 120, 10, 330, 320, ft.Colors.CYAN, fills_from_top=True
    )

    def busy_leaf_update():
        raise RuntimeError("Flet child is settling")

    # The dynamic paint layer is the normal Flet patch target.  Simulate both
    # it and the rare full-wrapper fallback being busy, so the stable parent
    # remains the final safety net for a settling scroll view.
    state.paint_layer.update = busy_leaf_update
    state.control.update = busy_leaf_update
    state.increase_button.on_click(None)

    assert state.value == 121
    assert saved == [True]
    assert parent_patches == [True]


def test_magnetic_scale_exact_step_patches_only_dynamic_paint_and_readout_leaves():
    """A normal +/- click never traverses the static tick/ruler container.

    Flet 0.85 computes an update diff from the requested control downwards.
    This regression guard keeps the hot 0.01-mm path to two tiny leaves: the
    fill/thumb overlay and the numeric readout.  The outer ruler owns 17
    static tick controls and must stay outside that patch surface.
    """
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None
    parent_patches = []
    manager._patch_magnetic_parameter_panel = lambda: parent_patches.append(True) or True

    _label, state, control = manager._make_vertical_magnetic_control(
        "Активация", 120, 10, 330, 320, ft.Colors.CYAN, fills_from_top=True
    )
    leaf_patches = []
    state.paint_layer.update = lambda: leaf_patches.append("paint")
    state.value_text.update = lambda: leaf_patches.append("readout")
    control.update = lambda: leaf_patches.append("outer")

    state.increase_button.on_click(None)

    assert state.control is control
    assert state.value == 121
    assert leaf_patches == ["paint", "readout"]
    assert parent_patches == []


def test_magnetic_scale_keeps_static_ticks_outside_the_dynamic_paint_leaf():
    """The glow/fill/thumb leaf contains no tick or endpoint controls."""
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None

    _label, state, _control = manager._make_vertical_magnetic_control(
        "Активация", 120, 10, 330, 320, ft.Colors.CYAN, fills_from_top=True
    )

    assert state.track.controls[0] is state.rail
    assert state.paint_layer.controls == [
        state.fill_glow,
        state.fill,
        state.thumb_glow,
        state.thumb,
    ]
    assert not any(tick in state.paint_layer.controls for tick in state.tick_controls)


def test_magnetic_scale_endpoint_patches_only_the_button_whose_state_changes():
    """Reaching MAX does not redraw both Material +/- state layers."""
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None
    _label, state, control = manager._make_vertical_magnetic_control(
        "Активация", 329, 10, 330, 320, ft.Colors.CYAN, fills_from_top=True
    )
    calls = []
    state.paint_layer.update = lambda: calls.append("paint")
    state.value_text.update = lambda: calls.append("readout")
    state.decrease_button.update = lambda: calls.append("decrease")
    state.increase_button.update = lambda: calls.append("increase")
    control.update = lambda: calls.append("outer")

    state.increase_button.on_click(None)

    assert state.value == state.maximum
    assert calls == ["paint", "readout", "increase"]


def test_magnetic_control_change_patches_only_the_current_keycap_metrics():
    """A ruler edit must not wait for selecting another key to show up."""
    manager = QMKManager.__new__(QMKManager)
    manager.magnetic_selected_slot = 12
    manager.magnetic_visual_selected_slot = 12
    manager.magnetic_rt_separate_switch = SimpleNamespace(value=True)
    manager.magnetic_rt_switch = SimpleNamespace(value=True)
    manager.magnetic_actuation_slider = SimpleNamespace(value=120)
    manager.magnetic_deactivation_slider = SimpleNamespace(value=120)
    manager.magnetic_rt_release_slider = SimpleNamespace(value=30)
    manager.magnetic_rt_press_slider = SimpleNamespace(value=45)
    manager.magnetic_lower_dead_zone_slider = SimpleNamespace(value=10)
    manager.magnetic_upper_dead_zone_slider = SimpleNamespace(value=5)
    manager._magnetic_key_is_advanced = lambda _slot: False
    patched = []
    manager._patch_magnetic_picker_keycap = lambda slot, **kwargs: patched.append(
        (slot, kwargs)
    )
    scheduled = []
    manager._schedule_magnetic_key_write = lambda: scheduled.append(True)

    manager._on_magnetic_control_changed()

    assert scheduled == [True]
    assert len(patched) == 1
    slot, kwargs = patched[0]
    assert slot == 12
    assert kwargs["selected"] is True
    settings = kwargs["settings"]
    assert settings.actuation == 1.20
    assert settings.rapid_release == 0.30
    assert settings.rapid_press == 0.45
    assert settings.deactivation == 1.20


def test_vertical_drag_keeps_local_values_live_but_debounces_hid_until_release():
    """A dense pointer stream must not create a Timer/HID write per pixel.

    The ruler's model and readout still change for every drag sample.  Flet
    paints the small subtree at a bounded cadence, and exactly one normal
    latest-wins debounce is requested when the gesture ends.
    """
    manager = QMKManager.__new__(QMKManager)
    manager.magnetic_selected_slot = SK75_KEYS[0].slot
    manager.magnetic_visual_selected_slot = SK75_KEYS[0].slot
    manager.magnetic_rt_switch = SimpleNamespace(value=True)
    manager.magnetic_rt_separate_switch = SimpleNamespace(value=True)
    manager.magnetic_deactivation_separate_switch = SimpleNamespace(value=False)
    manager.magnetic_rt_release_slider = SimpleNamespace(value=30)
    manager.magnetic_rt_press_slider = SimpleNamespace(value=45)
    manager.magnetic_lower_dead_zone_slider = SimpleNamespace(value=10)
    manager.magnetic_upper_dead_zone_slider = SimpleNamespace(value=0)
    manager.magnetic_deactivation_slider = SimpleNamespace(value=120)
    manager._magnetic_key_is_advanced = lambda _slot: False
    keycap_patches = []
    manager._patch_magnetic_picker_keycap = lambda slot, **kwargs: keycap_patches.append(
        (slot, kwargs["settings"].actuation)
    )
    hid_debounces = []
    manager._schedule_magnetic_key_write = lambda: hid_debounces.append(True)

    _label, state, control = manager._make_vertical_magnetic_control(
        "Активация", 120, 10, 330, 320, ft.Colors.CYAN, fills_from_top=True
    )
    manager.magnetic_actuation_slider = state
    # Keep the test's rapid raw stream deterministically inside one visual
    # interval.  The final end event always forces the current value through.
    state.visual_update_interval = 60.0
    drag_surface = control.content.controls[2]

    def event_at(y):
        return SimpleNamespace(local_position=SimpleNamespace(y=y))

    drag_surface.on_tap_down(event_at(0))
    for y in range(16, state.track_height, 16):
        drag_surface.on_vertical_drag_update(event_at(y))

    assert hid_debounces == []
    assert state.value_text.value != "1.20 мм"
    assert state.value > 120
    # The ruler and only the selected cap's changed metric leaf stay live.  At
    # this deliberately huge paint interval, the initial pointer sample is
    # the only bounded mirror before the final forced sample.
    assert keycap_patches == [(SK75_KEYS[0].slot, 0.1)]

    drag_surface.on_vertical_drag_end(event_at(state.track_height))

    assert state.value == 330
    assert state.value_text.value == "3.30 мм"
    assert hid_debounces == [True]
    # The final forced paint mirrors the actual last metric, without a
    # delayed whole-cap pass.
    assert keycap_patches[-1] == (SK75_KEYS[0].slot, 3.3)
    assert len(keycap_patches) == 2


def test_magnetic_scale_has_no_outer_rounded_card_chrome():
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None

    _label, _state, control = manager._make_vertical_magnetic_control(
        "RT вниз", 15, 1, 100, 99, ft.Colors.GREEN
    )

    assert control.bgcolor is None
    assert control.border is None
    assert control.border_radius is None
    # The scale contains a dark rail for legibility, but the rail is not a
    # second outlined card around the control.
    track = control.content.controls[2].content.content
    rail = track.controls[0]
    assert rail.border is None
    assert rail.width < track.width


def test_magnetic_scale_uses_travel_tester_track_and_tonal_precision_readout():
    """The tester-style rail stays compact and keeps exact controls intact."""
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None

    _label, state, control = manager._make_vertical_magnetic_control(
        "Активация", 150, 10, 350, 340, ft.Colors.CYAN, fills_from_top=True
    )

    # The physical rail follows the tester's small rounded frame; the outer
    # control remains an unframed layout wrapper.
    assert state.rail.border_radius == 8
    value_row = control.content.controls[1]
    assert value_row.controls[1].content is state.value_text
    assert state.value_text.color == ft.Colors.CYAN

    # +/- retains its exact one-step callback while receiving only a local
    # M3 state-layer animation, so dragging does not gain any new repaint.
    assert value_row.controls[0].content.style.animation_duration == 120
    assert value_row.controls[-1].content.style.animation_duration == 120
    assert ft.ControlState.HOVERED in value_row.controls[0].content.style.bgcolor


def test_magnetic_scale_keeps_a_full_height_readable_travel_range():
    """Regression: compact layout must not collapse scales into tick fragments."""
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None

    _label, state, control = manager._make_vertical_magnetic_control(
        "Активация", 150, 10, 350, 340, ft.Colors.CYAN, fills_from_top=True
    )

    drag_surface = control.content.controls[2]
    track = drag_surface.content.content
    assert state.track_height >= 200
    assert track.height == state.track_height
    assert drag_surface.content.height == state.track_height
    assert control.height >= state.track_height + 40


def test_magnetic_scale_uses_travel_tester_ruler_and_compact_endpoints():
    """Magnetic sliders reuse the travel test's readable side ruler."""
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None

    _label, state, control = manager._make_vertical_magnetic_control(
        "Активация", 150, 10, 350, 340, ft.Colors.CYAN, fills_from_top=True
    )

    # The top-down activation scale exposes actual range ends, not the old
    # duplicated "сверху / снизу" descriptions.  They live next to the
    # ruler, where they can be read while adjusting the bar.
    assert state.top_endpoint.value == "MIN 0.10 мм"
    assert state.bottom_endpoint.value == "MAX 3.50 мм"
    assert state.tick_count == 17
    assert len(state.tick_controls) == state.tick_count
    assert state.tick_controls[0].width == 34
    assert state.tick_controls[0].left == state.ruler_left
    # The active segment remains inset within the dark rail while the side
    # ruler stays outside it, as in the travel tester.
    assert state.fill.width < state.rail.width
    assert state.fill.left > state.rail.left
    assert state.rail.border_radius == 8


def test_magnetic_scale_uses_a_thin_contrasting_divider_inside_the_track():
    """The active boundary remains visible without a leaking external glow."""
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None

    _label, state, _control = manager._make_vertical_magnetic_control(
        "Активация", 150, 10, 350, 340, ft.Colors.CYAN, fills_from_top=True
    )

    # The active segment and divider follow the travel tester's compact
    # treatment, while the contained glow cannot leak at MIN/MAX.
    assert state.fill.border_radius == 3
    assert state.thumb.border_radius == 1
    assert state.thumb.shadow is None
    assert state.fill.shadow is None
    assert state.fill_glow.width == state.rail.width
    assert state.thumb.width == state.rail.width
    assert state.thumb_glow.width >= state.thumb.width
    assert state.thumb.left <= state.rail.left
    assert state.thumb_glow.left <= state.thumb.left
    track = _control.content.controls[2].content.content
    assert track.clip_behavior == ft.ClipBehavior.HARD_EDGE

    manager._set_vertical_magnetic_value(state, 180)
    # The narrow divider straddles the coloured segment boundary, so it is
    # legible against both the active and inactive parts of the dark rail.
    assert state.thumb.top <= state.fill.height <= state.thumb.top + state.thumb.height

    manager._set_vertical_magnetic_value(state, 350)
    assert state.thumb.top == state.track_height - state.thumb_height
    assert state.thumb_glow.top == state.track_height - state.thumb_glow_height


def test_magnetic_startup_lock_keeps_scales_at_zero_until_hardware_read():
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None
    manager._magnetic_values_ready = False

    states = [
        manager._make_vertical_magnetic_control(
            name, 150, 10, 350, 340, ft.Colors.CYAN, fills_from_top=True
        )[1]
        for name in ("Активация", "RT при отпускании", "RT вниз", "Дез-зона снизу", "Дез-зона сверху")
    ]
    (
        manager.magnetic_actuation_slider,
        manager.magnetic_rt_release_slider,
        manager.magnetic_rt_press_slider,
        manager.magnetic_lower_dead_zone_slider,
        manager.magnetic_upper_dead_zone_slider,
    ) = states
    manager.magnetic_rt_switch = SimpleNamespace(disabled=False)
    manager.magnetic_rt_separate_switch = SimpleNamespace(disabled=False)

    manager._set_magnetic_controls_ready_state(False, update=False)

    assert [state.value for state in states] == [0.0] * 5
    assert all(state.interaction_enabled is False for state in states)
    assert all(state.decrease_button.disabled and state.increase_button.disabled for state in states)
    assert manager.magnetic_rt_switch.disabled is True
    assert manager.magnetic_rt_separate_switch.disabled is True


def test_no_selected_key_keeps_verified_magnetic_values_neutral_and_locked():
    """A completed HID read must not silently target Q before a click."""
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None
    manager._magnetic_values_ready = True
    manager.magnetic_selected_slot = None

    states = [
        manager._make_vertical_magnetic_control(
            name, 120, 10, 330, 320, ft.Colors.CYAN, fills_from_top=True
        )[1]
        for name in (
            "Активация",
            "Деактивация",
            "RT при отпускании",
            "RT вниз",
            "Дез-зона снизу",
            "Дез-зона сверху",
        )
    ]
    (
        manager.magnetic_actuation_slider,
        manager.magnetic_deactivation_slider,
        manager.magnetic_rt_release_slider,
        manager.magnetic_rt_press_slider,
        manager.magnetic_lower_dead_zone_slider,
        manager.magnetic_upper_dead_zone_slider,
    ) = states
    manager.magnetic_rt_switch = SimpleNamespace(disabled=False)
    manager.magnetic_rt_separate_switch = SimpleNamespace(disabled=False)
    manager.magnetic_deactivation_separate_switch = SimpleNamespace(disabled=False)

    manager._load_magnetic_controls(None, update=False)

    assert manager._magnetic_values_ready is True
    assert [state.value for state in states] == [0.0] * 6
    assert all(state.interaction_enabled is False for state in states)
    assert all(state.decrease_button.disabled and state.increase_button.disabled for state in states)
    assert manager.magnetic_rt_switch.disabled is True
    assert manager.magnetic_rt_separate_switch.disabled is True
    assert manager.magnetic_deactivation_separate_switch.disabled is True


def test_material3_parameter_card_uses_native_slider_and_exact_step_buttons():
    manager = QMKManager.__new__(QMKManager)
    saved = []
    manager._on_magnetic_control_changed = lambda: saved.append(True)

    value_text, state, card = manager._make_m3_magnetic_parameter_control(
        "Активация",
        "Точка срабатывания клавиши",
        120,
        10,
        330,
        320,
        ft.Colors.CYAN,
        ft.Icons.ADS_CLICK_ROUNDED,
    )

    assert state.presentation == "m3_parameter"
    assert isinstance(state.slider, ft.Slider)
    assert state.slider.year_2023 is False
    assert card.border_radius == 20
    assert value_text.value == "1.20"

    state.increase_button.on_click(None)
    assert state.value == 121
    assert value_text.value == "1.21"
    assert saved == [True]

    state.slider.on_change(SimpleNamespace(control=SimpleNamespace(value=123)))
    assert state.value == 123
    assert value_text.value == "1.23"
    # Native slider motion updates only the local card.  The ordinary
    # debounce/HID callback is intentionally emitted once when the gesture
    # ends, rather than once for every raw thumb position.
    assert saved == [True]
    state.slider.on_change_end(None)
    assert saved == [True, True]


def test_vertical_material3_parameter_card_keeps_exact_buttons_and_drag_state():
    """The current visible magnetic controls stay vertical without losing precision."""
    manager = QMKManager.__new__(QMKManager)
    saved = []
    manager._on_magnetic_control_changed = lambda: saved.append(True)

    value_text, state, card = manager._make_m3_vertical_magnetic_parameter_control(
        "Активация",
        "Точка срабатывания клавиши",
        120,
        10,
        330,
        320,
        ft.Colors.CYAN,
        ft.Icons.ADS_CLICK_ROUNDED,
        fills_from_top=True,
    )

    assert state.presentation == "m3_vertical_parameter"
    assert state.track.height == state.track_height
    assert state.fill.top == 0
    assert card.border_radius == 20
    assert value_text.value == "1.20"

    state.increase_button.on_click(None)
    assert state.value == 121
    assert value_text.value == "1.21"
    assert saved == [True]


def test_vertical_material3_drag_coalesces_callback_until_gesture_end():
    """The retained M3 vertical factory must not revive per-sample HID work."""
    manager = QMKManager.__new__(QMKManager)
    saved = []
    manager.magnetic_selected_slot = SK75_KEYS[0].slot
    manager._on_magnetic_control_changed = lambda: saved.append(True)

    _value, state, card = manager._make_m3_vertical_magnetic_parameter_control(
        "Активация",
        "Точка срабатывания клавиши",
        120,
        10,
        330,
        320,
        ft.Colors.CYAN,
        ft.Icons.ADS_CLICK_ROUNDED,
        fills_from_top=True,
    )
    state.visual_update_interval = 60.0
    drag_surface = card.content.controls[2].content
    event = lambda y: SimpleNamespace(local_position=SimpleNamespace(y=y))

    drag_surface.on_tap_down(event(0))
    drag_surface.on_vertical_drag_update(event(100))
    assert saved == []

    drag_surface.on_vertical_drag_end(event(state.track_height))
    assert saved == [True]


def test_vertical_travel_meter_uses_green_rt_fill_and_linked_markers():
    manager = QMKManager.__new__(QMKManager)
    manager.magnetic_actuation_slider = SimpleNamespace(value=120)
    manager.magnetic_rt_release_slider = SimpleNamespace(value=30)
    manager.magnetic_rt_switch = SimpleNamespace(value=True)

    meter = manager._build_magnetic_travel_visualization()
    manager._refresh_magnetic_travel_visualization(update=False)

    assert meter.width == 258
    assert manager.magnetic_travel_rapid_fill.height > 0
    assert manager.magnetic_travel_activation_marker.top > manager.magnetic_travel_deactivation_marker.top
    assert "Зелёная" in manager.magnetic_travel_mode_caption.value


def test_material3_parameter_mode_switches_labels_and_cards_without_recreating_values():
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None
    _, manager.magnetic_actuation_slider, manager.magnetic_actuation_control = manager._make_m3_magnetic_parameter_control(
        "Активация", "x", 120, 10, 330, 320, ft.Colors.CYAN, ft.Icons.ADS_CLICK_ROUNDED
    )
    _, manager.magnetic_rt_release_slider, manager.magnetic_rt_release_control = manager._make_m3_magnetic_parameter_control(
        "RT при отпускании", "x", 30, 1, 200, 199, ft.Colors.AMBER, ft.Icons.KEYBOARD_RETURN_ROUNDED
    )
    _, manager.magnetic_rt_press_slider, manager.magnetic_rt_press_control = manager._make_m3_magnetic_parameter_control(
        "RT при повторном нажатии вниз", "x", 45, 1, 200, 199, ft.Colors.TEAL, ft.Icons.KEYBOARD_DOUBLE_ARROW_DOWN_ROUNDED
    )
    _, manager.magnetic_lower_dead_zone_slider, manager.magnetic_lower_dead_zone_control = manager._make_m3_magnetic_parameter_control(
        "Дез-зона снизу", "x", 10, 0, 100, 100, ft.Colors.ORANGE, ft.Icons.VERTICAL_ALIGN_BOTTOM_ROUNDED
    )
    _, manager.magnetic_upper_dead_zone_slider, manager.magnetic_upper_dead_zone_control = manager._make_m3_magnetic_parameter_control(
        "Дез-зона сверху", "x", 20, 0, 100, 100, ft.Colors.PINK, ft.Icons.VERTICAL_ALIGN_TOP_ROUNDED
    )
    manager.magnetic_rt_switch = SimpleNamespace(value=True)
    manager.magnetic_rt_separate_switch = SimpleNamespace(value=False)
    manager.magnetic_deactivation_separate_switch = SimpleNamespace(value=False)
    manager.magnetic_rt_separate_surface = SimpleNamespace(visible=False, opacity=0)
    manager.magnetic_deactivation_separate_surface = SimpleNamespace(visible=False, opacity=0)
    manager.magnetic_dead_zone_spacer = SimpleNamespace(visible=False, opacity=0)
    manager.magnetic_parameter_mode_title = SimpleNamespace(value=None)
    manager.magnetic_parameter_mode_description = SimpleNamespace(value=None)
    manager.magnetic_parameter_mode_badge_text = SimpleNamespace(value=None, color=None)
    manager.magnetic_parameter_mode_badge = SimpleNamespace(bgcolor=None)
    manager.magnetic_parameter_mode_surface = SimpleNamespace(bgcolor=None)

    manager._update_magnetic_parameter_mode_ui(update=False)
    assert manager.magnetic_parameter_mode_title.value == "Rapid Trigger"
    assert manager.magnetic_parameter_mode_badge_text.value == "ВКЛ"
    assert manager.magnetic_actuation_slider.title_text.value == "Активация"
    assert manager.magnetic_rt_release_slider.title_text.value == "RT при отпускании"
    assert manager.magnetic_lower_dead_zone_control.visible is True
    assert manager.magnetic_rt_press_control.visible is False
    assert manager.magnetic_dead_zone_spacer.visible is True
    assert manager.magnetic_rt_release_slider.value == 30

    manager.magnetic_rt_switch.value = False
    manager._update_magnetic_parameter_mode_ui(update=False)
    assert manager.magnetic_parameter_mode_title.value == "Rapid Trigger"
    assert manager.magnetic_parameter_mode_badge_text.value == "ВЫКЛ"
    assert manager.magnetic_actuation_slider.title_text.value == "Точка активации"
    assert manager.magnetic_rt_release_slider.title_text.value == "Точка деактивации"
    assert manager.magnetic_lower_dead_zone_control.visible is True
    assert manager.magnetic_upper_dead_zone_control.visible is True
    assert manager.magnetic_dead_zone_spacer.visible is True
    # Switching the view must not mutate the existing RT value.
    assert manager.magnetic_rt_release_slider.value == 30


def test_compact_rulers_switch_to_a_distinct_deactivation_scale_when_rt_is_off():
    """RT mode only changes the persistent ruler visibility, not values."""
    manager = QMKManager.__new__(QMKManager)
    manager._on_magnetic_control_changed = lambda: None

    def ruler(label, value, minimum, maximum, divisions, color, *, top=True, icon=None):
        return manager._make_vertical_magnetic_control(
            label,
            value,
            minimum,
            maximum,
            divisions,
            color,
            fills_from_top=top,
            icon=icon,
        )

    _, manager.magnetic_actuation_slider, manager.magnetic_actuation_control = ruler(
        "Активация", 120, 10, 330, 320, ft.Colors.CYAN,
        icon=ft.Icons.ADS_CLICK_ROUNDED,
    )
    _, manager.magnetic_rt_release_slider, manager.magnetic_rt_release_control = ruler(
        "RT при\nотпускании", 30, 1, 200, 199, ft.Colors.AMBER,
        icon=ft.Icons.KEYBOARD_RETURN_ROUNDED,
    )
    _, manager.magnetic_rt_press_slider, manager.magnetic_rt_press_control = ruler(
        "RT вниз", 45, 1, 200, 199, ft.Colors.TEAL,
        icon=ft.Icons.KEYBOARD_DOUBLE_ARROW_DOWN_ROUNDED,
    )
    _, manager.magnetic_lower_dead_zone_slider, manager.magnetic_lower_dead_zone_control = ruler(
        "Дез-зона\nснизу", 10, 0, 100, 100, ft.Colors.ORANGE, top=False,
        icon=ft.Icons.VERTICAL_ALIGN_BOTTOM_ROUNDED,
    )
    _, manager.magnetic_upper_dead_zone_slider, manager.magnetic_upper_dead_zone_control = ruler(
        "Дез-зона\nсверху", 20, 0, 100, 100, ft.Colors.PINK,
        icon=ft.Icons.VERTICAL_ALIGN_TOP_ROUNDED,
    )
    _, manager.magnetic_deactivation_slider, manager.magnetic_deactivation_control = ruler(
        "Точка\nдеактивации", 40, 10, 330, 320, ft.Colors.AMBER,
        icon=ft.Icons.KEYBOARD_RETURN_ROUNDED,
    )
    manager.magnetic_rt_switch = SimpleNamespace(value=True)
    manager.magnetic_rt_separate_switch = SimpleNamespace(value=False)
    manager.magnetic_deactivation_separate_switch = SimpleNamespace(value=False)
    manager.magnetic_rt_separate_surface = SimpleNamespace(visible=False, opacity=0)
    manager.magnetic_deactivation_separate_surface = SimpleNamespace(visible=False, opacity=0)
    manager.magnetic_dead_zone_spacer = SimpleNamespace(visible=False, opacity=0)
    manager.magnetic_parameter_mode_title = SimpleNamespace(value=None)
    manager.magnetic_parameter_mode_description = SimpleNamespace(value=None)
    manager.magnetic_parameter_mode_badge_text = SimpleNamespace(value=None, color=None)
    manager.magnetic_parameter_mode_badge = SimpleNamespace(bgcolor=None)
    manager.magnetic_parameter_mode_surface = SimpleNamespace(bgcolor=None)

    manager._update_magnetic_parameter_mode_ui(update=False)
    assert manager.magnetic_actuation_slider.label_text.value == "Активация"
    assert manager.magnetic_rt_release_slider.label_text.value == "RT при\nотпускании"
    assert manager.magnetic_rt_release_control.visible is True
    assert manager.magnetic_deactivation_control.visible is False
    assert manager.magnetic_dead_zone_spacer.visible is True
    icon_frame = manager.magnetic_actuation_control.content.controls[0].content.controls[0]
    assert isinstance(icon_frame, ft.Container)
    assert isinstance(icon_frame.content, ft.Icon)
    assert icon_frame.border is not None

    manager.magnetic_rt_switch.value = False
    manager._update_magnetic_parameter_mode_ui(update=False)

    assert manager.magnetic_actuation_slider.label_text.value == "Точка\nактивации"
    assert manager.magnetic_deactivation_control.visible is False
    assert manager.magnetic_rt_release_control.visible is False
    assert manager.magnetic_rt_press_control.visible is False
    assert manager.magnetic_lower_dead_zone_control.visible is True
    assert manager.magnetic_upper_dead_zone_control.visible is True
    # The reserved column remains in normal mode so enabling the independent
    # deactivation threshold cannot make both dead-zone rulers jump left.
    assert manager.magnetic_dead_zone_spacer.visible is True
    assert manager.magnetic_rt_separate_surface.visible is False
    assert manager.magnetic_deactivation_separate_surface.visible is True
    # No view switch can overwrite either persisted threshold.
    assert manager.magnetic_rt_release_slider.value == 30
    assert manager.magnetic_deactivation_slider.value == 40

    # The normal-mode toggle uses the official liftTravel field and reveals
    # the independent ruler only after an explicit user choice.
    manager.magnetic_deactivation_separate_switch.value = True
    manager._update_magnetic_deactivation_separation_ui(update=False)
    assert manager.magnetic_deactivation_control.visible is True


def test_rt_mode_change_patches_one_parameter_parent_not_four_children():
    """Regression for Flet's mutable-control-map race during RT toggles."""
    manager = QMKManager.__new__(QMKManager)

    def state(title):
        return SimpleNamespace(
            presentation="m3_vertical_parameter",
            title_text=SimpleNamespace(value=title),
            supporting=SimpleNamespace(value=None),
        )

    manager.magnetic_actuation_slider = state("Активация")
    manager.magnetic_rt_release_slider = state("RT")
    manager.magnetic_rt_press_slider = state("RT вниз")
    manager.magnetic_rt_switch = SimpleNamespace(value=False)
    manager.magnetic_rt_separate_switch = SimpleNamespace(value=False)
    manager.magnetic_rt_separate_surface = SimpleNamespace(visible=True, opacity=1.0)
    manager.magnetic_rt_press_control = SimpleNamespace(visible=True, opacity=1.0)
    manager.magnetic_lower_dead_zone_control = SimpleNamespace(visible=True, opacity=1.0)
    manager.magnetic_upper_dead_zone_control = SimpleNamespace(visible=True, opacity=1.0)
    manager.magnetic_dead_zone_spacer = SimpleNamespace(visible=False, opacity=0.0)
    manager.magnetic_parameter_mode_title = SimpleNamespace(value=None)
    manager.magnetic_parameter_mode_description = SimpleNamespace(value=None)
    manager.magnetic_parameter_mode_badge_text = SimpleNamespace(value=None, color=None)
    manager.magnetic_parameter_mode_badge = SimpleNamespace(bgcolor=None)
    manager.magnetic_parameter_mode_surface = SimpleNamespace(bgcolor=None)
    patches = []
    scheduled = []
    manager.magnetic_parameter_panel = SimpleNamespace(
        page=SimpleNamespace(
            session=SimpleNamespace(
                schedule_update=lambda control: scheduled.append(control)
            )
        ),
        update=lambda: patches.append("panel"),
    )

    manager._update_magnetic_parameter_mode_ui(update=True)

    # The stable parent is patched immediately.  Deferring through Flet's
    # session scheduler was the race that could iterate a mutated control map
    # during a second fast RT toggle.
    assert patches == ["panel"]
    assert scheduled == []
    assert manager.magnetic_rt_press_control.visible is False
    assert manager.magnetic_lower_dead_zone_control.visible is True
    assert manager.magnetic_upper_dead_zone_control.visible is True


def test_magnetic_mode_transition_batches_control_mutations_before_one_parent_patch():
    """Fast separate-RT toggles must not interleave a keycap patch with the panel."""
    manager = QMKManager.__new__(QMKManager)
    events = []

    manager._update_magnetic_parameter_mode_ui = lambda *, update: events.append(
        ("panel", update)
    )
    manager._on_magnetic_control_changed = lambda: events.append("write")

    for _ in range(4):
        manager._commit_magnetic_parameter_mode_transition()

    assert events == [
        ("panel", False), "write", ("panel", True),
    ] * 4
    assert manager._magnetic_parameter_mode_transition is False


def test_binding_summary_counts_enabled_rules_without_needing_a_page_redraw():
    manager = QMKManager.__new__(QMKManager)
    manager.config = {
        "bindings": [
            {"process": "game.exe", "profile_index": 0},
            {"process": "work.exe", "profile_index": 1, "enabled": False},
        ]
    }
    manager._profile_items = lambda: [("Игры", {}), ("Работа", {})]
    manager.bindings_total_value = SimpleNamespace(value=None)
    manager.bindings_enabled_value = SimpleNamespace(value=None)
    manager.bindings_profiles_value = SimpleNamespace(value=None)
    manager.bindings_summary_status = SimpleNamespace(value=None)

    manager._refresh_bindings_summary()

    assert manager.bindings_total_value.value == "2"
    assert manager.bindings_enabled_value.value == "1"
    assert manager.bindings_profiles_value.value == "2"
    assert "выключены: 1" in manager.bindings_summary_status.value


def test_binding_search_matches_process_or_profile_but_keeps_original_indexes():
    manager = QMKManager.__new__(QMKManager)
    manager.config = {
        "bindings": [
            {"process": "cs2.exe", "profile_index": 0},
            {"process": "discord.exe", "profile_index": 1},
            {"process": "osu!.exe", "profile_index": 2},
        ]
    }
    manager._profile_name_at = lambda index: ("CS", "Discord", "Rhythm")[index]
    manager.binding_search_field = SimpleNamespace(value="dis")

    assert manager._filtered_binding_items() == [
        (1, {"process": "discord.exe", "profile_index": 1})
    ]

    # A profile name is searchable too, while callbacks still receive the
    # source list index rather than the position in a filtered list.
    manager.binding_search_field.value = "rhythm"
    assert manager._filtered_binding_items() == [
        (2, {"process": "osu!.exe", "profile_index": 2})
    ]


def test_binding_search_clear_button_clears_query_and_refreshes_immediately():
    manager = QMKManager.__new__(QMKManager)
    manager.binding_search_field = SimpleNamespace(value="discord")
    manager.binding_search_clear_button = SimpleNamespace(disabled=None)
    refreshes = []
    manager.update_bindings_list = lambda: refreshes.append(True)

    manager._clear_binding_search()

    assert manager.binding_search_field.value == ""
    # The clear icon remains usable and visible even with an empty query.
    assert manager.binding_search_clear_button.disabled is False
    assert refreshes == [True]
