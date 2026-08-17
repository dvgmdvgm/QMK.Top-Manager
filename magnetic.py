"""SK75 TMR magnetic-switch HID protocol helpers.

The packet layout mirrors the SK75 module shipped with Womier Driver V3.1.
Only targeted ``SET_MULTI_MAGNETISM`` writes are built here: applying a value
to one key never serializes a factory-sized key matrix over the rest of the
keyboard.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


class MagneticProtocolError(ValueError):
    """Raised for magnetic-switch values which firmware cannot represent."""


# (matrix slot, visible key label, HID usage).  Slot 65 is the physical Fn
# switch. It uses a layer marker rather than a regular HID usage, so it has no
# target HID code but remains visible in the selector.
_SK75_SLOT_HIDS: tuple[int, ...] = (
    41, 53, 43, 57, 225, 224, 58, 30, 20, 4, 0, 0,
    59, 31, 26, 22, 29, 227, 60, 32, 8, 7, 27, 226,
    61, 33, 21, 9, 6, 0, 62, 34, 23, 10, 25, 0,
    63, 35, 28, 11, 5, 44, 64, 36, 24, 13, 17, 0,
    65, 37, 12, 14, 16, 0, 66, 38, 18, 15, 54, 0,
    67, 39, 19, 51, 55, 0, 68, 45, 47, 52, 56, 228,
    69, 46, 48, 0, 229, 80, 76, 42, 49, 40, 82, 81,
    74, 77, 75, 78, 0, 79, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0,
)

_HID_LABELS: dict[int, str] = {
    **{code: chr(ord("A") + code - 4) for code in range(4, 30)},
    **{code: str(code - 29) for code in range(30, 39)},
    39: "0", 40: "Enter", 41: "Esc", 42: "Backspace", 43: "Tab",
    44: "Space", 45: "-", 46: "=", 47: "[", 48: "]", 49: "\\",
    51: ";", 52: "'", 53: "`", 54: ",", 55: ".", 56: "/",
    57: "Caps Lock", **{code: f"F{code - 57}" for code in range(58, 70)},
    70: "Print Screen", 71: "Scroll Lock", 72: "Pause", 73: "Insert",
    74: "Home", 75: "Page Up", 76: "Delete", 77: "End", 78: "Page Down",
    79: "→", 80: "←", 81: "↓", 82: "↑", 224: "Left Ctrl",
    225: "Left Shift", 226: "Left Alt", 227: "Left Win",
    228: "Right Ctrl", 229: "Right Shift",
}


@dataclass(frozen=True)
class SK75Key:
    slot: int
    label: str
    hid: int | None

    @property
    def display_name(self) -> str:
        return f"{self.label} · key {self.slot + 1}"


SK75_KEYS: tuple[SK75Key, ...] = tuple(
    SK75Key(slot, _HID_LABELS[hid], hid)
    for slot, hid in enumerate(_SK75_SLOT_HIDS)
    if hid
) + (SK75Key(65, "Fn", None),)
SK75_KEY_BY_SLOT: dict[int, SK75Key] = {key.slot: key for key in SK75_KEYS}
SK75_KEY_BY_HID: dict[int, SK75Key] = {key.hid: key for key in SK75_KEYS if key.hid is not None}

# Visual 75% layout used by the Flet key picker.  Each tuple is
# ``(matrix_slot, label, relative_width)``.  The HID slot map above remains
# authoritative for protocol writes; this is intentionally only presentation.
#
# The row order mirrors the physical SK75 case: its navigation column is
# Delete / Home / End / Page Up / Page Down, while the up arrow sits beside
# Page Down above the three bottom arrows.  Keeping End here is important:
# the old illustration silently omitted a real physical switch from the
# magnetic-key picker.
def _visual_key(hid: int, label: str, width: float = 1.0) -> tuple[int, str, float]:
    return (SK75_KEY_BY_HID[hid].slot, label, width)


SK75_VISUAL_LAYOUT: tuple[tuple[tuple[int, str, float], ...], ...] = (
    (
        _visual_key(41, "Esc", 1.15),
        _visual_key(58, "F1"), _visual_key(59, "F2"), _visual_key(60, "F3"),
        _visual_key(61, "F4"), _visual_key(62, "F5"), _visual_key(63, "F6"),
        _visual_key(64, "F7"), _visual_key(65, "F8"), _visual_key(66, "F9"),
        _visual_key(67, "F10"), _visual_key(68, "F11"), _visual_key(69, "F12"),
        _visual_key(76, "Del"),
    ),
    (
        _visual_key(53, "`"), _visual_key(30, "1"), _visual_key(31, "2"),
        _visual_key(32, "3"), _visual_key(33, "4"), _visual_key(34, "5"),
        _visual_key(35, "6"), _visual_key(36, "7"), _visual_key(37, "8"),
        _visual_key(38, "9"), _visual_key(39, "0"), _visual_key(45, "-"),
        _visual_key(46, "="), _visual_key(42, "Back", 2.0), _visual_key(74, "Home"),
    ),
    (
        _visual_key(43, "Tab", 1.5), _visual_key(20, "Q"), _visual_key(26, "W"),
        _visual_key(8, "E"), _visual_key(21, "R"), _visual_key(23, "T"),
        _visual_key(28, "Y"), _visual_key(24, "U"), _visual_key(12, "I"),
        _visual_key(18, "O"), _visual_key(19, "P"), _visual_key(47, "["),
        _visual_key(48, "]"), _visual_key(49, "\\", 1.5), _visual_key(77, "End"),
    ),
    (
        _visual_key(57, "Caps", 1.75), _visual_key(4, "A"), _visual_key(22, "S"),
        _visual_key(7, "D"), _visual_key(9, "F"), _visual_key(10, "G"),
        _visual_key(11, "H"), _visual_key(13, "J"), _visual_key(14, "K"),
        _visual_key(15, "L"), _visual_key(51, ";"), _visual_key(52, "'"),
        _visual_key(40, "Enter", 2.25), _visual_key(75, "PgUp"),
    ),
    (
        _visual_key(225, "Shift", 2.25), _visual_key(29, "Z"), _visual_key(27, "X"),
        _visual_key(6, "C"), _visual_key(25, "V"), _visual_key(5, "B"),
        _visual_key(17, "N"), _visual_key(16, "M"), _visual_key(54, ","),
        _visual_key(55, "."), _visual_key(56, "/"), _visual_key(229, "R Shift", 2.0),
        _visual_key(82, "↑"), _visual_key(78, "PgDn"),
    ),
    (
        _visual_key(224, "Ctrl", 1.25), _visual_key(227, "Win", 1.25),
        _visual_key(226, "Alt", 1.25), _visual_key(44, "Space", 6.75),
        (65, "Fn", 1.25), _visual_key(228, "Ctrl", 1.25),
        _visual_key(80, "←"), _visual_key(81, "↓"), _visual_key(79, "→"),
    ),
)


def _scaled(value: float, name: str, *, minimum: float = 0.0, maximum: float = 3.5) -> int:
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise MagneticProtocolError(f"{name}: нужно число") from exc
    if not minimum <= value <= maximum:
        raise MagneticProtocolError(f"{name}: допустимо от {minimum:.2f} до {maximum:.2f} мм")
    return round(value * 100)


@dataclass(frozen=True)
class KeyMagneticSettings:
    """A key-scoped set of travel and rapid-trigger values in millimetres."""

    actuation: float
    rapid_trigger: bool
    rapid_press: float
    rapid_release: float
    lower_dead_zone: float
    upper_dead_zone: float
    # Womier calls this normal-mode release threshold ``liftTravel``.  It is
    # intentionally distinct from ``rapid_release`` (``fireLiftTravel``): the
    # latter is only used while Rapid Trigger is enabled.  Keeping a default
    # makes existing six-value presets backwards compatible; their normal
    # release point starts at the same position as actuation until a user
    # changes it explicitly.
    deactivation: float | None = None

    def __post_init__(self) -> None:
        # These are the broad values which the SK75 feature protocol can
        # encode.  The official Womier *UI* deliberately exposes a narrower,
        # firmware-generation-specific range; use
        # ``MagneticProtocol.clamp_key_settings_to_official_bounds`` before a
        # normal UI/HID write.  Keeping the transport representation broad
        # means a read of an older keyboard is never silently discarded.
        _scaled(self.actuation, "Точка активации", minimum=0.1, maximum=4.0)
        deactivation = self.actuation if self.deactivation is None else self.deactivation
        _scaled(deactivation, "Точка деактивации", minimum=0.1, maximum=4.0)
        object.__setattr__(self, "deactivation", float(deactivation))
        _scaled(self.rapid_press, "Порог нажатия RT", minimum=0.01, maximum=2.5)
        _scaled(self.rapid_release, "Порог отпускания RT", minimum=0.01, maximum=2.5)
        # Legacy SK75 firmware represents a wider dead-zone range.  Current
        # UI/HID paths are clamped to 1.00 mm by ``MagneticSettingsBounds``;
        # retaining the protocol envelope here keeps a legacy read decodable.
        _scaled(self.lower_dead_zone, "Нижняя мёртвая зона", maximum=4.0)
        _scaled(self.upper_dead_zone, "Верхняя мёртвая зона", maximum=4.0)


@dataclass(frozen=True)
class MagneticSettingsBounds:
    """One honest set of ranges exposed by the official SK75 driver.

    Calibration reports are intentionally *not* part of this object.  The
    official driver treats their ``0xFE`` values as progress toward a normalised
    sensor reference (300 on current firmware), not as a per-key physical
    millimetre measurement.  A 294 response therefore must never be presented
    as a made-up ``3.43 mm`` hardware limit.
    """

    actuation_min: float
    actuation_max: float
    rapid_min: float
    rapid_max: float
    dead_zone_min: float
    dead_zone_max: float

    @staticmethod
    def _clamp(value: float, minimum: float, maximum: float) -> float:
        return round(max(minimum, min(maximum, float(value))), 2)

    def clamp(self, settings: KeyMagneticSettings) -> KeyMagneticSettings:
        """Return settings that the official UI can actually configure.

        The feature protocol itself accepts a wider number range.  Clamping
        here mirrors the official UI instead of letting a local preset store a
        value the stock driver cannot select afterwards.
        """
        if not isinstance(settings, KeyMagneticSettings):
            raise MagneticProtocolError("некорректные параметры магнитной клавиши")
        return KeyMagneticSettings(
            actuation=self._clamp(
                settings.actuation, self.actuation_min, self.actuation_max
            ),
            rapid_trigger=bool(settings.rapid_trigger),
            rapid_press=self._clamp(
                settings.rapid_press, self.rapid_min, self.rapid_max
            ),
            rapid_release=self._clamp(
                settings.rapid_release, self.rapid_min, self.rapid_max
            ),
            lower_dead_zone=self._clamp(
                settings.lower_dead_zone, self.dead_zone_min, self.dead_zone_max
            ),
            upper_dead_zone=self._clamp(
                settings.upper_dead_zone, self.dead_zone_min, self.dead_zone_max
            ),
            deactivation=self._clamp(
                settings.deactivation, self.actuation_min, self.actuation_max
            ),
        )


@dataclass(frozen=True)
class KeyboardOptions:
    fn_index: int = 0
    anti_accidental: bool = False
    rt_stab: int = 0
    wasd_swap: bool = False
    system: str = "win"

    def __post_init__(self) -> None:
        if not 0 <= self.fn_index <= 255:
            raise MagneticProtocolError("индекс Fn должен быть от 0 до 255")
        if self.rt_stab not in (0, 25, 50, 75, 100, 125):
            raise MagneticProtocolError("RTStab должен быть 0, 25, 50, 75, 100 или 125")
        if self.system not in ("win", "mac", "ios", "android"):
            raise MagneticProtocolError("неизвестная система Fn")


class MagneticProtocol:
    REPORT_SIZE = 64
    SET_KB_OPTION = 0x09
    GET_KB_OPTION = 0x89
    # This is Womier's documented simulation-test switch.  It streams the
    # current magnetic travel through the keyboard's input endpoint; it is
    # deliberately separate from calibration commands and never writes a
    # calibration value.
    SET_MAGNETISM_REPORT = 0x1B
    # Womier Driver V3.1's official SK75 calibration sequence.  Calibration
    # is *not* the normal 0x1B live-travel test: the first two commands reset
    # the minimum reference and the last command starts/stops the maximum
    # travel pass.  Keep the command names explicit so callers cannot confuse
    # the reversible tester with a calibration write.
    SET_MAGNETISM_MINIMUM_CALIBRATION = 0x1C
    SET_MAGNETISM_MAXIMUM_CALIBRATION = 0x1E
    GET_USB_VERSION = 0x8F
    SET_MULTI_MAGNETISM = 0x65
    GET_MULTI_MAGNETISM = 0xE5

    OP_ACTUATION = 0
    # Womier's ordinary-release / deactivation threshold.  It is independent
    # from the Rapid Trigger release threshold (operation 3) and is written
    # even when the Rapid Trigger bit is off.
    OP_DEACTIVATION = 1
    OP_RAPID_PRESS = 2
    OP_RAPID_RELEASE = 3
    OP_LOWER_DEAD_ZONE = 6
    OP_MODE = 7
    OP_SNAP_PARTNER = 9
    OP_UPPER_DEAD_ZONE = 251
    # Womier's calibration panel reads four 64-byte chunks of unsigned
    # 16-bit progress values using GET_MULTI_MAGNETISM operation 254.
    # This request is read-only and is valid both before and during a
    # calibration session.
    OP_CALIBRATION_PROGRESS = 254
    CALIBRATION_PROGRESS_CHUNKS = 4
    CALIBRATION_COMPLETE_RAW = 300
    CALIBRATION_COMPLETE_LEGACY_RAW = 1

    MODE_NORMAL = 0
    MODE_SNAP = 7
    MODE_RAPID_TRIGGER_BIT = 0x80

    _SYSTEM_CODES = {"win": 0, "mac": 1, "ios": 2, "android": 3}

    # These values come from Womier Driver V3.1's magnetic settings state:
    # current SK75 firmware uses a global 3.30 mm travel maximum, RT thresholds
    # of 0.01..2.00 mm and dead zones of 0.00..1.00 mm.  The stock application
    # does *not* read or persist an individual maximum travel after calibration;
    # its 0xFE stream is only a 0..300 calibration-progress meter.
    OFFICIAL_SK75_ACTUATION_MIN_MM = 0.10
    OFFICIAL_SK75_ACTUATION_MAX_MM = 3.30
    OFFICIAL_SK75_RAPID_MIN_MM = 0.01
    OFFICIAL_SK75_RAPID_MAX_MM = 2.00
    OFFICIAL_SK75_DEAD_ZONE_MIN_MM = 0.00
    OFFICIAL_SK75_DEAD_ZONE_MAX_MM = 1.00

    @classmethod
    def official_sk75_settings_bounds(
        cls, firmware_version: int | None = None
    ) -> MagneticSettingsBounds:
        """Return Womier's displayed bounds for this SK75 firmware family.

        Current SK75 firmware is version 768 or newer.  The older branch is
        retained because the official V3.1 JavaScript explicitly uses a 4 mm
        travel/dead-zone scale and 2.5 mm RT cap below that version.  An unknown
        version intentionally takes the current SK75 path: it is the safer
        range and never writes a threshold that the current official UI rejects.
        """
        try:
            version = int(firmware_version) if firmware_version is not None else None
        except (TypeError, ValueError):
            version = None
        if version is not None and version < 768:
            return MagneticSettingsBounds(
                actuation_min=0.10,
                actuation_max=4.00,
                rapid_min=0.01,
                rapid_max=2.50,
                dead_zone_min=0.00,
                dead_zone_max=4.00,
            )
        return MagneticSettingsBounds(
            actuation_min=cls.OFFICIAL_SK75_ACTUATION_MIN_MM,
            actuation_max=cls.OFFICIAL_SK75_ACTUATION_MAX_MM,
            rapid_min=cls.OFFICIAL_SK75_RAPID_MIN_MM,
            rapid_max=cls.OFFICIAL_SK75_RAPID_MAX_MM,
            dead_zone_min=cls.OFFICIAL_SK75_DEAD_ZONE_MIN_MM,
            dead_zone_max=cls.OFFICIAL_SK75_DEAD_ZONE_MAX_MM,
        )

    @classmethod
    def clamp_key_settings_to_official_bounds(
        cls,
        settings: KeyMagneticSettings,
        firmware_version: int | None = None,
    ) -> KeyMagneticSettings:
        """Canonicalise a preset without inferring a fictional per-key cap.

        This is a pure local operation.  It never reads calibration progress,
        never sends HID and does not decide whether a key was calibrated.  The
        caller performs a HID write only as part of an explicit user edit or a
        chosen preset application.
        """
        return cls.official_sk75_settings_bounds(firmware_version).clamp(settings)

    @classmethod
    def _empty_packet(cls) -> list[int]:
        return [0] * cls.REPORT_SIZE

    @staticmethod
    def _checksum(values: Sequence[int]) -> int:
        return (255 - (sum(values) & 0xFF)) & 0xFF

    @classmethod
    def with_bit7_checksum(cls, packet: Sequence[int]) -> list[int]:
        result = list(packet)
        if len(result) != cls.REPORT_SIZE:
            raise MagneticProtocolError("HID-пакет должен содержать 64 байта")
        result[7] = cls._checksum(result[:7])
        return result

    @staticmethod
    def _key_slot(slot: int) -> int:
        if slot not in SK75_KEY_BY_SLOT:
            raise MagneticProtocolError("выберите клавишу SK75 из списка")
        return slot

    @classmethod
    def _simple_packet(cls, operation: int, slot: int, is_final: bool, data: Sequence[int]) -> list[int]:
        cls._key_slot(slot)
        if not 0 <= operation <= 255:
            raise MagneticProtocolError("неизвестная команда магнитной клавиши")
        if len(data) > cls.REPORT_SIZE - 8 or any(not 0 <= int(value) <= 255 for value in data):
            raise MagneticProtocolError("некорректные данные магнитной команды")
        packet = cls._empty_packet()
        packet[0] = cls.SET_MULTI_MAGNETISM
        packet[1] = operation
        packet[2] = 0  # simple one-key command
        packet[3] = slot
        packet[4] = int(is_final)
        packet[8:8 + len(data)] = [int(value) for value in data]
        return cls.with_bit7_checksum(packet)

    @classmethod
    def key_settings_packets(
        cls,
        slot: int,
        settings: KeyMagneticSettings,
        *,
        firmware_version: int | None = None,
    ) -> list[list[int]]:
        """Build writes for one key only; other keys are never reset."""
        # Mirror the values offered by the official current-SK75 UI even when a
        # stale local JSON preset contains protocol-representable 3.50 mm / RT
        # 3.50 mm values.  This is a packet-only guard; application startup
        # never calls this method, so loading an old config cannot write HID.
        settings = cls.clamp_key_settings_to_official_bounds(
            settings, firmware_version
        )
        bounds = cls.official_sk75_settings_bounds(firmware_version)
        actuation = _scaled(
            settings.actuation,
            "Точка активации",
            minimum=bounds.actuation_min,
            maximum=bounds.actuation_max,
        )
        # Womier's `liftTravel` operation uses the stock driver's offset
        # representation: raw 9 means 0.10 mm, raw 119 means 1.20 mm.  This
        # is deliberately kept local to operation 1; the older operations
        # retain their established app-side encoding.
        deactivation = _scaled(
            settings.deactivation,
            "Точка деактивации",
            minimum=bounds.actuation_min,
            maximum=bounds.actuation_max,
        ) - 1
        rapid_press = _scaled(
            settings.rapid_press,
            "Порог нажатия RT",
            minimum=bounds.rapid_min,
            maximum=bounds.rapid_max,
        )
        rapid_release = _scaled(
            settings.rapid_release,
            "Порог отпускания RT",
            minimum=bounds.rapid_min,
            maximum=bounds.rapid_max,
        )
        lower_zone = _scaled(
            settings.lower_dead_zone,
            "Нижняя мёртвая зона",
            maximum=bounds.dead_zone_max,
        )
        upper_zone = _scaled(
            settings.upper_dead_zone,
            "Верхняя мёртвая зона",
            maximum=bounds.dead_zone_max,
        )
        option = cls.MODE_NORMAL | (cls.MODE_RAPID_TRIGGER_BIT if settings.rapid_trigger else 0)
        commands: list[tuple[int, list[int]]] = [
            (cls.OP_MODE, [option]),
            (cls.OP_ACTUATION, [actuation & 0xFF, actuation >> 8]),
            (cls.OP_DEACTIVATION, [deactivation & 0xFF, deactivation >> 8]),
        ]
        if settings.rapid_trigger:
            commands.extend(
                (
                    (cls.OP_RAPID_PRESS, [rapid_press & 0xFF, rapid_press >> 8]),
                    (cls.OP_RAPID_RELEASE, [rapid_release & 0xFF, rapid_release >> 8]),
                )
            )
        commands.extend(
            (
                (cls.OP_LOWER_DEAD_ZONE, [lower_zone & 0xFF, lower_zone >> 8]),
                (cls.OP_UPPER_DEAD_ZONE, [upper_zone & 0xFF, upper_zone >> 8]),
            )
        )
        return [
            cls._simple_packet(operation, slot, index == len(commands) - 1, data)
            for index, (operation, data) in enumerate(commands)
        ]

    @classmethod
    def snap_pair_packets(cls, first_slot: int, second_slot: int) -> list[list[int]]:
        """Bind two keys as Snap Key, touching only those two matrix slots."""
        cls._key_slot(first_slot)
        cls._key_slot(second_slot)
        if first_slot == second_slot:
            raise MagneticProtocolError("для Snap Key нужны две разные клавиши")
        return [
            cls._simple_packet(cls.OP_MODE, first_slot, False, [cls.MODE_SNAP]),
            cls._simple_packet(cls.OP_MODE, second_slot, False, [cls.MODE_SNAP]),
            cls._simple_packet(cls.OP_SNAP_PARTNER, first_slot, False, [second_slot]),
            cls._simple_packet(cls.OP_SNAP_PARTNER, second_slot, True, [first_slot]),
        ]

    @classmethod
    def clear_snap_pair_packets(cls, first_slot: int, second_slot: int) -> list[list[int]]:
        """Remove a Snap pair without rewriting any other keyboard settings."""
        cls._key_slot(first_slot)
        cls._key_slot(second_slot)
        if first_slot == second_slot:
            raise MagneticProtocolError("выберите две разные клавиши")
        return [
            cls._simple_packet(cls.OP_MODE, first_slot, False, [cls.MODE_NORMAL]),
            cls._simple_packet(cls.OP_MODE, second_slot, False, [cls.MODE_NORMAL]),
            cls._simple_packet(cls.OP_SNAP_PARTNER, first_slot, False, [0]),
            cls._simple_packet(cls.OP_SNAP_PARTNER, second_slot, True, [0]),
        ]

    @classmethod
    def clear_snap_slots_packets(
        cls,
        slots: Sequence[int],
        *,
        rapid_trigger_slots: Sequence[int] = (),
    ) -> list[list[int]]:
        """Remove Snap Key only from the supplied, known matrix slots.

        A keyboard can retain an old Snap marker after an interrupted pairing
        attempt, and the firmware's bulk mode read does not expose the partner
        mapping.  Clearing a slot therefore writes *only* its mode and partner
        byte; all of its actuation, RT and dead-zone values stay untouched.
        """
        unique_slots: list[int] = []
        for raw_slot in slots:
            slot = int(raw_slot)
            cls._key_slot(slot)
            if slot not in unique_slots:
                unique_slots.append(slot)
        if not unique_slots:
            return []

        rapid_slots = {int(slot) for slot in rapid_trigger_slots}
        commands = [
            (
                cls.OP_MODE,
                slot,
                [
                    cls.MODE_NORMAL
                    | (cls.MODE_RAPID_TRIGGER_BIT if slot in rapid_slots else 0)
                ],
            )
            for slot in unique_slots
        ] + [
            (cls.OP_SNAP_PARTNER, slot, [0]) for slot in unique_slots
        ]
        return [
            cls._simple_packet(operation, slot, index == len(commands) - 1, data)
            for index, (operation, slot, data) in enumerate(commands)
        ]

    @classmethod
    def get_multi_magnetism_packet(cls, operation: int, chunk_index: int) -> list[int]:
        """Build one read-only chunk request for the SK75 magnetic matrix.

        The V3.1 driver reads 128 matrix slots in 64-byte chunks.  ``operation``
        is a firmware field (0 = actuation, 1 = normal-mode deactivation,
        2/3 = RT thresholds, 6/251 = dead zones, 7 = key mode).  The caller only sends these packets via a HID
        feature read, so this method cannot change a keyboard setting.
        """
        if not 0 <= operation <= 255 or not 0 <= chunk_index <= 255:
            raise MagneticProtocolError("некорректная команда чтения магнитных клавиш")
        packet = cls._empty_packet()
        packet[0] = cls.GET_MULTI_MAGNETISM
        packet[1] = operation
        packet[2] = 1  # matrix/bulk read, as used by Womier Driver V3.1
        packet[3] = chunk_index
        return cls.with_bit7_checksum(packet)

    @staticmethod
    def _response_payload(report: Sequence[int], operation: int) -> list[int]:
        """Normalise hidapi's optional report-id byte and return 64 payload bytes."""
        values = list(report)
        # SK75 returns raw matrix bytes (the command header is not echoed),
        # while hidapi prepends a zero report-id byte on Windows.
        if len(values) == 65 and values[0] == 0:
            values = values[1:]
        if len(values) < 64:
            raise MagneticProtocolError("неполный ответ магнитной матрицы")
        return values[:64]

    @classmethod
    def decode_multi_magnetism(cls, reports: dict[tuple[int, int], Sequence[int]]) -> dict[int, KeyMagneticSettings]:
        """Decode the V3.1 bulk-read responses into settings for physical SK75 keys.

        ``reports`` is keyed by ``(operation, chunk_index)``.  Missing or zero
        values are deliberately skipped: a blank response must never overwrite
        a known keyboard value with made-up defaults.
        """
        def bytes_for(operation: int, chunk_count: int) -> list[int]:
            values: list[int] = []
            for chunk_index in range(chunk_count):
                report = reports.get((operation, chunk_index))
                if report is None:
                    raise MagneticProtocolError("неполный ответ магнитной матрицы")
                values.extend(cls._response_payload(report, operation))
            return values

        def words_for(operation: int) -> list[int]:
            values = bytes_for(operation, 4)
            return [values[index] | (values[index + 1] << 8) for index in range(0, 256, 2)]

        modes = bytes_for(cls.OP_MODE, 2)
        actuation = words_for(cls.OP_ACTUATION)
        deactivation = words_for(cls.OP_DEACTIVATION)
        rapid_press = words_for(cls.OP_RAPID_PRESS)
        rapid_release = words_for(cls.OP_RAPID_RELEASE)
        lower_dead_zone = words_for(cls.OP_LOWER_DEAD_ZONE)
        upper_dead_zone = words_for(cls.OP_UPPER_DEAD_ZONE)

        decoded: dict[int, KeyMagneticSettings] = {}
        for slot in SK75_KEY_BY_SLOT:
            if slot >= len(modes) or slot >= len(actuation):
                continue
            # A zero actuation word is a blank/unavailable slot in the SK75
            # matrix, not a valid setting (the firmware minimum is 0.10 mm).
            if actuation[slot] < 10:
                continue
            try:
                # Old local builds did not write operation 1.  A zero word
                # from such a keyboard is not a valid normal-mode point, so
                # fall back to its known activation threshold rather than
                # manufacturing an impossible 0.00 mm value.
                normal_deactivation = (
                    (deactivation[slot] + 1) / 100
                    if deactivation[slot] >= 9
                    else actuation[slot] / 100
                )
                decoded[slot] = KeyMagneticSettings(
                    actuation=actuation[slot] / 100,
                    rapid_trigger=bool(modes[slot] & cls.MODE_RAPID_TRIGGER_BIT),
                    rapid_press=max(rapid_press[slot], 1) / 100,
                    rapid_release=max(rapid_release[slot], 1) / 100,
                    lower_dead_zone=min(lower_dead_zone[slot], 100) / 100,
                    upper_dead_zone=min(upper_dead_zone[slot], 100) / 100,
                    deactivation=normal_deactivation,
                )
            except MagneticProtocolError:
                continue
        return decoded

    @classmethod
    def decode_multi_magnetism_modes(
        cls, reports: dict[tuple[int, int], Sequence[int]]
    ) -> dict[int, int]:
        """Return the raw per-key mode bytes from a bulk matrix read.

        The low seven bits distinguish normal, Snap Key, DKS and other
        advanced modes; the high bit enables Rapid Trigger.  Keeping this
        information prevents a normal per-key edit from accidentally turning
        a Snap Key back into a normal key.
        """
        values: list[int] = []
        for chunk_index in range(2):
            report = reports.get((cls.OP_MODE, chunk_index))
            if report is None:
                raise MagneticProtocolError("неполный ответ режимов магнитной матрицы")
            values.extend(cls._response_payload(report, cls.OP_MODE))
        return {
            slot: values[slot]
            for slot in SK75_KEY_BY_SLOT
            if slot < len(values)
        }

    @classmethod
    def get_keyboard_options_packet(cls) -> list[int]:
        packet = cls._empty_packet()
        packet[0] = cls.GET_KB_OPTION
        return cls.with_bit7_checksum(packet)

    @classmethod
    def magnetism_report_packet(cls, enabled: bool) -> list[int]:
        """Start or stop Womier's live magnetic-travel test stream.

        The firmware command is a reversible test-mode toggle.  Callers must
        always send ``False`` when their reader stops, including on errors.
        """
        packet = cls._empty_packet()
        packet[0] = cls.SET_MAGNETISM_REPORT
        packet[1] = int(bool(enabled))
        return cls.with_bit7_checksum(packet)

    @classmethod
    def _calibration_toggle_packet(cls, command: int, enabled: bool) -> list[int]:
        """Build one official, explicitly scoped calibration toggle packet.

        These packets are intentionally private: callers should use
        :meth:`calibration_start_packets` or :meth:`calibration_stop_packet`
        so the required minimum-reference reset cannot be accidentally
        omitted.  The command structure comes directly from Womier Driver
        V3.1's ``setJiaoZhunKaiGuan`` implementation.
        """
        if command not in (
            cls.SET_MAGNETISM_MINIMUM_CALIBRATION,
            cls.SET_MAGNETISM_MAXIMUM_CALIBRATION,
        ):
            raise MagneticProtocolError("неизвестная команда калибровки")
        packet = cls._empty_packet()
        packet[0] = command
        packet[1] = int(bool(enabled))
        return cls.with_bit7_checksum(packet)

    @classmethod
    def calibration_start_packets(cls) -> list[list[int]]:
        """Return Womier SK75's exact, ordered calibration-start sequence.

        The official driver sends ``0x1C/1``, ``0x1C/0``, then ``0x1E/1``.
        The first two packets establish the minimum reference; the final
        packet starts the maximum-travel pass.  They must be sent serially on
        the same exclusive diagnostic session, never interleaved with normal
        magnetic setting writes.
        """
        return [
            cls._calibration_toggle_packet(cls.SET_MAGNETISM_MINIMUM_CALIBRATION, True),
            cls._calibration_toggle_packet(cls.SET_MAGNETISM_MINIMUM_CALIBRATION, False),
            cls._calibration_toggle_packet(cls.SET_MAGNETISM_MAXIMUM_CALIBRATION, True),
        ]

    @classmethod
    def calibration_stop_packet(cls) -> list[int]:
        """Return Womier SK75's official calibration-stop packet (``0x1E/0``)."""
        return cls._calibration_toggle_packet(cls.SET_MAGNETISM_MAXIMUM_CALIBRATION, False)

    @classmethod
    def calibration_progress_packet(cls, chunk_index: int) -> list[int]:
        """Build one *read-only* calibration-progress request.

        The normal ``GET_MULTI_MAGNETISM`` packet layout is shared with
        magnetic settings.  This dedicated name makes it clear that operation
        ``0xFE`` exposes current calibration levels rather than rewriting a
        key or opening the 0x1B travel-report stream.
        """
        if not 0 <= int(chunk_index) < cls.CALIBRATION_PROGRESS_CHUNKS:
            raise MagneticProtocolError("некорректный фрагмент калибровки")
        return cls.get_multi_magnetism_packet(
            cls.OP_CALIBRATION_PROGRESS, int(chunk_index)
        )

    @classmethod
    def calibration_progress_packets(cls) -> list[list[int]]:
        """Return all four read-only SK75 calibration-progress requests."""
        return [
            cls.calibration_progress_packet(chunk_index)
            for chunk_index in range(cls.CALIBRATION_PROGRESS_CHUNKS)
        ]

    @classmethod
    def decode_calibration_progress(
        cls, reports: dict[tuple[int, int], Sequence[int]]
    ) -> dict[int, int]:
        """Decode raw per-key calibration levels for the visible SK75 keys.

        Womier's calibration UI does not receive a key ID in an input stream.
        Instead it polls four feature responses for ``0xE5/0xFE`` and maps the
        128 unsigned 16-bit words back to the keyboard matrix.  A raw level of
        300 completes a current-firmware key (1 on legacy firmware); consumers
        can convert it to a fill fraction with
        :meth:`calibration_progress_fraction`.

        Missing chunks are errors on purpose: a partial response must never
        paint a key as calibrated just because its absent bytes look like zero.
        """
        raw_bytes: list[int] = []
        for chunk_index in range(cls.CALIBRATION_PROGRESS_CHUNKS):
            report = reports.get((cls.OP_CALIBRATION_PROGRESS, chunk_index))
            if report is None:
                raise MagneticProtocolError("неполный ответ калибровки")
            raw_bytes.extend(
                cls._response_payload(report, cls.OP_CALIBRATION_PROGRESS)
            )
        words = [
            raw_bytes[index] | (raw_bytes[index + 1] << 8)
            for index in range(0, 256, 2)
        ]
        return {
            slot: words[slot]
            for slot in SK75_KEY_BY_SLOT
            if slot < len(words)
        }

    @classmethod
    def calibration_completion_raw(cls, firmware_version: int | None) -> int:
        """Return the completion threshold used by the official Womier UI."""
        try:
            version = int(firmware_version) if firmware_version is not None else None
        except (TypeError, ValueError):
            version = None
        return (
            cls.CALIBRATION_COMPLETE_LEGACY_RAW
            if version is not None and version < 768
            else cls.CALIBRATION_COMPLETE_RAW
        )

    @classmethod
    def calibration_progress_fraction(
        cls, raw_value: int | float, firmware_version: int | None = None
    ) -> float:
        """Clamp one raw calibration level to the official 0..1 cap fill."""
        try:
            raw = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise MagneticProtocolError("некорректное значение калибровки") from exc
        if raw < 0:
            raise MagneticProtocolError("некорректное значение калибровки")
        complete = cls.calibration_completion_raw(firmware_version)
        return max(0.0, min(1.0, raw / complete))

    @classmethod
    def get_usb_version_packet(cls) -> list[int]:
        """Build the read-only firmware-version request used for travel scale."""
        packet = cls._empty_packet()
        packet[0] = cls.GET_USB_VERSION
        return cls.with_bit7_checksum(packet)

    @classmethod
    def decode_usb_version(cls, report: Sequence[int]) -> int:
        """Decode Womier's USB firmware number without guessing its scale."""
        values = list(report)
        # hidapi on Windows prepends report ID 0 to a feature response.
        if len(values) >= 2 and values[0] == 0 and values[1] == cls.GET_USB_VERSION:
            values = values[1:]
        if len(values) < 9 or values[0] != cls.GET_USB_VERSION:
            raise MagneticProtocolError("ответ версии прошивки не распознан")
        return values[7] | (values[8] << 8)

    @staticmethod
    def magnetic_travel_step(firmware_version: int | None) -> int:
        """Return raw magnetic-travel units per millimetre for this firmware."""
        if firmware_version is None:
            # SK75 firmware normally uses 0.01 mm.  This is also safer than
            # reporting a falsely precise value if the read-only version query
            # is unavailable on an older board.
            return 100
        if 768 <= firmware_version < 1280:
            return 100
        if firmware_version >= 1280:
            return 200
        return 10

    @classmethod
    def decode_magnetic_travel_report(
        cls, report: Sequence[int], *, step: int
    ) -> float | None:
        """Decode a live-test input report into millimetres.

        SK75's input endpoint normally returns ``[5, 0x1B, low, high, ...]``.
        Some hidapi backends remove the report ID, so the short
        ``[0x1B, low, high, ...]`` form is accepted too.  Unrelated keyboard
        input reports are ignored rather than converted into fake movement.
        """
        if step <= 0:
            raise MagneticProtocolError("некорректный шаг хода магнитной клавиши")
        # ``hid.device.read()`` already returns an indexable list.  This
        # decoder is used by the live 0x1B path, so avoid copying every report
        # before inspecting its few header/data bytes.  ``Sequence`` keeps
        # the same safe indexing behaviour for bytes, tuples and test input.
        values = report
        payload_offset = None
        if len(values) >= 4 and values[1] == cls.SET_MAGNETISM_REPORT:
            payload_offset = 1
        elif len(values) >= 3 and values[0] == cls.SET_MAGNETISM_REPORT:
            payload_offset = 0
        if payload_offset is None:
            return None
        low = values[payload_offset + 1]
        high = values[payload_offset + 2]
        raw = low if step == 10 else low | (high << 8)
        return raw / step

    @classmethod
    def keyboard_options_packet(cls, options: KeyboardOptions) -> list[int]:
        packet = cls._empty_packet()
        packet[0] = cls.SET_KB_OPTION
        packet[2] = options.fn_index
        packet[3] = int(options.anti_accidental)
        packet[4] = options.rt_stab // 25
        packet[5] = int(options.wasd_swap)
        return cls.with_bit7_checksum(packet)

    @classmethod
    def decode_keyboard_options(cls, report: Sequence[int]) -> KeyboardOptions:
        values = list(report)
        if len(values) >= 2 and values[0] == 0 and values[1] == cls.GET_KB_OPTION:
            values = values[1:]
        if len(values) < 6 or values[0] != cls.GET_KB_OPTION:
            raise MagneticProtocolError("ответ KB Option не распознан")
        systems = {0: "win", 1: "mac", 2: "ios", 3: "android"}
        return KeyboardOptions(
            system=systems.get(values[1], "win"),
            fn_index=values[2],
            anti_accidental=bool(values[3]),
            rt_stab=(values[4] if values[4] <= 5 else 0) * 25,
            wasd_swap=values[5] == 1,
        )
