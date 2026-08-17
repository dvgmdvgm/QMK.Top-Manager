from magnetic import KeyMagneticSettings, KeyboardOptions, MagneticProtocol, SK75_VISUAL_LAYOUT


def test_key_settings_are_only_single_slot_packets_and_have_checksums():
    packets = MagneticProtocol.key_settings_packets(
        8,
        KeyMagneticSettings(1.25, True, 0.15, 0.20, 0.05, 0.10),
    )

    assert [packet[1] for packet in packets] == [7, 0, 1, 2, 3, 6, 251]
    assert all(packet[0] == 0x65 and packet[3] == 8 for packet in packets)
    assert [packet[4] for packet in packets] == [0, 0, 0, 0, 0, 0, 1]
    assert packets[1][8:10] == [125, 0]
    # A legacy six-value setting defaults normal deactivation to activation.
    assert packets[2][8:10] == [124, 0]
    assert packets[0][8] == 0x80
    assert all(packet[7] == (255 - sum(packet[:7])) & 0xFF for packet in packets)


def test_normal_mode_deactivation_uses_its_own_official_operation():
    """Operation 1 is Womier's liftTravel, not a Rapid Trigger setting."""
    packets = MagneticProtocol.key_settings_packets(
        8,
        KeyMagneticSettings(
            1.20, False, 0.30, 0.30, 0.10, 0.00, deactivation=0.85
        ),
    )
    by_operation = {packet[1]: packet for packet in packets}

    assert set(by_operation) == {
        MagneticProtocol.OP_MODE,
        MagneticProtocol.OP_ACTUATION,
        MagneticProtocol.OP_DEACTIVATION,
        MagneticProtocol.OP_LOWER_DEAD_ZONE,
        MagneticProtocol.OP_UPPER_DEAD_ZONE,
    }
    assert by_operation[MagneticProtocol.OP_MODE][8] == 0
    assert by_operation[MagneticProtocol.OP_DEACTIVATION][8:10] == [84, 0]


def test_normal_deactivation_decodes_official_lift_travel_offset_and_legacy_zero():
    reports = {}

    def add_bytes(operation, values, chunks):
        for index in range(chunks):
            reports[(operation, index)] = [0] + values[index * 64:(index + 1) * 64]

    def add_words(operation, value):
        values = [0] * 128
        values[8] = value
        packed = []
        for word in values:
            packed.extend((word & 0xFF, word >> 8))
        add_bytes(operation, packed, 4)

    modes = [0] * 128
    add_bytes(MagneticProtocol.OP_MODE, modes, 2)
    for operation, value in (
        (MagneticProtocol.OP_ACTUATION, 120),
        # Official op 1 stores 0.10 mm as 9, not 10.
        (MagneticProtocol.OP_DEACTIVATION, 9),
        (MagneticProtocol.OP_RAPID_PRESS, 30),
        (MagneticProtocol.OP_RAPID_RELEASE, 30),
        (MagneticProtocol.OP_LOWER_DEAD_ZONE, 0),
        (MagneticProtocol.OP_UPPER_DEAD_ZONE, 0),
    ):
        add_words(operation, value)

    decoded = MagneticProtocol.decode_multi_magnetism(reports)
    assert decoded[8].deactivation == 0.10

    # A keyboard touched by an older app can still have an unset op-1 word.
    # Keep its normal release point safe by falling back to actuation.
    add_words(MagneticProtocol.OP_DEACTIVATION, 0)
    assert MagneticProtocol.decode_multi_magnetism(reports)[8].deactivation == 1.20


def test_snap_clear_only_addresses_the_selected_two_keys():
    packets = MagneticProtocol.clear_snap_pair_packets(8, 9)

    assert [packet[3] for packet in packets] == [8, 9, 8, 9]
    assert [packet[1] for packet in packets] == [7, 7, 9, 9]
    assert packets[-1][4] == 1
    assert all(packet[8] == 0 for packet in packets)


def test_clear_all_snap_packets_touch_only_known_mode_and_partner_bytes():
    packets = MagneticProtocol.clear_snap_slots_packets([8, 9, 8])

    # Duplicates are intentionally collapsed.  The operation writes mode=0
    # then partner=0 only; actuation/RT/dead-zone operations never appear.
    assert [packet[3] for packet in packets] == [8, 9, 8, 9]
    assert [packet[1] for packet in packets] == [7, 7, 9, 9]
    assert [packet[8] for packet in packets] == [0, 0, 0, 0]
    assert [packet[4] for packet in packets] == [0, 0, 0, 1]


