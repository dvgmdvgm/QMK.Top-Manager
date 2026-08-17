"""Lighting protocol and colour helpers for Rongyuan/Womier keyboards.

The SK75 TMR exposes its lighting settings through 64-byte HID feature
reports.  The protocol used here was verified with a non-mutating
``GET_LEDPARAM`` request on VID 0x3151 / PID 0x5030 (SK75 TMR).

Keeping the packet construction out of the Flet UI makes it possible to test
the risky part of the feature without a keyboard attached.
"""
from __future__ import annotations

from colorsys import hsv_to_rgb as _hsv_to_rgb
from colorsys import rgb_to_hsv as _rgb_to_hsv
from dataclasses import dataclass
from typing import Iterable, Sequence

RGB = tuple[int, int, int]

# These are deliberately only *UI fallbacks* for a brand-new local entry.
# They are never sent to the keyboard merely because an entry was created:
# the application issues a read-only ``GET_LEDPARAM`` query after a keyboard
# is discovered and replaces this preview with the firmware's actual state.
#
# Keep the fallback visibly neutral and generic.  A publisher's selected pink
# colour or animated effect must not leak into a public first-run experience
# when the keyboard is temporarily unavailable for that readback.
NEUTRAL_LIGHTING_EFFECT = 1  # "Постоянный"
NEUTRAL_LIGHTING_COLOR: RGB = (255, 255, 255)
NEUTRAL_LIGHTING_COLOR_HEX = "#FFFFFF"
NEUTRAL_LIGHTING_BRIGHTNESS = 2  # 3 / 5 in the UI
NEUTRAL_LIGHTING_SPEED = 2


class LightingProtocolError(ValueError):
    """Raised when a lighting value cannot be represented by the protocol."""


# Values used by the firmware's FEA_CMD_SET_LEDPARAM command.
EFFECTS: dict[int, str] = {
    0: "Выключено",
    1: "Постоянный",
    2: "Дыхание",
    3: "Неон",
    4: "Волна",
    5: "Рябь",
    6: "Капли дождя",
    7: "Змейка",
    8: "Реакция на нажатие",
    9: "Схождение",
    10: "Синусоида",
    11: "Калейдоскоп",
    12: "Линейная волна",
    14: "Лазер",
    15: "Круговая волна",
    16: "Мерцание",
    17: "Дождь",
    18: "Метеор",
    19: "След от нажатия",
    20: "Музыка",
    21: "Цвет экрана",
    22: "Музыка (режим 2)",
    23: "Поезд",
    24: "Фейерверк",
}

EFFECT_OPTIONS: dict[int, tuple[str, ...]] = {
    4: ("Вправо", "Влево", "Вниз", "Вверх"),
    7: ("Зигзаг", "По кругу"),
    11: ("Наружу", "Внутрь"),
    12: ("Вправо", "Влево"),
    15: ("Против часовой", "По часовой"),
}


def _byte(value: int, name: str) -> int:
    if not isinstance(value, int) or not 0 <= value <= 255:
        raise LightingProtocolError(f"{name} must be an integer from 0 to 255")
    return value


def normalize_rgb(color: Iterable[int]) -> RGB:
    values = tuple(color)
    if len(values) != 3:
        raise LightingProtocolError("RGB color must have exactly three channels")
    return (_byte(values[0], "red"), _byte(values[1], "green"), _byte(values[2], "blue"))


def parse_hex_color(value: str) -> RGB:
    """Parse ``#RRGGBB`` (or ``RRGGBB``) into an RGB tuple."""
    text = (value or "").strip().lstrip("#")
    if len(text) != 6:
        raise LightingProtocolError("color must be in #RRGGBB format")
    try:
        return normalize_rgb((int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)))
    except ValueError as exc:
        raise LightingProtocolError("color must be in #RRGGBB format") from exc


def rgb_to_hex(color: Iterable[int]) -> str:
    red, green, blue = normalize_rgb(color)
    return f"#{red:02X}{green:02X}{blue:02X}"


def rgb_to_hsv_degrees(color: Iterable[int]) -> tuple[float, float, float]:
    """Convert RGB to picker-friendly hue degrees, saturation and value."""
    red, green, blue = normalize_rgb(color)
    hue, saturation, value = _rgb_to_hsv(red / 255, green / 255, blue / 255)
    return (hue * 360.0) % 360.0, saturation, value


def hsv_degrees_to_rgb(hue_degrees: float, saturation: float, value: float) -> RGB:
    """Convert the local colour-picker values back to a validated RGB triplet.

    The interactive picker can report coordinates a fraction beyond its edge
    while the pointer is being dragged.  Clamp saturation/value here so the
    UI stays stable instead of raising an exception during a drag event.
    """
    try:
        hue = float(hue_degrees) % 360.0
        saturation = max(0.0, min(1.0, float(saturation)))
        value = max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError) as exc:
        raise LightingProtocolError("HSV values must be numeric") from exc
    red, green, blue = _hsv_to_rgb(hue / 360.0, saturation, value)
    return normalize_rgb((round(red * 255), round(green * 255), round(blue * 255)))


