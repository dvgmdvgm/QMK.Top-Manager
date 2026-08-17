import threading
from typing import Callable, Optional

import pystray
from PIL import Image, ImageDraw, ImageFont

from battery import BatteryState


_ICON_SIZE = 32

_OUTLINE_COLOR = (220, 220, 220, 255)
_GREY_COLOR = (140, 140, 140, 255)
_GREEN = (60, 179, 113, 255)    # >=50
_YELLOW = (229, 165, 10, 255)   # 20..49
_RED = (208, 68, 55, 255)       # <20
_USB = (83, 177, 255, 255)

_BASE_ICON_PATH: Optional[str] = None
_BASE_ICON_CACHE: Optional[Image.Image] = None


def set_icon_source(path: Optional[str]) -> None:
    """Set a base icon (e.g. .ico) used as the tray window icon, with the
    battery indicator composited as a small badge over the bottom-right."""
    global _BASE_ICON_PATH, _BASE_ICON_CACHE
    _BASE_ICON_PATH = path
    _BASE_ICON_CACHE = None


def _load_base_icon() -> Optional[Image.Image]:
    global _BASE_ICON_CACHE
    if _BASE_ICON_CACHE is not None:
        return _BASE_ICON_CACHE
    if not _BASE_ICON_PATH:
        return None
    try:
        img = Image.open(_BASE_ICON_PATH).convert("RGBA")
        _BASE_ICON_CACHE = img
        return img
    except Exception:
        return None


def _fill_color(percent: int) -> tuple:
    if percent >= 50:
        return _GREEN
    if percent >= 20:
        return _YELLOW
    return _RED


def render_battery_image(
    state: BatteryState, *, transport: Optional[str] = None
) -> Image.Image:
    return _render_battery_badge(state, badge_size=64, transport=transport)


def _render_usb_badge(badge_size: int) -> Image.Image:
    """Draw a compact blue USB trident for a confirmed wired connection."""
    img = Image.new("RGBA", (badge_size, badge_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    scale = badge_size / 32

    def p(x, y):
        return int(x * scale), int(y * scale)

    width = max(1, int(2 * scale))
    # Stem and plug.
    draw.line([p(16, 28), p(16, 12)], fill=_USB, width=width)
    draw.rectangle((*p(12, 23), *p(20, 29)), outline=_USB, width=width)
    # Up arrow, circle and square branches: conventional USB trident shape.
    draw.line([p(16, 12), p(16, 5)], fill=_USB, width=width)
    draw.polygon([p(16, 3), p(13, 7), p(19, 7)], fill=_USB)
    draw.line([p(16, 16), p(8, 11)], fill=_USB, width=width)
    draw.ellipse((*p(5, 8), *p(9, 12)), outline=_USB, width=width)
    draw.line([p(16, 19), p(24, 14)], fill=_USB, width=width)
    draw.rectangle((*p(23, 11), *p(27, 15)), outline=_USB, width=width)
    return img


def _render_battery_badge(
    state: BatteryState,
    badge_size: int = _ICON_SIZE,
    *,
    transport: Optional[str] = None,
) -> Image.Image:
    if transport == "wired" and (state.is_stale or state.percent is None):
        return _render_usb_badge(badge_size)
    img = Image.new("RGBA", (badge_size, badge_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    scale = badge_size / 32

    def s(*vals):
        return tuple(int(v * scale) for v in vals)

    body = s(1, 6, 27, 26)
    nub = s(27, 11, 31, 21)
    inner_left, inner_top, inner_right, inner_bottom = 3 * scale, 8 * scale, 25 * scale, 24 * scale

    if state.is_stale or state.percent is None:
        draw.rounded_rectangle(body, radius=max(1, int(3 * scale)), outline=_GREY_COLOR, width=max(1, int(2 * scale)))
        draw.rectangle(nub, fill=_GREY_COLOR)
        try:
            font = ImageFont.truetype("arial.ttf", int(14 * scale))
        except OSError:
            font = ImageFont.load_default()
        draw.text(s(11, 7), "?", fill=_GREY_COLOR, font=font)
        return img

    draw.rounded_rectangle(body, radius=max(1, int(3 * scale)), outline=_OUTLINE_COLOR, width=max(1, int(2 * scale)))
    draw.rectangle(nub, fill=_OUTLINE_COLOR)

    inner_width = inner_right - inner_left
    fill_width = int(inner_width * (max(0, min(100, state.percent)) / 100))
    if fill_width > 0:
        color = _fill_color(state.percent)
        draw.rectangle(
            (inner_left, inner_top, inner_left + fill_width, inner_bottom),
            fill=color,
        )

    if state.charging:
        bolt = [
            s(15, 11), s(12, 17), s(14, 17),
            s(13, 21), s(16, 15), s(14, 15),
        ]
        draw.polygon(bolt, fill=(255, 255, 255, 255))

    return img


class TrayIcon:
    """pystray wrapper. Owns its own thread; all callbacks fire on that thread."""

    def __init__(
        self,
        on_toggle_window: Callable[[], None],
        on_show: Callable[[], None],
        on_hide: Callable[[], None],
        on_quit: Callable[[], None],
    ):
        self._on_toggle = on_toggle_window
        self._on_show = on_show
        self._on_hide = on_hide
        self._on_quit = on_quit
        self._window_visible = True
        self._transport: Optional[str] = None
        self._icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None

    def _build_menu(self) -> pystray.Menu:
        # On Windows pystray invokes the menu's default item on a normal
        # left-click.  Keep that action deliberately invisible in the
        # right-click menu: a single click must immediately restore the app,
        # not open a second "show/hide" choice menu.
        return pystray.Menu(
            pystray.MenuItem(
                "Открыть приложение",
                lambda icon, item: self._on_show(),
                default=True,
                visible=False,
            ),
            pystray.MenuItem("Выход", lambda icon, item: self._on_quit()),
        )

    def start(self) -> None:
        initial = render_battery_image(BatteryState(), transport=self._transport)
        self._icon = pystray.Icon(
            name="qmk_manager",
            icon=initial,
            title="QMK.Top Manager — Battery: no data",
            menu=self._build_menu(),
        )
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()

    def update_battery(
        self, state: BatteryState, *, transport: Optional[str] = None
    ) -> None:
        if self._icon is None:
            return
        if transport in ("wired", "wireless"):
            self._transport = transport
        self._icon.icon = render_battery_image(state, transport=self._transport)
        if self._transport == "wired" and (state.is_stale or state.percent is None):
            tooltip = "QMK.Top Manager — USB: проводное подключение"
        elif state.is_stale or state.percent is None:
            tooltip = "QMK.Top Manager — Battery: no data"
        else:
            suffix = " ⚡" if state.charging else ""
            tooltip = f"QMK.Top Manager — Battery: {state.percent}%{suffix}"
        self._icon.title = tooltip

    def set_window_visible(self, visible: bool) -> None:
        self._window_visible = visible
        if self._icon is not None:
            self._icon.update_menu()