def test_clear_all_snap_can_restore_rapid_trigger_mode_without_rewriting_values():
    packets = MagneticProtocol.clear_snap_slots_packets(
        [8, 9], rapid_trigger_slots=[9]
    )

    assert [packet[8] for packet in packets] == [0, 0x80, 0, 0]


def test_keyboard_options_round_trip():
    source = KeyboardOptions(fn_index=2, anti_accidental=True, rt_stab=75, wasd_swap=True)
    packet = MagneticProtocol.keyboard_options_packet(source)
    assert packet[:6] == [9, 0, 2, 1, 3, 1]

    decoded = MagneticProtocol.decode_keyboard_options([0, 0x89, 0, 2, 1, 3, 1])
    assert decoded == source

def test_visual_layout_is_a_75_percent_keyboard_with_real_protocol_slots():
    slots = [slot for row in SK75_VISUAL_LAYOUT for slot, _, _ in row]

    assert len(SK75_VISUAL_LAYOUT) == 6
    assert len(slots) == len(set(slots))
    assert 65 in slots  # physical Fn key


def test_bulk_magnetic_read_decodes_real_per_key_values_without_writes():
    reports = {}

    def add_bytes(operation, values, chunks):
        for index in range(chunks):
            reports[(operation, index)] = [0] + values[index * 64:(index + 1) * 64]

    def add_words(operation, values):
        raw = []
        for value in values:
            raw.extend([value & 0xFF, value >> 8])
        add_bytes(operation, raw, 4)

    modes = [0] * 128
    modes[8] = 0x80
    add_bytes(MagneticProtocol.OP_MODE, modes, 2)
    for operation, value in (
        (MagneticProtocol.OP_ACTUATION, 150),
        (MagneticProtocol.OP_DEACTIVATION, 124),
        (MagneticProtocol.OP_RAPID_PRESS, 15),
        (MagneticProtocol.OP_RAPID_RELEASE, 20),
        (MagneticProtocol.OP_LOWER_DEAD_ZONE, 5),
        (MagneticProtocol.OP_UPPER_DEAD_ZONE, 10),
    ):
        words = [0] * 128
        words[8] = value
        add_words(operation, words)

    decoded = MagneticProtocol.decode_multi_magnetism(reports)
    assert decoded[8] == KeyMagneticSettings(
        1.50, True, 0.15, 0.20, 0.05, 0.10, deactivation=1.25
    )
    assert MagneticProtocol.decode_multi_magnetism_modes(reports)[8] == 0x80

    packet = MagneticProtocol.get_multi_magnetism_packet(MagneticProtocol.OP_MODE, 0)
    assert packet[:4] == [0xE5, 7, 1, 0]
    assert packet[7] == (255 - sum(packet[:7])) & 0xFF


def test_live_travel_test_packets_and_reports_use_the_real_sk75_stream():
    start = MagneticProtocol.magnetism_report_packet(True)
    stop = MagneticProtocol.magnetism_report_packet(False)
    assert start[:8] == [0x1B, 1, 0, 0, 0, 0, 0, 0xE3]
    assert stop[:8] == [0x1B, 0, 0, 0, 0, 0, 0, 0xE4]

    # Firmware 0x0308 is the SK75's 0.01 mm format.  The USB response keeps
    # its report ID, exactly as hidapi returns it on Windows.
    version = MagneticProtocol.decode_usb_version([0, 0x8F, 0, 0, 0, 0, 0, 0, 8, 3])
    assert version == 0x0308
    step = MagneticProtocol.magnetic_travel_step(version)
    assert step == 100
    assert MagneticProtocol.decode_magnetic_travel_report([5, 0x1B, 114, 0], step=step) == 1.14
    assert MagneticProtocol.decode_magnetic_travel_report([0x1B, 50, 1], step=step) == 3.06
    assert MagneticProtocol.decode_magnetic_travel_report([5, 4, 114, 0], step=step) is None