def picker_position_to_sv(x: float, y: float, width: float, height: float) -> tuple[float, float]:
    """Map a pointer location in the saturation/value square to HSV values."""
    try:
        width = float(width)
        height = float(height)
        x = float(x)
        y = float(y)
    except (TypeError, ValueError) as exc:
        raise LightingProtocolError("picker coordinates must be numeric") from exc
    if width <= 0 or height <= 0:
        raise LightingProtocolError("picker dimensions must be positive")
    saturation = max(0.0, min(1.0, x / width))
    value = 1.0 - max(0.0, min(1.0, y / height))
    return saturation, value


@dataclass(frozen=True)
class LightingSettings:
    effect: int = NEUTRAL_LIGHTING_EFFECT
    color: RGB = NEUTRAL_LIGHTING_COLOR
    brightness: int = NEUTRAL_LIGHTING_BRIGHTNESS
    speed: int = NEUTRAL_LIGHTING_SPEED
    option: int = 0
    rainbow: bool = False

    def __post_init__(self) -> None:
        if self.effect not in EFFECTS:
            raise LightingProtocolError(f"unknown lighting effect: {self.effect}")
        normalize_rgb(self.color)
        if not 0 <= self.brightness <= 4:
            raise LightingProtocolError("brightness must be between 0 and 4")
        if not 0 <= self.speed <= 4:
            raise LightingProtocolError("speed must be between 0 and 4")
        if not 0 <= self.option <= 15:
            raise LightingProtocolError("effect option must be between 0 and 15")

    def to_config(self) -> dict[str, int | str | bool]:
        return {
            "effect": self.effect,
            "color": rgb_to_hex(self.color),
            "brightness": self.brightness,
            "speed": self.speed,
            "option": self.option,
            "rainbow": self.rainbow,
        }

    @classmethod
    def from_config(cls, value: object) -> "LightingSettings":
        if not isinstance(value, dict):
            return cls()
        try:
            return cls(
                effect=int(value.get("effect", NEUTRAL_LIGHTING_EFFECT)),
                color=parse_hex_color(str(value.get("color", NEUTRAL_LIGHTING_COLOR_HEX))),
                brightness=int(value.get("brightness", NEUTRAL_LIGHTING_BRIGHTNESS)),
                speed=int(value.get("speed", NEUTRAL_LIGHTING_SPEED)),
                option=int(value.get("option", 0)),
                rainbow=bool(value.get("rainbow", False)),
            )
        except (TypeError, ValueError, LightingProtocolError):
            return cls()


class RongyuanLightingProtocol:
    """Builder for the feature reports used by SK75 TMR lighting firmware."""

    REPORT_SIZE = 64
    GET_LEDPARAM = 0x87
    SET_LEDPARAM = 0x07
    MAX_SPEED = 4
    NORMAL_COLOR_MODE = 7
    DAZZLE_COLOR_MODE = 8

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
            raise LightingProtocolError("feature report must be exactly 64 bytes")
        result[7] = cls._checksum(result[:7])
        return result

    @classmethod
    def with_bit8_checksum(cls, packet: Sequence[int]) -> list[int]:
        result = list(packet)
        if len(result) != cls.REPORT_SIZE:
            raise LightingProtocolError("feature report must be exactly 64 bytes")
        result[8] = cls._checksum(result[:8])
        return result

    @classmethod
    def get_settings_packet(cls) -> list[int]:
        packet = cls._empty_packet()
        packet[0] = cls.GET_LEDPARAM
        return cls.with_bit7_checksum(packet)

    @classmethod
    def settings_packet(cls, settings: LightingSettings) -> list[int]:
        packet = cls._empty_packet()
        red, green, blue = normalize_rgb(settings.color)
        packet[0] = cls.SET_LEDPARAM
        packet[1] = settings.effect
        packet[2] = cls.MAX_SPEED - settings.speed
        packet[3] = settings.brightness
        color_mode = cls.DAZZLE_COLOR_MODE if settings.rainbow else cls.NORMAL_COLOR_MODE
        packet[4] = ((settings.option & 0x0F) << 4) | color_mode
        packet[5:8] = [red, green, blue]
        return cls.with_bit8_checksum(packet)

    @classmethod
    def decode_settings(cls, report: Sequence[int]) -> LightingSettings:
        """Decode a GET_LEDPARAM response; accepts reports with or without report ID."""
        values = list(report)
        if len(values) >= 2 and values[0] == 0 and values[1] == cls.GET_LEDPARAM:
            values = values[1:]
        if len(values) < 8 or values[0] != cls.GET_LEDPARAM:
            raise LightingProtocolError("not a GET_LEDPARAM response")
        mode = values[4] & 0x0F
        return LightingSettings(
            effect=values[1] if values[1] in EFFECTS else 0,
            color=normalize_rgb(values[5:8]),
            brightness=min(4, values[3]),
            speed=max(0, min(cls.MAX_SPEED, cls.MAX_SPEED - values[2])),
            option=values[4] >> 4,
            rainbow=mode == cls.DAZZLE_COLOR_MODE,
        )