def test_official_sk75_calibration_sequence_and_read_only_progress_packets():
    """Regression-proof the exact V3.1 Womier calibration protocol order."""
    start = MagneticProtocol.calibration_start_packets()
    stop = MagneticProtocol.calibration_stop_packet()

    # Womier's setJiaoZhunKaiGuan(true): minimum on, minimum off, maximum on.
    assert [packet[:2] for packet in start] == [[0x1C, 1], [0x1C, 0], [0x1E, 1]]
    assert stop[:2] == [0x1E, 0]
    assert all(len(packet) == 64 for packet in [*start, stop])
    assert all(packet[7] == (255 - sum(packet[:7])) & 0xFF for packet in [*start, stop])

    reads = MagneticProtocol.calibration_progress_packets()
    assert [packet[:4] for packet in reads] == [
        [0xE5, 0xFE, 1, 0],
        [0xE5, 0xFE, 1, 1],
        [0xE5, 0xFE, 1, 2],
        [0xE5, 0xFE, 1, 3],
    ]
    assert all(packet[7] == (255 - sum(packet[:7])) & 0xFF for packet in reads)


def test_calibration_progress_decodes_raw_matrix_words_and_official_fill_threshold():
    reports = {}
    raw_values = [0] * 128
    raw_values[8] = 150
    raw_values[65] = 300
    packed = []
    for value in raw_values:
        packed.extend([value & 0xFF, value >> 8])
    for chunk_index in range(MagneticProtocol.CALIBRATION_PROGRESS_CHUNKS):
        reports[(MagneticProtocol.OP_CALIBRATION_PROGRESS, chunk_index)] = [0] + packed[
            chunk_index * 64:(chunk_index + 1) * 64
        ]

    decoded = MagneticProtocol.decode_calibration_progress(reports)
    assert decoded[8] == 150
    assert decoded[65] == 300
    assert MagneticProtocol.calibration_completion_raw(0x0308) == 300
    assert MagneticProtocol.calibration_completion_raw(767) == 1
    assert MagneticProtocol.calibration_progress_fraction(150, 0x0308) == 0.5
    assert MagneticProtocol.calibration_progress_fraction(999, 0x0308) == 1.0


def test_current_sk75_official_bounds_are_global_not_calibration_progress_mm():
    """0xFE calibration progress must never become a fictional per-key cap."""
    bounds = MagneticProtocol.official_sk75_settings_bounds(0x0308)

    assert bounds.actuation_min == 0.10
    assert bounds.actuation_max == 3.30
    assert bounds.rapid_min == 0.01
    assert bounds.rapid_max == 2.00
    assert bounds.dead_zone_max == 1.00
    # The official calibration panel treats 294 only as 98% completion, not
    # as a 3.43 mm hardware measurement.
    assert MagneticProtocol.calibration_progress_fraction(294, 0x0308) == 0.98


def test_current_sk75_hid_packets_clamp_to_official_ui_bounds():
    settings = KeyMagneticSettings(
        actuation=3.50,
        rapid_trigger=True,
        rapid_press=2.50,
        rapid_release=2.50,
        lower_dead_zone=1.50,
        upper_dead_zone=1.50,
    )

    bounded = MagneticProtocol.clamp_key_settings_to_official_bounds(settings)
    assert bounded == KeyMagneticSettings(3.30, True, 2.00, 2.00, 1.00, 1.00)

    packets = MagneticProtocol.key_settings_packets(8, settings)
    by_operation = {packet[1]: packet for packet in packets}
    assert by_operation[MagneticProtocol.OP_ACTUATION][8:10] == [0x4A, 1]
    assert by_operation[MagneticProtocol.OP_DEACTIVATION][8:10] == [0x49, 1]
    assert by_operation[MagneticProtocol.OP_RAPID_PRESS][8:10] == [200, 0]
    assert by_operation[MagneticProtocol.OP_RAPID_RELEASE][8:10] == [200, 0]
    assert by_operation[MagneticProtocol.OP_LOWER_DEAD_ZONE][8:10] == [100, 0]
    assert by_operation[MagneticProtocol.OP_UPPER_DEAD_ZONE][8:10] == [100, 0]


def test_legacy_official_bounds_remain_explicit_when_a_version_is_known():
    bounds = MagneticProtocol.official_sk75_settings_bounds(767)

    assert (bounds.actuation_max, bounds.rapid_max, bounds.dead_zone_max) == (
        4.00,
        2.50,
        4.00,
    )
