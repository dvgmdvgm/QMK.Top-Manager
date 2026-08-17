import argparse
import asyncio
import inspect
from collections import deque
import flet as ft
import hid
import json
import logging
import math
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import ctypes
from pathlib import Path
from types import SimpleNamespace
import win32gui
import win32process
try:
    import win32ui
except ImportError:  # The app remains importable on non-Windows test hosts.
    win32ui = None
from PIL import Image
try:
    import win32clipboard
except ImportError:  # Keep the portable/Flet fallback usable outside Windows.
    win32clipboard = None
import psutil
import keyboard
from dataclasses import dataclass, field
from enum import IntEnum, Flag, auto
from battery import BatteryMonitor, BatteryState
from tray import TrayIcon, set_icon_source
from sniffer import HIDSniffer, _find_chrome, is_chromium_executable
from autostart import APP_ID, paths, acquire_single_instance, bring_existing_to_front
from lighting import (
    EFFECT_OPTIONS,
    EFFECTS,
    LightingProtocolError,
    LightingSettings,
    NEUTRAL_LIGHTING_COLOR_HEX,
    RongyuanLightingProtocol,
    hsv_degrees_to_rgb,
    parse_hex_color,
    picker_position_to_sv,
    rgb_to_hex,
    rgb_to_hsv_degrees,
)
from magnetic import (
    KeyMagneticSettings,
    KeyboardOptions,
    MagneticProtocol,
    MagneticProtocolError,
    SK75_KEYS,
    SK75_KEY_BY_SLOT,
    SK75_VISUAL_LAYOUT,
)
from womier_import import (
    WOMIER_DRIVER_LEVELDB,
    WOMIER_DRIVER_EXE,
    WOMIER_IOT_DRIVER_EXE,
    WOMIER_IOT_DRIVER_V210_EXE,
    WomierCacheSyncError,
    find_womier_magnetic_import,
    sync_womier_magnetic_cache,
    womier_storage_fingerprint,
)

logger = logging.getLogger(__name__)

CONFIG_FILE = paths.config_path
# Public copy/paste deliberately moves selected *configuration* only.  HID
# profile payloads are never exported: they are machine/firmware state, while
# profile labels, process rules, Lighting Lab and Magnetic Lab are safe,
# independently selectable user settings.
CONFIG_TRANSFER_FORMAT = "qmk-top-manager-selected-config"
CONFIG_TRANSFER_VERSION = 3
# The first public clipboard document copied profile names/process rules only.
# Keep accepting it so users can paste a document made by any recent build.
PROFILE_RULES_TRANSFER_FORMAT = "qmk-top-manager-profile-rules"
PROFILE_RULES_TRANSFER_VERSION = 2
LEGACY_CONFIG_TRANSFER_FORMAT = "qmk-top-manager-config"
LEGACY_CONFIG_TRANSFER_VERSION = 1
# Stable IDs are intentionally storage/API names rather than translated UI
# labels.  They make a transfer portable between Russian/English releases.
TRANSFER_SECTION_PROFILE_NAMES = "profile_names"
TRANSFER_SECTION_PROCESS_BINDINGS = "process_bindings"
TRANSFER_SECTION_LIGHTING_LAB = "lighting_lab"
TRANSFER_SECTION_MAGNETIC_LAB = "magnetic_lab"
CONFIG_TRANSFER_SECTION_ORDER = (
    TRANSFER_SECTION_PROFILE_NAMES,
    TRANSFER_SECTION_LIGHTING_LAB,
    TRANSFER_SECTION_MAGNETIC_LAB,
    TRANSFER_SECTION_PROCESS_BINDINGS,
)
CONFIG_TRANSFER_SECTIONS = frozenset(CONFIG_TRANSFER_SECTION_ORDER)
# These keys are in-memory references to the selected device.  They are
# deliberately reconstructed after a load and must never be copied as a
# second, potentially stale device configuration.
CONFIG_RUNTIME_ALIAS_KEYS = ("payloads", "bindings", "battery", "device")
# A copy/paste transfer is authoritative.  Keep a small, durable marker in a
# transferred document so the next ordinary launch cannot import a different
# local Womier cache over the presets that the user explicitly pasted.
#
# This is deliberately separate from ``womier_magnetic_import``: that field is
# a report about a concrete Womier cache read, whereas this one records the
# import boundary itself and remains valid when a configuration moves to a
# different Windows account/path.
CONFIG_TRANSFER_WOMIER_GUARD_KEY = "womier_magnetic_profiles_authoritative"
# A config save can originate from a UI event and a deferred Womier-cache
# mirror at roughly the same time.  Serialise whole-file replacements so one
# complete JSON document always wins; never interleave two writes.
_CONFIG_WRITE_LOCK = threading.RLock()
OFFLINE_MODE = os.environ.get("QMK_OFFLINE_MODE", "1").strip().lower() not in ("0", "false", "no")
ENABLE_UPDATE_CHECK = os.environ.get("QMK_ENABLE_UPDATE_CHECK", "0").strip().lower() in ("1", "true", "yes")
def _path_from_environment(name: str) -> Path | None:
    """Return an explicitly configured optional path, never a machine path.

    Releases must work outside the developer's folder layout.  A previous
    manager can still be imported manually, but only when its config location
    is deliberately supplied via the environment instead of being guessed
    from ``C:\\QMK.Top.Manager`` on every first launch.
    """
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


# Legacy configuration migration is an explicit opt-in only.  This protects a
# new public install from inheriting another manager's profile names, process
# bindings, RGB presets or keyboard state.  Users who truly want that import
# can set this one optional path before launching, or use portable JSON copy /
# paste in the Settings screen.
LEGACY_CONFIG_FILE = _path_from_environment("QMK_TOP_MANAGER_LEGACY_CONFIG")
# A release always carries its own icon.  Never extract an image from an
# unrelated installation — the original manager may not exist or may live in
# a completely different folder on another Windows account.
QMK_TOP_MANAGER_ICON_RESOURCE = os.path.join(
    "assets", "qmk-top-manager-keyboard.ico"
)
PROFILE_SWITCH_DELAY_VALUES_MS = (0, 50, 100, 150, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000)
# Latest-wins cancellation makes a built-in foreground delay unnecessary.
# Keep the chooser for users who deliberately want one, but never make
# Alt+Tab feel delayed before they opt in.
DEFAULT_PROFILE_SWITCH_DELAY_MS = 0
MAX_PROFILE_SWITCH_DELAY_MS = max(PROFILE_SWITCH_DELAY_VALUES_MS)
# A portable export is normally tens of kilobytes.  Keep the import cap high
# enough for several devices/profiles, while preventing an unrelated huge
# clipboard payload from being pushed into a desktop TextField and freezing
# the UI before validation runs.
MAX_CONFIG_IMPORT_CHARS = 512_000
MAX_PROFILE_RULES_DEVICES = 16
MAX_PROFILE_RULES_PER_DEVICE = 16
MAX_PROFILE_RULES_BINDINGS_PER_DEVICE = 10_000
MAX_PROFILE_RULE_NAME_CHARS = 80
MAX_PROFILE_RULE_PROCESS_CHARS = 260
# A profile report is a HID feature payload.  Older exports sometimes keep a
# short prefix (the manager fills in a default when it is absent), so imports
# intentionally allow 0..64 bytes rather than requiring an exact 64-byte
# report.  They must still be real byte values: otherwise a malformed pasted
# JSON used to be persisted and later crash profile rendering or HID sends.
MAX_PROFILE_PAYLOAD_BYTES = 64
# The opt-in Chromium sniffer receives browser-controlled JSON.  Retain only
# the recent useful HID records and only the scalar fields its UI needs.  This
# keeps a busy page from growing the in-memory/copy-to-clipboard log forever.
SNIFF_EVENT_LIMIT = 500
SNIFF_EVENT_DATA_LIMIT = 256
SNIFF_EVENT_TEXT_LIMIT = 128
# Womier's own ``setMagnetismInfoSimple`` waits for its default 100 ms
# vendorSleep after each completed simple-key transaction.  A profile is a
# sequence of those same simple transactions, so a batch must preserve the
# individual commit boundary instead of sending the final packet of one key
# immediately into the first packet of the next key.
WOMIER_MAGNETIC_SIMPLE_COMMIT_DELAY_SEC = 0.10
# A slider is a high-frequency input path.  The HID setting is still applied
# after its short write debounce, but serialising the whole configuration and
# rewriting Womier's Chromium cache on every 0.01-mm step competes with Flet's
# event loop (and can visibly stall the ruler).  Keep those two durable cache
# operations behind one quiet-period gate.  Explicit hide/quit paths flush it
# so no accepted value is lost.
MAGNETIC_BACKGROUND_PERSIST_DEBOUNCE_SEC = 0.75
# A LevelDB read/write can fail transiently when Windows antivirus, Explorer,
# or the official driver briefly holds one of its files.  The HID write already
# succeeded at this point, so retain its narrow cache delta and retry it later
# instead of silently losing the official-driver mirror.
WOMIER_CACHE_SYNC_ERROR_RETRY_SEC = 15.0
# Polling the foreground window eight times per second keeps an Alt+Tab change
# responsive without busy-looping or repeatedly applying a profile.
PROFILE_WINDOW_POLL_INTERVAL_SEC = 0.12
# ``AUTO_MAGNETIC_PROFILE_SETTLE_SEC`` used to delay a large automatic
# magnetic-preset write after every Alt+Tab.  It is deliberately retained as a
# zero-valued compatibility constant for small integrations which import it,
# but automatic foreground switching no longer starts such a batch at all.
#
# The SK75 has no firmware magnetic-profile byte: applying a local preset means
# dozens of per-key feature reports.  Even a cancelled batch can keep the
# keyboard's HID endpoint busy long enough to make physical keys appear dead.
# Automatic rules now send only the tiny ordinary profile packet; Magnetic Lab
# remains the explicit, verified place to apply a magnetic preset to hardware.
AUTO_MAGNETIC_PROFILE_SETTLE_SEC = 0.0

# ``0x1B`` live-travel reports can arrive much faster than a desktop UI can
# present them.  Always drain the HID endpoint to its newest packet, then
# render one coalesced frame.  Reading just one report and sleeping used to
# leave an ever-growing native HID backlog behind a moving key, which looked
# like severe visual lag (especially in the packaged desktop build).
TRAVEL_TESTER_HID_DRAIN_LIMIT = 64
# The live tester may feed a 144 Hz display, but it must never manufacture
# frames for an unchanged switch.  The reader samples at most once per display
# interval, publishes only a new visible hundredth of a millimetre, and the
# UI paints only when such a sample exists.  Flutter/Windows may still present
# fewer frames when the device or compositor is slower; no stale frame queue
# is allowed to accumulate.
TRAVEL_TESTER_MAX_FRAME_RATE = 144
TRAVEL_TESTER_HID_SAMPLE_INTERVAL_SEC = 1 / TRAVEL_TESTER_MAX_FRAME_RATE
TRAVEL_TESTER_RENDER_INTERVAL_SEC = 1 / TRAVEL_TESTER_MAX_FRAME_RATE
# While the key is stationary the async task only checks for the first new
# sample at display-grade 60 Hz.  Once motion is detected it stays on the
# 144 Hz path briefly, avoiding a permanent high-frequency idle loop without
# making an ongoing press feel stepped.
TRAVEL_TESTER_IDLE_RENDER_INTERVAL_SEC = 1 / 60
TRAVEL_TESTER_ACTIVE_RENDER_HOLD_SEC = 0.10
TRAVEL_TESTER_DIRECTION_HYSTERESIS_MM = 0.02
TRAVEL_TESTER_KEY_POLL_INTERVAL_SEC = 0.12


# The stock Windows package contains two processes which can both keep an
# exclusive HID/cache handle open.  The dynamic, user-install aware executable
# candidates are resolved in ``womier_import``; this UI layer only permits
# exact path/name matches and never searches by a loose process name.
WOMIER_DRIVER_PROCESS_TARGETS = (
    ("Womier Driver", "womier driver.exe", WOMIER_DRIVER_EXE),
    ("Womier iot_driver", "iot_driver.exe", WOMIER_IOT_DRIVER_EXE),
    (
        "Womier iot_driver_v210",
        "iot_driver_v210.exe",
        WOMIER_IOT_DRIVER_V210_EXE,
    ),
)


@dataclass(frozen=True)
class WomierDriverProcessMatch:
    """One exact stock-driver process that is safe to close after confirmation.

    ``process`` is deliberately retained only for the very short close
    transaction.  It is revalidated by executable *and* process name directly
    before every terminate/kill call, preventing an old PID snapshot from
    turning into a broad process killer.
    """

    pid: int
    label: str
    executable: str
    created_at: float | None
    process: object = field(repr=False, compare=False)

    @property
    def display_name(self) -> str:
        return f"{self.label} (PID {self.pid})"


@dataclass(frozen=True)
class WomierDriverCloseResult:
    """Narrow outcome of a confirmed stock-driver close operation."""

    found: tuple[WomierDriverProcessMatch, ...]
    closed: tuple[WomierDriverProcessMatch, ...]
    remaining: tuple[WomierDriverProcessMatch, ...]
    skipped: tuple[WomierDriverProcessMatch, ...]
    errors: tuple[str, ...]


def _normalised_executable_path(value) -> str:
    """Return a comparison-only canonical path; invalid data never matches."""
    try:
        text = os.fspath(value)
    except (TypeError, ValueError):
        return ""
    if not text:
        return ""
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(text)))
    except (OSError, TypeError, ValueError):
        return ""


def _read_process_identity(process, *, fresh=False):
    """Read the minimum identity fields needed for an exact close decision.

    ``psutil.process_iter`` supplies a cached ``info`` mapping, which is ideal
    for listing.  A close action asks the Process object again (`fresh=True`)
    so a just-reused PID cannot inherit that original snapshot.
    """
    info = getattr(process, "info", {})
    if not isinstance(info, dict):
        info = {}
    name = info.get("name")
    executable = info.get("exe")
    created_at = info.get("create_time")
    if fresh:
        try:
            name_method = getattr(process, "name", None)
            if callable(name_method):
                name = name_method()
        except (psutil.Error, OSError, AttributeError):
            return None
        try:
            exe_method = getattr(process, "exe", None)
            if callable(exe_method):
                executable = exe_method()
        except (psutil.Error, OSError, AttributeError):
            return None
        try:
            create_time_method = getattr(process, "create_time", None)
            if callable(create_time_method):
                created_at = create_time_method()
        except (psutil.Error, OSError, AttributeError):
            return None
    try:
        pid = int(getattr(process, "pid"))
    except (AttributeError, TypeError, ValueError):
        return None
    if not name or not executable:
        return None
    try:
        created_at = float(created_at) if created_at is not None else None
    except (TypeError, ValueError):
        created_at = None
    return pid, str(name), str(executable), created_at


def _exact_womier_driver_match(process, *, fresh=False):
    """Return a match only for the two canonical Womier installation files.

    A same-named executable outside the known Womier installation, a process
    with an unreadable executable path, and this application's own PID are all
    intentionally ignored.  This function is the sole admission check for
    process termination below.
    """
    identity = _read_process_identity(process, fresh=fresh)
    if identity is None:
        return None
    pid, name, executable, created_at = identity
    if pid == os.getpid():
        return None
    normalized_executable = _normalised_executable_path(executable)
    normalized_name = name.casefold()
    for label, expected_name, expected_executable in WOMIER_DRIVER_PROCESS_TARGETS:
        if (
            normalized_name == expected_name
            and normalized_executable
            and normalized_executable
            == _normalised_executable_path(expected_executable)
        ):
            return WomierDriverProcessMatch(
                pid=pid,
                label=label,
                executable=executable,
                created_at=created_at,
                process=process,
            )
    return None


def _find_exact_womier_driver_processes():
    """List only official-driver processes proved by exact name+path matches."""
    matches = []
    try:
        iterator = psutil.process_iter(("pid", "name", "exe", "create_time"))
        for process in iterator:
            try:
                match = _exact_womier_driver_match(process)
            except (psutil.Error, OSError):
                continue
            if match is not None:
                matches.append(match)
    except (psutil.Error, OSError):
        return ()
    return tuple(sorted(matches, key=lambda item: (item.label, item.pid)))


def _same_exact_womier_driver_process(match):
    """Revalidate a listed process immediately before a destructive call."""
    refreshed = _exact_womier_driver_match(match.process, fresh=True)
    if refreshed is None or refreshed.pid != match.pid or refreshed.label != match.label:
        return None
    # psutil's Process object itself also guards against PID reuse.  Preserve
    # a known creation time as a second cheap identity check where available.
    if (
        match.created_at is not None
        and refreshed.created_at is not None
        and abs(match.created_at - refreshed.created_at) > 0.001
    ):
        return None
    return refreshed


def _womier_process_has_exited(process) -> bool:
    """Tell an exited target from a live process whose identity changed."""
    try:
        is_running = getattr(process, "is_running", None)
        if callable(is_running):
            return not bool(is_running())
        # Lightweight test doubles and old psutil wrappers may not expose
        # ``is_running``.  A direct fresh name call still distinguishes a
        # gone process (NoSuchProcess) from one that merely changed path.
        name_method = getattr(process, "name", None)
        if callable(name_method):
            name_method()
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError, AttributeError):
        return False
    return False


def _close_exact_womier_driver_processes(*, graceful_timeout=1.2):
    """Close only exact Womier/iot executables after the UI confirmation.

    Windows' ``terminate`` is normally enough for these packaged Electron
    helpers.  A short wait is followed by a second exact-identity check before
    a force kill, so the button never targets a process by PID alone.
    """
    found = _find_exact_womier_driver_processes()
    if not found:
        return WomierDriverCloseResult((), (), (), (), ())

    candidates = []
    skipped = []
    errors = []
    for match in found:
        current = _same_exact_womier_driver_process(match)
        if current is None:
            skipped.append(match)
            continue
        try:
            current.process.terminate()
            candidates.append(current)
        except psutil.NoSuchProcess:
            # It already exited after the scan, which is the desired outcome.
            candidates.append(current)
        except (psutil.Error, OSError) as exc:
            errors.append(f"{current.display_name}: {exc}")

    deadline = time.monotonic() + max(0.0, float(graceful_timeout))
    remaining = list(candidates)
    while remaining:
        alive = []
        for match in remaining:
            current = _same_exact_womier_driver_process(match)
            if current is not None:
                alive.append(current)
            elif not _womier_process_has_exited(match.process):
                # It is still alive but no longer proves to be one of the
                # exact files.  Do not send it a force-kill by stale PID.
                skipped.append(match)
        remaining = alive
        if not remaining or time.monotonic() >= deadline:
            break
        time.sleep(0.05)

    # Explicitly requested close/kill, but re-check the exact canonical path
    # one last time.  Anything which changed identity is reported as skipped.
    force_remaining = []
    for match in remaining:
        current = _same_exact_womier_driver_process(match)
        if current is None:
            if not _womier_process_has_exited(match.process):
                skipped.append(match)
            continue
        try:
            current.process.kill()
            force_remaining.append(current)
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError) as exc:
            errors.append(f"{current.display_name}: {exc}")
            force_remaining.append(current)

    # Give Windows a tiny bounded chance to reap an Electron child after the
    # force request.  Do not wait indefinitely on a process that refuses to
    # exit; the result tells the user exactly what remains.
    if force_remaining:
        deadline = time.monotonic() + 0.5
        while force_remaining:
            alive = []
            for match in force_remaining:
                current = _same_exact_womier_driver_process(match)
                if current is not None:
                    alive.append(current)
                elif not _womier_process_has_exited(match.process):
                    skipped.append(match)
            force_remaining = alive
            if not force_remaining or time.monotonic() >= deadline:
                break
            time.sleep(0.05)

    remaining = tuple(force_remaining)
    remaining_pids = {match.pid for match in remaining}
    skipped_pids = {match.pid for match in skipped}
    closed = tuple(
        match
        for match in candidates
        if match.pid not in remaining_pids and match.pid not in skipped_pids
    )
    return WomierDriverCloseResult(
        found=found,
        closed=closed,
        remaining=remaining,
        skipped=tuple(skipped),
        errors=tuple(errors),
    )


def _launch_exact_womier_driver():
    """Open only the known stock Womier executable.

    The navigation action is deliberately not a generic ``open .exe``
    launcher: it never accepts a path from the UI and does not use a shell.
    That keeps it paired with the equally narrow exact-path close action
    above, while still allowing the user to hand the keyboard back to the
    official driver.
    """
    executable = Path(WOMIER_DRIVER_EXE)
    try:
        if not executable.is_file():
            return False, f"WOMIER Driver не найден: {executable}"
    except OSError as exc:
        return False, f"Не удалось проверить WOMIER Driver: {exc}"

    # ``startfile`` starts exactly this executable through Windows' normal
    # desktop association and returns immediately.  It avoids a command
    # shell, quoting ambiguity and any ability to inject a second command.
    starter = getattr(os, "startfile", None)
    if not callable(starter):
        return False, "Открыть WOMIER Driver можно только в Windows."
    try:
        starter(os.fspath(executable))
    except OSError as exc:
        return False, f"Не удалось открыть WOMIER Driver: {exc}"
    return True, "WOMIER Driver открыт."


# Calibration changes switch reference points in the keyboard firmware, so a
# key that happens to be held while the dialog is opening must never be
# treated as an intentional calibration press.  The first pause happens *before*
# the official 0x1C/0x1E start sequence.  After the mode is enabled, use the
# stock driver's 500 ms settling window plus stable released samples and the
# same one-second full press described by Womier's own wired-calibration hint.
# This cannot undo a firmware calibration already accepted by the board, but it
# stops a transient 0xFE value from becoming a misleading green result locally.
CALIBRATION_START_SETTLE_SECONDS = 0.90
CALIBRATION_POST_START_GUARD_SECONDS = 0.70
CALIBRATION_RELEASE_STABLE_SAMPLES = 4
CALIBRATION_FULL_HOLD_SECONDS = 1.00

# The generic settings reader waits 30 ms after every feature report because
# it is deliberately conservative for one-shot configuration reads.  During
# calibration that becomes four open/read/close cycles per sample and makes
# the indicator visibly lag.  Womier's own calibration loop asks for the four
# 0xFE chunks with a 1 ms request cadence.  Keep a small 6 ms settle for this
# one exclusive, read-only mode, while retaining the generic 30 ms path
# everywhere else.
CALIBRATION_FEATURE_REPORT_SETTLE_SECONDS = 0.006
# With the reusable handle, four progress chunks take about one display frame.
# Leave a small cooperative pause rather than polling as fast as Python can;
# the Flutter paint loop independently caps itself at 45 ms.
CALIBRATION_PROGRESS_POLL_PAUSE_SECONDS = 0.012


# Presentation constants for the visual SK75 deck.  These intentionally live
# beside the app UI rather than the HID map: they describe physical case gaps,
# not matrix wiring.  Keeping one source of truth prevents a small visual
# change from making one row drift independently from the others.
SK75_VISUAL_CLUSTER_BREAKS: dict[int, frozenset[int]] = {
    # Esc/F1, F4/F5, F8/F9 and F12/Del are separate physical groups.
    0: frozenset({0, 4, 8, 12, 13}),
    # Main typing area and the right navigation column.
    1: frozenset({13}),
    2: frozenset({13}),
    3: frozenset({12}),
    4: frozenset({11}),
    # Bottom modifiers and arrow cluster.
    5: frozenset({5}),
}

# Physical x positions in keyboard units.  A normal key is one unit wide.
# This is deliberately a positioned board, rather than two rows held apart by
# an expanding spacer: the SK75 has one compact navigation column on the
# right, not a large elastic void through the middle.  The Up arrow occupies
# the same column as Down, leaving the last navigation column empty on that
# row, just as on the physical 75% layout.
#
# ``SK75_VISUAL_LAYOUT`` remains the HID-facing, stable 81-key ordering in
# ``magnetic.py``.  The official Womier driver renders the same keys in a
# slightly different *visual* order: Delete sits after F12, while Home/End/
# PgUp/PgDn form a vertical right column.  Keep that presentation-only order
# local to this UI module, so correcting a keycap's physical place never
# changes a matrix slot or a HID write.
SK75_VISUAL_CASE_UNITS = 16.25
SK75_VISUAL_ROW_X_UNITS: tuple[tuple[float, ...], ...] = (
    (0.0, 1.25, 2.25, 3.25, 4.25, 5.5, 6.5, 7.5, 8.5, 9.75, 10.75, 11.75, 12.75, 14.0, 15.25),
    (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 15.25),
    (0.0, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5, 11.5, 12.5, 13.5, 15.25),
    (0.0, 1.75, 2.75, 3.75, 4.75, 5.75, 6.75, 7.75, 8.75, 9.75, 10.75, 11.75, 12.75, 15.25),
    (0.0, 2.25, 3.25, 4.25, 5.25, 6.25, 7.25, 8.25, 9.25, 10.25, 11.25, 12.25, 14.25),
    (0.0, 1.25, 2.5, 3.75, 10.5, 11.75, 13.25, 14.25, 15.25),
)


def _sk75_official_visual_layout():
    """Return the official-driver physical order without touching HID slots.

    The firmware map stores Home in the number-row tuple and Page Down next
    to the Up arrow.  The physical SK75 cap layout shown by Womier puts Home,
    End, PgUp and PgDn in the right-hand column instead.  Reordering the
    existing tuples preserves every one of the 81 slots exactly once.
    """
    top, number, qwerty, home, shift, bottom = SK75_VISUAL_LAYOUT
    delete_key = top[-1]
    home_key = number[-1]
    end_key = qwerty[-1]
    pg_up_key = home[-1]
    up_key, pg_down_key = shift[-2:]
    return (
        (*top[:-1], delete_key, home_key),
        (*number[:-1], end_key),
        (*qwerty[:-1], pg_up_key),
        (*home[:-1], pg_down_key),
        (*shift[:-2], up_key),
        bottom,
    )


SK75_OFFICIAL_VISUAL_LAYOUT = _sk75_official_visual_layout()

# A 1360 px window leaves a little over 1100 px for the physical deck once
# the left rail, card insets and the vertical scrollbar are accounted for.
# Keep this calculation here rather than hard-coding the former 1128 px deck:
# on a high-DPI desktop Flet can report a few pixels less usable width than
# the native window, which used to cut off Home/PgUp/PgDn at the right edge.
SK75_VISUAL_FULL_DECK_WIDTH = 1128
SK75_VISUAL_COMPACT_DECK_WIDTH = 760
SK75_VISUAL_VIEWPORT_RESERVE = 260
SK75_VISUAL_INITIAL_DECK_WIDTH = 1100

# These are flat driver-style keycaps, not an illustration of a physical case.
# The real values are the bright element; gradients and coloured activation
# bands made the board harder to scan and did not match the Womier driver.
SK75_VISUAL_KEY_BACKGROUND = "#2B2B31"
SK75_VISUAL_KEY_BORDER = "#4B4A54"
SK75_VISUAL_KEY_SELECTED_BACKGROUND = "#3A304F"
SK75_VISUAL_KEY_SELECTED_BORDER = "#B98DFF"

# This one-line guide is deliberately kept directly below the visual keyboard
# instead of duplicating labels over every tiny cap.  The corner positions are
# part of the magnetic UI's language, so spell them out once in the place
# where the user can compare a cap with the vertical controls below it.
MAGNETIC_KEY_METRICS_EXPLANATION = (
    "Значения на клавише: слева сверху — точка активации (голубой). При включённом "
    "Rapid Trigger справа сверху — RT при отпускании (жёлтый), справа снизу — RT "
    "при повторном нажатии вниз (бирюзовый). При выключенном Rapid Trigger справа "
    "сверху — точка деактивации (жёлтый), а справа снизу — прочерк: второго RT-порога "
    "нет. При включённом RT шкала при отпускании задаёт сброс при движении вверх; "
    "при выключенном RT применяется обычная точка деактивации. Дополнительная шкала "
    "появляется только при включении отдельного RT и задаёт новое срабатывание при "
    "повторном нажатии вниз. Дез-зоны сверху и снизу — "
    "запас у соответствующих краёв хода. Максимум SK75 по официальной прошивке — "
    "3.30 мм."
)

# The scale headers use the same wording as the three coloured metrics on a
# keycap.  The explicit two-line layout leaves the full physical travel ruler
# readable while avoiding ambiguous short labels such as just "RT вверх".
MAGNETIC_SCALE_ROLE_LABELS = {
    "actuation": "Активация",
    "rapid_release": "RT при\nотпускании",
    "rapid_press": "RT при повторном\nнажатии вниз",
    "lower_dead_zone": "Дез-зона\nснизу",
    "upper_dead_zone": "Дез-зона\nсверху",
}

# A metric keeps one colour everywhere it is represented: on its vertical
# ruler and in the corresponding corner of a keycap.  In particular the old
# red actuation number looked like it belonged to a different control than
# the cyan activation ruler below the deck.
MAGNETIC_METRIC_COLORS = {
    "actuation": "#46D7FF",
    "rapid_release": "#FFD24A",
    "rapid_press": "#4EE8DF",
}


def _sk75_visual_deck_width_for_viewport(
    viewport_width: object | None, *, compact: bool = False
) -> int:
    """Return a deck width that fits the actual central workspace.

    The compact Snap Key dialog has its own stable width.  The full Magnetic
    Lab board instead derives its width from the page viewport so its 17u
    physical grid scales as one object and never lets the right navigation
    column escape the case on the minimum desktop window.
    """
    if compact:
        return SK75_VISUAL_COMPACT_DECK_WIDTH
    try:
        width = int(float(viewport_width))
    except (TypeError, ValueError):
        width = 0
    if width <= 0:
        return SK75_VISUAL_INITIAL_DECK_WIDTH
    # The native window is constrained to >= 1360 px, but retain a small
    # positive lower bound for detached previews/tests rather than producing
    # a negative content width.
    return min(
        SK75_VISUAL_FULL_DECK_WIDTH,
        max(360, width - SK75_VISUAL_VIEWPORT_RESERVE),
    )


@dataclass(frozen=True)
class SK75VisualDeckGeometry:
    """Measured, positioned layout values for the visual 75% keyboard.

    The board uses one 17u physical coordinate system.  Every key's x
    position is explicit and every row ends against the same right case edge,
    so a short alpha row cannot turn into a left/right layout with a floating
    navigation cluster in the middle.
    """

    deck_width: int
    deck_padding: int
    row_width: int
    key_spacing: int
    cluster_spacer_width: int
    key_base_width: float
    key_pitch: float
    case_units: float
    cluster_breaks: dict[int, frozenset[int]]
    key_x_units: tuple[tuple[float, ...], ...]
    key_x_positions: tuple[tuple[int, ...], ...]
    key_widths: tuple[tuple[int, ...], ...]
    row_content_widths: tuple[int, ...]
    row_right_edges: tuple[int, ...]
    right_cluster_starts: tuple[int, ...]
    left_cluster_widths: tuple[int, ...]
    right_cluster_widths: tuple[int, ...]
    right_cluster_offsets: tuple[int, ...]
    right_cluster_min_gap: int


def _sk75_visual_deck_geometry(
    *, compact: bool = False, deck_width: int | None = None
) -> SK75VisualDeckGeometry:
    """Return one positioned, non-overflowing geometry for the SK75 deck."""
    # The driver keeps the physical caps close together.  Large card-like
    # gaps made the current selector look like a dashboard and amplified the
    # blank space before the navigation keys.
    key_spacing = 3 if compact else 4
    if deck_width is None:
        # The standalone helper represents the natural board.  The live UI
        # always supplies its measured fit width.
        deck_width = (
            SK75_VISUAL_COMPACT_DECK_WIDTH
            if compact
            else SK75_VISUAL_FULL_DECK_WIDTH
        )
    else:
        deck_width = max(360, int(deck_width))
    # This is a flat driver-style plate, not a decorative keyboard case: use
    # all available deck width for actual caps.
    deck_padding = 0
    row_width = max(1, deck_width)
    # Keep the pitch fractional until pixel coordinates are rounded so the
    # full rows land cleanly on the shared case edge at every supported size.
    key_pitch = (row_width + key_spacing) / SK75_VISUAL_CASE_UNITS
    key_base_width = key_pitch - key_spacing
    cluster_spacer_width = max(4 if compact else 7, round(key_pitch * 0.18))

    if len(SK75_VISUAL_ROW_X_UNITS) != len(SK75_OFFICIAL_VISUAL_LAYOUT):
        raise RuntimeError("SK75 visual rows and physical coordinates are out of sync")

    key_x_positions: list[tuple[int, ...]] = []
    key_widths: list[tuple[int, ...]] = []
    row_content_widths = []
    row_right_edges = []
    right_cluster_starts = []
    left_cluster_widths = []
    right_cluster_widths = []
    right_cluster_offsets = []
    cluster_gaps = []

    for row_index, (layout_row, row_x_units) in enumerate(
        zip(SK75_OFFICIAL_VISUAL_LAYOUT, SK75_VISUAL_ROW_X_UNITS)
    ):
        if len(layout_row) != len(row_x_units):
            raise RuntimeError(
                f"SK75 visual row {row_index} has {len(layout_row)} keys but "
                f"{len(row_x_units)} x coordinates"
            )
        x_positions = tuple(round(x_unit * key_pitch) for x_unit in row_x_units)
        # Calculate each right edge from the same physical coordinate system
        # as its left edge.  This is more reliable than clamping a key's
        # width: rounding then cannot push the last navigation key past the
        # 17u case boundary on a resized window.
        widths = tuple(
            max(
                1,
                round((x_unit + width) * key_pitch) - key_spacing - left,
            )
            for (_slot, _label, width), x_unit, left in zip(
                layout_row, row_x_units, x_positions
            )
        )
        right_edge = max(x + width for x, width in zip(x_positions, widths))
        row_content_widths.append(right_edge)
        row_right_edges.append(right_edge)
        key_x_positions.append(x_positions)
        key_widths.append(widths)

        breaks = SK75_VISUAL_CLUSTER_BREAKS.get(row_index, frozenset())
        split_at = max(breaks) + 1 if breaks else len(layout_row)
        split_at = min(max(0, split_at), len(layout_row))
        left_edge = max(
            (x + width for x, width in zip(x_positions[:split_at], widths[:split_at])),
            default=0,
        )
        right_offset = min(x_positions[split_at:], default=left_edge)
        right_edge_group = max(
            (x + width for x, width in zip(x_positions[split_at:], widths[split_at:])),
            default=right_offset,
        )
        right_cluster_starts.append(split_at)
        left_cluster_widths.append(left_edge)
        right_cluster_offsets.append(right_offset)
        right_cluster_widths.append(right_edge_group - right_offset)
        if split_at < len(layout_row):
            cluster_gaps.append(max(0, right_offset - left_edge))

    return SK75VisualDeckGeometry(
        deck_width=deck_width,
        deck_padding=deck_padding,
        row_width=row_width,
        key_spacing=key_spacing,
        cluster_spacer_width=cluster_spacer_width,
        key_base_width=key_base_width,
        key_pitch=key_pitch,
        case_units=SK75_VISUAL_CASE_UNITS,
        cluster_breaks=SK75_VISUAL_CLUSTER_BREAKS,
        key_x_units=SK75_VISUAL_ROW_X_UNITS,
        key_x_positions=tuple(key_x_positions),
        key_widths=tuple(key_widths),
        row_content_widths=tuple(row_content_widths),
        row_right_edges=tuple(row_right_edges),
        right_cluster_starts=tuple(right_cluster_starts),
        left_cluster_widths=tuple(left_cluster_widths),
        right_cluster_widths=tuple(right_cluster_widths),
        right_cluster_offsets=tuple(right_cluster_offsets),
        right_cluster_min_gap=min(cluster_gaps, default=0),
    )

_LOCAL_FLET_CLIENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "flet-windows.zip")
if os.path.isfile(_LOCAL_FLET_CLIENT) and not os.environ.get("FLET_CLIENT_URL"):
    os.environ["FLET_CLIENT_URL"] = Path(_LOCAL_FLET_CLIENT).resolve().as_uri()

KEYBOARD_TYPES = {
    "magnetic":    {"opcode": 0x04, "checksum_base": 0xFB, "profiles": 4},
    "mechanical":  {"opcode": 0x05, "checksum_base": 0xFA, "profiles": 3},
}
PROFILE_COUNT = 4
# The SK75 exposes four ordinary keyboard-profile slots.  Its documented
# magnetic HID commands do not carry a profile byte, therefore magnetic
# values are kept as four *application-side* presets (see
# ``magnetic_profiles`` below) rather than pretending the firmware has four
# independent magnetic matrices.
MAGNETIC_PROFILE_COUNT = KEYBOARD_TYPES["magnetic"]["profiles"]


class PollingRate(IntEnum):
    HZ_125  = 125
    HZ_250  = 250
    HZ_500  = 500
    HZ_1000 = 1000
    HZ_2000 = 2000
    HZ_4000 = 4000
    HZ_8000 = 8000


POLLING_RATE_CODES: dict[PollingRate, int] = {
    PollingRate.HZ_125:  6,
    PollingRate.HZ_250:  5,
    PollingRate.HZ_500:  4,
    PollingRate.HZ_1000: 3,
    PollingRate.HZ_2000: 2,
    PollingRate.HZ_4000: 1,
    PollingRate.HZ_8000: 0,
}

VALID_POLLING_RATES = {r.value for r in PollingRate}

LIGHTING_PROFILE_COUNT = 5
VALID_LIGHTING_PROFILES = set(range(LIGHTING_PROFILE_COUNT))


# ---------------------------------------------------------------------------
# Device capability system
# ---------------------------------------------------------------------------

class DeviceCapability(Flag):
    PROFILE_SWITCH = auto()
    HOTKEYS = auto()
    LIGHTING_PROFILES = auto()
    POLLING_RATE = auto()
    PROCESS_RULES = auto()

_CAP_MAGNETIC = (
    DeviceCapability.PROFILE_SWITCH
    | DeviceCapability.HOTKEYS
    | DeviceCapability.LIGHTING_PROFILES
    | DeviceCapability.POLLING_RATE
    | DeviceCapability.PROCESS_RULES
)
_CAP_MECHANICAL = (
    DeviceCapability.PROFILE_SWITCH
    | DeviceCapability.HOTKEYS
    | DeviceCapability.PROCESS_RULES
)

_CAPABILITY_MAP: dict[str | None, DeviceCapability] = {
    "magnetic": _CAP_MAGNETIC,
    "mechanical": _CAP_MECHANICAL,
    None: DeviceCapability(0),
}

def device_capabilities(keyboard_type: str | None) -> DeviceCapability:
    return _CAPABILITY_MAP.get(keyboard_type, DeviceCapability(0))

def has_capability(keyboard_type: str | None, cap: DeviceCapability) -> bool:
    return cap in device_capabilities(keyboard_type)


# ---------------------------------------------------------------------------
# Process-rule evaluator
# ---------------------------------------------------------------------------

@dataclass
class ProcessRule:
    process: str
    profile_index: int
    enabled: bool = True

class RuleEvaluator:
    def __init__(self):
        self._rules: list[ProcessRule] = []
        self._active_index: dict[str, int] = {}

    def load(self, bindings: list[dict]):
        self._rules = [
            ProcessRule(
                process=b["process"],
                profile_index=b["profile_index"],
                enabled=b.get("enabled", True),
            )
            for b in bindings
            if "profile_index" in b
        ]
        self._rebuild_index()

    def _rebuild_index(self):
        self._active_index = {r.process: r.profile_index for r in self._rules if r.enabled}

    def match(self, process_name: str) -> int | None:
        return self._active_index.get(process_name)

    def is_disabled_match(self, process_name: str) -> ProcessRule | None:
        for r in self._rules:
            if r.process == process_name and not r.enabled:
                return r
        return None

    def set_enabled(self, process: str, enabled: bool):
        for r in self._rules:
            if r.process == process:
                r.enabled = enabled
                break
        self._rebuild_index()

    def to_config(self) -> list[dict]:
        return [
            {"process": r.process, "profile_index": r.profile_index, "enabled": r.enabled}
            for r in self._rules
        ]

    @property
    def all_rules(self) -> list[ProcessRule]:
        return list(self._rules)


def _polling_rate_payload(rate: PollingRate) -> list:
    code = POLLING_RATE_CODES[rate]
    payload = [0] * 64
    payload[0] = 0x03
    payload[2] = code
    payload[7] = (255 - sum(payload[0:7])) & 0xFF
    return payload


def _lighting_profile_payload(index: int) -> list:
    payload = [0] * 64
    payload[0] = 0x07
    payload[1] = 0x0D
    payload[2] = 0x04
    payload[3] = 0x04
    payload[4] = (index & 0xFF) * 0x10
    payload[6] = 0xC8
    payload[7] = 0xC8
    payload[8] = (511 - sum(payload[0:8])) & 0xFF
    return payload


DEFAULT_BATTERY_QUERY = [0xF7] + [0] * 63

WIRED_STAGE_DELAYS_MS = {
    "profile": 50,
    "polling": 30,
    "lighting": 30,
}

WIRELESS_STAGE_DELAYS_MS = {
    "profile": 300,
    "polling": 200,
    "lighting": 150,
}


def _resolved_cooldown_ms(entry: dict | None) -> int:
    if not entry:
        return 0
    transport = entry.get("transport") or "wired"
    kb_type = entry.get("keyboard_type")
    if transport == "wireless":
        for key in ("cooldown_wireless_ms", "cooldown_ms"):
            value = entry.get(key)
            if isinstance(value, int) and value > 0:
                return value
        if kb_type == "mechanical":
            return 2000
        return 250
    for key in ("cooldown_wired_ms", "cooldown_ms"):
        value = entry.get(key)
        if isinstance(value, int) and value > 0:
            return value
    if kb_type == "mechanical":
        return 1000
    return 100


def _resolved_profile_switch_delay_ms(entry: dict | None) -> int:
    """Return the user-selected, safe foreground-window settle delay.

    This is intentionally distinct from ``cooldown_ms``.  The latter protects
    keyboard input while a HID transaction is in flight; this value is the
    short pause used to make sure Alt+Tab has settled on the intended window.
    """
    raw_value = (entry or {}).get("profile_switch_delay_ms", DEFAULT_PROFILE_SWITCH_DELAY_MS)
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = DEFAULT_PROFILE_SWITCH_DELAY_MS
    return max(0, min(MAX_PROFILE_SWITCH_DELAY_MS, value))


def _json_copy(value):
    """Detach a JSON-compatible value without retaining mutable aliases."""
    # ``allow_nan=False`` keeps transfer files valid JSON.  Python otherwise
    # accepts/export NaN and Infinity, which are not portable through the
    # native Windows clipboard or another JSON implementation.
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _bounded_sniff_event_snapshot(event: object) -> dict:
    """Return a small, detached and JSON-safe representation of one CDP event.

    The Chromium sniffer receives this structure from a page under inspection,
    not from trusted application state.  The UI only needs the direction,
    report type/id and a short byte array, so never retain arbitrary nested
    values or an unbounded report body in the desktop process.
    """
    source = event if isinstance(event, dict) else {}
    raw_data = source.get("data")
    if isinstance(raw_data, (list, tuple, bytes, bytearray)):
        data_truncated = len(raw_data) > SNIFF_EVENT_DATA_LIMIT
        raw_items = raw_data[:SNIFF_EVENT_DATA_LIMIT]
    else:
        data_truncated = bool(raw_data)
        raw_items = ()

    data: list[int] = []
    for value in raw_items:
        # JSON's numbers are untrusted here.  HID payload bytes must be real
        # unsigned octets; drop malformed members instead of later formatting
        # or persisting them as arbitrary Python values.
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFF:
            data.append(value)

    def bounded_text(value: object) -> str:
        return value[:SNIFF_EVENT_TEXT_LIMIT] if isinstance(value, str) else ""

    report_id = source.get("reportId")
    if not (isinstance(report_id, int) and not isinstance(report_id, bool) and 0 <= report_id <= 0xFF):
        report_id = None

    timestamp = source.get("ts")
    if isinstance(timestamp, int) and not isinstance(timestamp, bool):
        if not -(2**63) <= timestamp <= 2**63 - 1:
            timestamp = None
    elif isinstance(timestamp, float):
        if not math.isfinite(timestamp) or not -(2**63) <= timestamp <= 2**63 - 1:
            timestamp = None
    else:
        timestamp = None

    return {
        "dir": bounded_text(source.get("dir")),
        "type": bounded_text(source.get("type")),
        "reportId": report_id,
        "data": data,
        "ts": timestamp,
        "data_truncated": bool(source.get("data_truncated")) or data_truncated,
    }


def _write_json_atomically(path: Path, value: object) -> None:
    """Write a JSON file without leaving a partly-written configuration.

    Config writes happen from several ordinary UI paths (including import and
    profile edits).  Writing straight to the live file means a forced close or
    a full disk can turn a perfectly valid configuration into an empty or
    truncated file.  A sibling temporary file plus ``os.replace`` keeps the
    prior version intact until serialization has completed successfully.
    """
    destination = Path(path)
    with _CONFIG_WRITE_LOCK:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(value, temporary, indent=4, ensure_ascii=False, allow_nan=False)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
        except Exception:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise


def _preserve_unreadable_config_file(path: Path | str) -> Path | None:
    """Make a non-destructive recovery copy before using in-memory defaults.

    A config can be temporarily unreadable while OneDrive/antivirus is
    touching it, or genuinely malformed after an interrupted external edit.
    The old startup code treated both cases as ``{}`` and immediately wrote
    that value back to the same path.  Preserve the exact source first, then
    let a later explicit save create a clean config.  A symlink is rejected:
    the config writer owns one local path and must not turn a recovery step
    into an arbitrary-file copy/write primitive.
    """
    try:
        source = Path(path)
        if source.is_symlink() or not source.is_file():
            return None
        metadata = source.stat()
        # The source metadata makes the recovery copy idempotent for the same
        # damaged file.  A restart must not make another backup every time
        # while leaving the original untouched for manual inspection.
        candidate = source.with_name(
            f"{source.name}.unreadable-{metadata.st_mtime_ns}-{metadata.st_size}.bak"
        )
        if candidate.exists():
            return candidate
        # ``copy2`` is deliberately used rather than moving/replacing the
        # original: an unreadable file stays available for manual recovery.
        shutil.copy2(source, candidate)
        return candidate
    except (OSError, ValueError, TypeError):
        return None


def _valid_profile_payload_bytes(value: object) -> list[int] | None:
    """Return one bounded HID payload list, or ``None`` when it is unsafe."""
    if not isinstance(value, list) or len(value) > MAX_PROFILE_PAYLOAD_BYTES:
        return None
    result = []
    for byte in value:
        if (
            isinstance(byte, bool)
            or not isinstance(byte, int)
            or not 0 <= byte <= 0xFF
        ):
            return None
        result.append(byte)
    return result


def _validate_ignored_profile_payloads(payloads: object, *, context: str) -> None:
    """Reject malformed HID payloads even when a CFG transfer discards them.

    The public CFG format deliberately moves profile *names*, never HID
    bytes.  A manually edited/old document can still carry a ``payloads``
    field, though.  Silently ignoring invalid bytes would not persist them,
    but it would make a corrupt document appear to have imported cleanly and
    leaves direct callers with different validation from the clipboard path.
    Validate any such legacy field before continuing, then let the portable
    transfer omit it as designed.
    """
    if not isinstance(payloads, dict):
        raise ValueError(f"{context}: payloads должен быть объектом")
    if len(payloads) > MAX_PROFILE_RULES_PER_DEVICE:
        raise ValueError(f"{context}: слишком много профилей")
    for profile_name, profile in payloads.items():
        if not isinstance(profile_name, str) or not profile_name.strip():
            raise ValueError(f"{context}: имя профиля должно быть текстом")
        if not isinstance(profile, dict):
            raise ValueError(f"{context}: профиль {profile_name!r} должен быть объектом")
        if "data" not in profile:
            continue
        if _valid_profile_payload_bytes(profile["data"]) is None:
            raise ValueError(
                f"{context}: data профиля {profile_name!r} "
                f"должен быть списком до {MAX_PROFILE_PAYLOAD_BYTES} байт"
            )


def _reject_duplicate_config_json_keys(pairs: list[tuple[str, object]]) -> dict:
    """Build one JSON object while rejecting ambiguous duplicate keys.

    Python's standard decoder otherwise silently keeps the last occurrence of
    a duplicate key.  That is surprising for an import file (in particular for
    a device id or a magnetic profile slot), and makes review of a copied
    configuration needlessly ambiguous.
    """
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON-конфигурация содержит повторяющийся ключ")
        result[key] = value
    return result


def _reject_nonstandard_config_json_constant(value: str):
    """Reject JSON extensions such as NaN/Infinity in portable imports."""
    raise ValueError(f"недопустимое JSON-значение: {value}")


def _parse_imported_configuration_text(text: object) -> dict:
    """Parse, size-check and normalise one clipboard configuration document.

    The native clipboard and Flet clipboard APIs both return the complete text
    before application code sees it, so the practical safety boundary is to
    refuse an oversized value *before* JSON decoding or placing it into the
    import field.  Keeping that check in this pure helper gives manual imports
    and tests exactly the same protection.
    """
    if not isinstance(text, str):
        raise ValueError("ожидается текстовая JSON-конфигурация")
    if len(text) > MAX_CONFIG_IMPORT_CHARS:
        raise ValueError(
            f"конфигурация слишком большая: максимум {MAX_CONFIG_IMPORT_CHARS:,} символов"
        )
    if not text.strip():
        raise ValueError("конфигурация JSON пуста")
    try:
        loaded = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_config_json_keys,
            parse_constant=_reject_nonstandard_config_json_constant,
        )
    except RecursionError as exc:
        # A syntactically valid but extremely deep document can otherwise
        # produce a Python implementation-detail error in the UI.
        raise ValueError("JSON-конфигурация имеет слишком большую вложенность") from exc
    return _normalise_imported_configuration(loaded)


def _normalise_profile_rule_name(value: object, position: int) -> str:
    """Validate one portable profile name without carrying its HID payload."""
    if not isinstance(value, str):
        raise ValueError(f"имя профиля {position} должно быть текстом")
    name = value.strip()
    if not name:
        raise ValueError(f"имя профиля {position} пустое")
    if len(name) > MAX_PROFILE_RULE_NAME_CHARS:
        raise ValueError(
            f"имя профиля {position} длиннее {MAX_PROFILE_RULE_NAME_CHARS} символов"
        )
    return name


def _normalise_profile_rule_bindings(
    bindings: object, profile_count: int
) -> list[dict]:
    """Keep only safe process->profile rules from a portable transfer."""
    if bindings is None:
        return []
    if not isinstance(bindings, list):
        raise ValueError("привязки процессов должны быть списком")
    if len(bindings) > MAX_PROFILE_RULES_BINDINGS_PER_DEVICE:
        raise ValueError(
            f"слишком много привязок: максимум {MAX_PROFILE_RULES_BINDINGS_PER_DEVICE:,}"
        )
    normalized = []
    for index, item in enumerate(bindings, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"привязка {index} должна быть объектом")
        process = item.get("process")
        if not isinstance(process, str):
            raise ValueError(f"процесс в привязке {index} должен быть текстом")
        process = process.strip().casefold()
        if not process or "\x00" in process or len(process) > MAX_PROFILE_RULE_PROCESS_CHARS:
            raise ValueError(f"некорректное имя процесса в привязке {index}")
        profile_index = item.get("profile_index")
        if (
            isinstance(profile_index, bool)
            or not isinstance(profile_index, int)
            or not 0 <= profile_index < profile_count
        ):
            raise ValueError(f"некорректный профиль в привязке {index}")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"enabled в привязке {index} должен быть true/false")
        normalized.append(
            {
                "process": process,
                "profile_index": profile_index,
                "enabled": enabled,
            }
        )
    return normalized


def _normalise_transfer_sections(sections: object, *, default: object = None) -> tuple[str, ...]:
    """Validate an explicit user selection of portable config sections."""
    if sections is None:
        sections = default
    if isinstance(sections, str) or not isinstance(sections, (list, tuple, set, frozenset)):
        raise ValueError("выберите хотя бы один раздел конфигурации")
    selected = set()
    for section in sections:
        if not isinstance(section, str) or section not in CONFIG_TRANSFER_SECTIONS:
            raise ValueError("выбран неизвестный раздел конфигурации")
        selected.add(section)
    if not selected:
        raise ValueError("выберите хотя бы один раздел конфигурации")
    # A predictable order makes a copied JSON deterministic.
    return tuple(section for section in CONFIG_TRANSFER_SECTION_ORDER if section in selected)


def _normalise_profile_count(value: object, *, device_index: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_PROFILE_RULES_PER_DEVICE
    ):
        raise ValueError(f"некорректное количество профилей у устройства {device_index}")
    return value


def _normalise_default_profile_index(value: object, profile_count: int) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value < profile_count
    ):
        raise ValueError("некорректный профиль по умолчанию")
    return value


def _normalise_device_identity(raw: object, device_index: int) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"identity устройства {device_index} должен быть объектом")
    identity = {}
    for field_name in ("vid", "pid", "usage_page"):
        value = raw.get(field_name)
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 0xFFFF
        ):
            raise ValueError(f"{field_name} устройства {device_index} некорректен")
        identity[field_name] = value
    return identity


def _normalise_magnetic_slot(raw_slot: object) -> str:
    if isinstance(raw_slot, bool):
        raise ValueError("некорректная магнитная клавиша")
    if isinstance(raw_slot, int):
        slot = raw_slot
    elif isinstance(raw_slot, str) and raw_slot.isdecimal():
        slot = int(raw_slot)
    else:
        raise ValueError("некорректная магнитная клавиша")
    if slot not in SK75_KEY_BY_SLOT:
        raise ValueError("магнитный профиль содержит несуществующую клавишу SK75")
    return str(slot)


def _normalise_magnetic_settings_transfer(values: object) -> dict:
    if not isinstance(values, dict):
        raise ValueError("настройки магнитных клавиш должны быть объектом")
    if len(values) > len(SK75_KEY_BY_SLOT):
        raise ValueError("слишком много магнитных клавиш в профиле")
    normalised = {}
    for raw_slot, raw in values.items():
        slot = _normalise_magnetic_slot(raw_slot)
        if not isinstance(raw, dict) or not isinstance(raw.get("rapid_trigger"), bool):
            raise ValueError("некорректные параметры магнитной клавиши")
        try:
            settings = KeyMagneticSettings(
                actuation=raw["actuation"],
                rapid_trigger=raw["rapid_trigger"],
                rapid_press=raw["rapid_press"],
                rapid_release=raw["rapid_release"],
                lower_dead_zone=raw["lower_dead_zone"],
                upper_dead_zone=raw["upper_dead_zone"],
                deactivation=raw.get(
                    "deactivation", raw.get("lift_travel", raw["actuation"])
                ),
            )
            settings = MagneticProtocol.clamp_key_settings_to_official_bounds(settings)
        except (KeyError, TypeError, ValueError, MagneticProtocolError) as exc:
            raise ValueError("некорректные параметры магнитной клавиши") from exc
        normalised[slot] = _magnetic_settings_config_values(settings)
    return normalised


def _normalise_magnetic_mode_transfer(values: object, *, boolean: bool = False) -> dict:
    if not isinstance(values, dict):
        raise ValueError("магнитные параметры должны быть объектом")
    if len(values) > len(SK75_KEY_BY_SLOT):
        raise ValueError("слишком много магнитных клавиш в профиле")
    normalised = {}
    for raw_slot, raw_value in values.items():
        slot = _normalise_magnetic_slot(raw_slot)
        if boolean:
            if not isinstance(raw_value, bool):
                raise ValueError("переключатель отдельного RT должен быть true/false")
            normalised[slot] = raw_value
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, int) or not 0 <= raw_value <= 0xFF:
            raise ValueError("некорректный режим магнитной клавиши")
        normalised[slot] = raw_value
    return normalised


def _normalise_magnetic_snap_pairs_transfer(
    values: object, *, key_modes: object = None
) -> list[list[int]]:
    """Validate the partner mapping that makes stored Snap modes usable.

    A matrix mode read exposes that a key is in Snap mode, but it does not
    expose its partner byte.  Keeping only ``key_modes`` therefore loses the
    actual pair during a CFG round trip.  The portable representation is a
    small list of disjoint physical SK75 slot pairs; raw protocol packets are
    deliberately neither accepted nor generated here.
    """
    if not isinstance(values, list):
        raise ValueError("пары Snap Key должны быть списком")
    if len(values) > len(SK75_KEY_BY_SLOT) // 2:
        raise ValueError("слишком много пар Snap Key")
    modes = key_modes if isinstance(key_modes, dict) else None
    pairs: list[list[int]] = []
    used_slots: set[int] = set()
    for raw_pair in values:
        if not isinstance(raw_pair, (list, tuple)) or len(raw_pair) != 2:
            raise ValueError("некорректная пара Snap Key")
        first = int(_normalise_magnetic_slot(raw_pair[0]))
        second = int(_normalise_magnetic_slot(raw_pair[1]))
        if first == second or first in used_slots or second in used_slots:
            raise ValueError("клавиша не может входить в две пары Snap Key")
        if modes is not None:
            try:
                first_mode = int(modes.get(str(first), MagneticProtocol.MODE_NORMAL))
                second_mode = int(modes.get(str(second), MagneticProtocol.MODE_NORMAL))
            except (TypeError, ValueError) as exc:
                raise ValueError("некорректный режим пары Snap Key") from exc
            if (
                (first_mode & 0x7F) != MagneticProtocol.MODE_SNAP
                or (second_mode & 0x7F) != MagneticProtocol.MODE_SNAP
            ):
                raise ValueError("пара Snap Key не совпадает с режимами клавиш")
        used_slots.update((first, second))
        pairs.append([first, second])
    return pairs


def _safe_magnetic_snap_pairs(values: object, key_modes: object) -> list[list[int]]:
    """Canonicalise local metadata and recover an unambiguous legacy pair.

    Builds predating portable pair metadata persisted only mode 7.  When
    exactly two physical keys have that mode they can only be partners, so the
    relationship is recoverable without reading or guessing a protocol byte.
    More than two markers remain deliberately unpaired because their mapping
    is ambiguous.
    """
    try:
        pairs = _normalise_magnetic_snap_pairs_transfer(values, key_modes=key_modes)
    except ValueError:
        pairs = []
    if pairs or not isinstance(key_modes, dict):
        return pairs
    snap_slots = []
    for raw_slot, raw_mode in key_modes.items():
        try:
            slot = int(_normalise_magnetic_slot(raw_slot))
            mode = int(raw_mode)
        except (TypeError, ValueError):
            continue
        if (mode & 0x7F) == MagneticProtocol.MODE_SNAP:
            snap_slots.append(slot)
    snap_slots = sorted(set(snap_slots))
    return [snap_slots] if len(snap_slots) == 2 else []


def _normalise_magnetic_keyboard_options_transfer(values: object) -> dict:
    if not isinstance(values, dict):
        raise ValueError("параметры Magnetic Lab должны быть объектом")
    fn_index = values.get("fn_index", 0)
    rt_stab = values.get("rt_stab", 0)
    anti_accidental = values.get("anti_accidental", False)
    wasd_swap = values.get("wasd_swap", False)
    system = values.get("system", "win")
    if (
        isinstance(fn_index, bool)
        or not isinstance(fn_index, int)
        or isinstance(rt_stab, bool)
        or not isinstance(rt_stab, int)
        or not isinstance(anti_accidental, bool)
        or not isinstance(wasd_swap, bool)
        or not isinstance(system, str)
    ):
        raise ValueError("некорректные параметры Magnetic Lab")
    try:
        options = KeyboardOptions(
            fn_index=fn_index,
            anti_accidental=anti_accidental,
            rt_stab=rt_stab,
            wasd_swap=wasd_swap,
            system=system,
        )
    except (TypeError, ValueError, MagneticProtocolError) as exc:
        raise ValueError("некорректные параметры Magnetic Lab") from exc
    return {
        "fn_index": options.fn_index,
        "anti_accidental": options.anti_accidental,
        "rt_stab": options.rt_stab,
        "wasd_swap": options.wasd_swap,
        "system": options.system,
    }


def _normalise_magnetic_lab_transfer(values: object) -> dict:
    """Validate Magnetic Lab state without ever accepting a HID payload."""
    if not isinstance(values, dict):
        raise ValueError("Magnetic Lab должен быть объектом")
    selected = values.get("selected_profile", 0)
    if (
        isinstance(selected, bool)
        or not isinstance(selected, int)
        or not 0 <= selected < MAGNETIC_PROFILE_COUNT
    ):
        raise ValueError("некорректный выбранный магнитный профиль")
    raw_profiles = values.get("profiles")
    if not isinstance(raw_profiles, dict):
        raise ValueError("магнитные профили должны быть объектом")
    profiles = {}
    for index in range(MAGNETIC_PROFILE_COUNT):
        raw_profile = raw_profiles.get(str(index), raw_profiles.get(index))
        if not isinstance(raw_profile, dict):
            raise ValueError("неполный набор магнитных профилей")
        key_modes = _normalise_magnetic_mode_transfer(
            raw_profile.get("key_modes", {})
        )
        profiles[str(index)] = {
            "key_settings": _normalise_magnetic_settings_transfer(
                raw_profile.get("key_settings", {})
            ),
            "key_modes": key_modes,
            "keyboard_options": _normalise_magnetic_keyboard_options_transfer(
                raw_profile.get("keyboard_options", {})
            ),
            "rt_separate": _normalise_magnetic_mode_transfer(
                raw_profile.get("rt_separate", {}), boolean=True
            ),
            "snap_pairs": _normalise_magnetic_snap_pairs_transfer(
                raw_profile.get("snap_pairs", []), key_modes=key_modes
            ),
            "initialized": bool(raw_profile.get("initialized", False)),
        }
    return {"selected_profile": selected, "profiles": profiles}


def _magnetic_lab_transfer_from_entry(entry: dict) -> dict:
    """Build compact Magnetic Lab state, excluding cache journals/HID bytes."""
    seed = {
        "magnetic_key_settings": entry.get("magnetic_key_settings"),
        "magnetic_key_modes": entry.get("magnetic_key_modes"),
        "magnetic_keyboard_options": entry.get("magnetic_keyboard_options"),
        "magnetic_rt_separate": entry.get("magnetic_rt_separate"),
        "magnetic_snap_pairs": entry.get("magnetic_snap_pairs"),
        "magnetic_profiles": entry.get("magnetic_profiles"),
        "magnetic_selected_profile": entry.get("magnetic_selected_profile", 0),
    }
    _normalize_magnetic_profile_slots(seed)

    def known_slots(values: object) -> dict:
        """Firmware reads can contain unused matrix slots; they are not keys."""
        if not isinstance(values, dict):
            return {}
        result = {}
        for raw_slot, value in values.items():
            try:
                result[_normalise_magnetic_slot(raw_slot)] = value
            except ValueError:
                # This is a local firmware/cache artefact, not a user-visible
                # SK75 switch.  Do not make a valid export fail because it is
                # present in a historic read of the full 81-slot matrix.
                continue
        return result

    profiles = {}
    for index, profile in seed["magnetic_profiles"].items():
        if not isinstance(profile, dict):
            continue
        profiles[str(index)] = {
            **profile,
            "key_settings": known_slots(profile.get("key_settings")),
            "key_modes": known_slots(profile.get("key_modes")),
            "rt_separate": known_slots(profile.get("rt_separate")),
        }
    return _normalise_magnetic_lab_transfer(
        {
            "selected_profile": seed["magnetic_selected_profile"],
            "profiles": profiles,
        }
    )


def _portable_profile_rule_devices_from_config(config: object) -> list[dict]:
    """Extract names/rules from a legacy full configuration without its state.

    This accepts the old 250k+ clipboard documents, but deliberately ignores
    payload bytes, RGB, magnetic settings, battery data and all other device
    state.  It is also the sole source used by the new compact exporter.
    """
    if not isinstance(config, dict):
        raise ValueError("ожидается объект конфигурации")
    devices = config.get("devices")
    if not isinstance(devices, dict):
        raise ValueError("ожидается конфигурация с объектом devices")
    if len(devices) > MAX_PROFILE_RULES_DEVICES:
        raise ValueError(f"слишком много устройств: максимум {MAX_PROFILE_RULES_DEVICES}")

    rules = []
    for device_key, entry in devices.items():
        if not isinstance(device_key, str) or not device_key.strip() or not isinstance(entry, dict):
            raise ValueError("некорректное устройство в конфигурации")
        payloads = entry.get("payloads")
        if payloads is None:
            payloads = {}
        else:
            _validate_ignored_profile_payloads(
                payloads, context=f"устройство {device_key!r}"
            )
        profile_names = [
            _normalise_profile_rule_name(name, index)
            for index, name in enumerate(payloads.keys(), start=1)
        ]
        if len(profile_names) > MAX_PROFILE_RULES_PER_DEVICE:
            raise ValueError(
                f"слишком много профилей: максимум {MAX_PROFILE_RULES_PER_DEVICE}"
            )
        lowered_names = [name.casefold() for name in profile_names]
        if len(set(lowered_names)) != len(lowered_names):
            raise ValueError("имена профилей не должны повторяться")
        if not profile_names:
            # A device with no profile slots cannot meaningfully have a process
            # binding.  Do not export an unusable empty rule object.
            continue
        bindings = _normalise_profile_rule_bindings(
            entry.get("bindings"), len(profile_names)
        )
        identity = {}
        for field_name in ("vid", "pid", "usage_page"):
            value = entry.get(field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 0xFFFF
            ):
                # Older configs can omit an identity; exact device_key matching
                # still makes a safe transfer possible on the same keyboard.
                identity = {}
                break
            identity[field_name] = value
        rules.append(
            {
                "device_key": device_key,
                "identity": identity,
                "profile_names": profile_names,
                "bindings": bindings,
            }
        )
    return rules


def _portable_config_transfer_devices_from_config(
    config: object, sections: object
) -> list[dict]:
    """Extract selected portable settings and deliberately omit HID payloads."""
    selected = _normalise_transfer_sections(sections)
    if not isinstance(config, dict):
        raise ValueError("ожидается объект конфигурации")
    devices = config.get("devices")
    if not isinstance(devices, dict):
        raise ValueError("ожидается конфигурация с объектом devices")
    if len(devices) > MAX_PROFILE_RULES_DEVICES:
        raise ValueError(f"слишком много устройств: максимум {MAX_PROFILE_RULES_DEVICES}")

    rules = []
    needs_profiles = (
        TRANSFER_SECTION_PROFILE_NAMES in selected
        or TRANSFER_SECTION_PROCESS_BINDINGS in selected
    )
    for device_key, entry in devices.items():
        if not isinstance(device_key, str) or not device_key.strip() or not isinstance(entry, dict):
            raise ValueError("некорректное устройство в конфигурации")
        payloads = entry.get("payloads")
        if not isinstance(payloads, dict):
            payloads = {}
        names = [
            _normalise_profile_rule_name(name, index)
            for index, name in enumerate(payloads.keys(), start=1)
        ]
        if len(names) > MAX_PROFILE_RULES_PER_DEVICE:
            raise ValueError(f"слишком много профилей: максимум {MAX_PROFILE_RULES_PER_DEVICE}")
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("имена профилей не должны повторяться")

        rule = {
            "device_key": device_key,
            "identity": _normalise_device_identity(
                {field: entry.get(field) for field in ("vid", "pid", "usage_page")},
                len(rules) + 1,
            ),
        }
        if needs_profiles and names:
            rule["profile_count"] = len(names)
        if TRANSFER_SECTION_PROFILE_NAMES in selected and names:
            rule["profile_names"] = names
            raw_default = entry.get("default_profile_index")
            rule["default_profile_index"] = (
                raw_default
                if isinstance(raw_default, int)
                and not isinstance(raw_default, bool)
                and 0 <= raw_default < len(names)
                else None
            )
        if TRANSFER_SECTION_PROCESS_BINDINGS in selected and names:
            rule["bindings"] = _normalise_profile_rule_bindings(
                entry.get("bindings"), len(names)
            )
        if TRANSFER_SECTION_LIGHTING_LAB in selected:
            rule["lighting_lab"] = LightingSettings.from_config(
                entry.get("lighting_lab")
            ).to_config()
        if TRANSFER_SECTION_MAGNETIC_LAB in selected:
            rule["magnetic_lab"] = _magnetic_lab_transfer_from_entry(entry)
        # A non-profile-only transfer can still target a device with no
        # profile slots.  Do not emit an empty shell, though.
        if len(rule) > 2:
            rules.append(rule)
    return rules


def _normalise_selected_config_transfer(loaded: object) -> dict:
    """Parse selected config sections while retaining old clipboard support."""
    if not isinstance(loaded, dict):
        raise ValueError("ожидается JSON-объект")
    transfer_format = loaded.get("format")
    if transfer_format == CONFIG_TRANSFER_FORMAT:
        version = loaded.get("version")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != CONFIG_TRANSFER_VERSION
        ):
            raise ValueError("неподдерживаемая версия конфигурации")
        selected = _normalise_transfer_sections(loaded.get("categories"))
        raw_rules = loaded.get("devices")
    elif transfer_format == PROFILE_RULES_TRANSFER_FORMAT:
        version = loaded.get("version")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != PROFILE_RULES_TRANSFER_VERSION
        ):
            raise ValueError("неподдерживаемая версия правил")
        selected = _normalise_transfer_sections(
            (TRANSFER_SECTION_PROFILE_NAMES, TRANSFER_SECTION_PROCESS_BINDINGS)
        )
        raw_rules = loaded.get("devices")
    else:
        # Historic full documents are reduced to the safe fields copied by the
        # old public UI.  They can never revive old raw payload/HID state.
        if transfer_format == LEGACY_CONFIG_TRANSFER_FORMAT:
            version = loaded.get("version")
            if (
                isinstance(version, bool)
                or not isinstance(version, int)
                or version != LEGACY_CONFIG_TRANSFER_VERSION
            ):
                raise ValueError("неподдерживаемая версия конфигурации")
            candidate = loaded.get("config")
        else:
            if "format" in loaded:
                raise ValueError("неизвестный формат конфигурации")
            candidate = loaded
        selected = _normalise_transfer_sections(
            (TRANSFER_SECTION_PROFILE_NAMES, TRANSFER_SECTION_PROCESS_BINDINGS)
        )
        return {
            "categories": list(selected),
            "devices": _portable_config_transfer_devices_from_config(candidate, selected),
        }

    if not isinstance(raw_rules, list):
        raise ValueError("devices должен быть списком")
    if len(raw_rules) > MAX_PROFILE_RULES_DEVICES:
        raise ValueError(f"слишком много устройств: максимум {MAX_PROFILE_RULES_DEVICES}")

    normalised_rules = []
    needs_profiles = (
        TRANSFER_SECTION_PROFILE_NAMES in selected
        or TRANSFER_SECTION_PROCESS_BINDINGS in selected
    )
    for device_index, raw_rule in enumerate(raw_rules, start=1):
        if not isinstance(raw_rule, dict):
            raise ValueError(f"устройство {device_index} должно быть объектом")
        # ``payloads`` is not a supported public CFG section: profile HID
        # bytes stay on the receiving keyboard.  Validate a legacy/manual
        # field before intentionally discarding it so malformed data cannot
        # be mistaken for a successful import or reach a later direct call.
        if "payloads" in raw_rule:
            _validate_ignored_profile_payloads(
                raw_rule["payloads"], context=f"устройство {device_index}"
            )
        device_key = raw_rule.get("device_key")
        if not isinstance(device_key, str) or not device_key.strip() or len(device_key) > 160:
            raise ValueError(f"некорректный ключ устройства {device_index}")
        rule = {
            "device_key": device_key,
            "identity": _normalise_device_identity(raw_rule.get("identity", {}), device_index),
        }
        profile_count = None
        if needs_profiles:
            raw_names = raw_rule.get("profile_names")
            # V2 always contained labels.  A V3 process-only document carries
            # profile_count instead so it does not leak profile names.
            if TRANSFER_SECTION_PROFILE_NAMES in selected:
                if not isinstance(raw_names, list) or not raw_names:
                    raise ValueError(f"profile_names устройства {device_index} должен быть непустым списком")
                profile_count = _normalise_profile_count(len(raw_names), device_index=device_index)
                names = [
                    _normalise_profile_rule_name(value, position)
                    for position, value in enumerate(raw_names, start=1)
                ]
                if len({name.casefold() for name in names}) != len(names):
                    raise ValueError("имена профилей не должны повторяться")
                rule["profile_names"] = names
                rule["default_profile_index"] = _normalise_default_profile_index(
                    raw_rule.get("default_profile_index"), profile_count
                )
            else:
                profile_count = _normalise_profile_count(
                    raw_rule.get("profile_count"), device_index=device_index
                )
            rule["profile_count"] = profile_count
        if TRANSFER_SECTION_PROCESS_BINDINGS in selected:
            rule["bindings"] = _normalise_profile_rule_bindings(
                raw_rule.get("bindings"), profile_count
            )
        if TRANSFER_SECTION_LIGHTING_LAB in selected:
            rule["lighting_lab"] = LightingSettings.from_config(
                raw_rule.get("lighting_lab")
            ).to_config()
        if TRANSFER_SECTION_MAGNETIC_LAB in selected:
            rule["magnetic_lab"] = _normalise_magnetic_lab_transfer(
                raw_rule.get("magnetic_lab")
            )
        normalised_rules.append(rule)
    return {"categories": list(selected), "devices": normalised_rules}


def _normalise_config_transfer_for_apply(transfer: object) -> dict:
    """Revalidate a selected CFG document at the persistence boundary.

    The dialog already parses clipboard text before invoking the merger, but
    this method is also used by compatibility helpers and focused integrations.
    Rebuilding the tiny versioned wrapper ensures every route applies the same
    strict profile/magnetic validation before it can cancel a worker or write
    the local JSON file.  Unknown fields remain intentionally omitted.
    """
    if not isinstance(transfer, dict):
        raise ValueError("некорректная конфигурация для импорта")
    return _normalise_selected_config_transfer(
        {
            "format": CONFIG_TRANSFER_FORMAT,
            "version": CONFIG_TRANSFER_VERSION,
            "categories": transfer.get("categories"),
            "devices": transfer.get("devices"),
        }
    )


def _normalise_profile_rules_transfer(loaded: object) -> dict:
    """Backward-compatible name for the selected-config parser."""
    return _normalise_selected_config_transfer(loaded)


def _parse_config_transfer_text(text: object) -> dict:
    """Safely parse selected configuration without rendering it in Flet."""
    if not isinstance(text, str):
        raise ValueError("ожидается текстовая JSON-конфигурация")
    if len(text) > MAX_CONFIG_IMPORT_CHARS:
        raise ValueError(
            f"конфигурация слишком большая: максимум {MAX_CONFIG_IMPORT_CHARS:,} символов"
        )
    if not text.strip():
        raise ValueError("JSON-конфигурация пуста")
    try:
        loaded = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_config_json_keys,
            parse_constant=_reject_nonstandard_config_json_constant,
        )
    except RecursionError as exc:
        raise ValueError("JSON-конфигурация имеет слишком большую вложенность") from exc
    return _normalise_selected_config_transfer(loaded)


def _parse_profile_rules_transfer_text(text: object) -> dict:
    """Backward-compatible name for the selected configuration parser."""
    return _parse_config_transfer_text(text)


def _normalise_imported_configuration(loaded: object) -> dict:
    """Validate and detach a copy/paste configuration before it is saved.

    This intentionally accepts the former bare-object export as well as the
    current versioned wrapper.  It never mutates the parsed clipboard object,
    removes transient runtime aliases, and keeps every device, binding,
    lighting and magnetic-preset field JSON-compatible for a complete
    round-trip.
    """
    if isinstance(loaded, dict) and "format" in loaded:
        # This helper remains only for compatibility with historical *full*
        # backups.  The public clipboard path now uses
        # ``_normalise_profile_rules_transfer`` and never calls this route.
        if loaded.get("format") != LEGACY_CONFIG_TRANSFER_FORMAT:
            raise ValueError("неизвестный формат конфигурации")
        version = loaded.get("version")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != LEGACY_CONFIG_TRANSFER_VERSION
        ):
            raise ValueError("неподдерживаемая версия конфигурации")
        candidate = loaded.get("config")
    else:
        # Keep accepting the pre-versioned bare JSON object generated by old
        # releases.  It is still validated and detached below.
        candidate = loaded
    if not isinstance(candidate, dict) or not isinstance(candidate.get("devices", {}), dict):
        raise ValueError("ожидается конфигурация с объектом devices")

    # A JSON round trip rejects Python-only values and prevents aliases to a
    # parsed clipboard object from leaking into the running configuration.
    normalized = _json_copy(candidate)
    normalized["mode"] = "auto"
    for alias in CONFIG_RUNTIME_ALIAS_KEYS:
        normalized.pop(alias, None)
    settings = normalized.get("settings")
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("settings должен быть JSON-объектом")
    if settings is None:
        settings = {}
        normalized["settings"] = settings
    # A direct clipboard import must remain authoritative after the next
    # application restart as well.  Without this durable flag, the one-time
    # Womier Local Storage migration could replace all pasted SK75 presets
    # merely because the destination PC has its own official-driver cache.
    settings[CONFIG_TRANSFER_WOMIER_GUARD_KEY] = True

    devices = normalized["devices"]
    active_device = normalized.get("active_device")
    if active_device is not None and (
        not isinstance(active_device, str) or active_device not in devices
    ):
        raise ValueError("активное устройство отсутствует в devices")

    for device_key, entry in devices.items():
        if not isinstance(device_key, str) or not device_key.strip():
            raise ValueError("ключ устройства должен быть непустой строкой")
        if not isinstance(entry, dict):
            raise ValueError(f"устройство {device_key!r} должно быть JSON-объектом")
        # These fields are accessed directly when the active-device aliases
        # are rebuilt.  Reject a malformed clipboard file before it can leave
        # the application with an unusable selected device.
        for field_name in ("vid", "pid", "usage_page"):
            field_value = entry.get(field_name)
            if (
                isinstance(field_value, bool)
                or not isinstance(field_value, int)
                or not 0 <= field_value <= 0xFFFF
            ):
                raise ValueError(
                    f"устройство {device_key!r}: {field_name} должен быть числом 0–65535"
                )
        for field_name in (
            "payloads",
            "magnetic_key_settings",
            "magnetic_key_modes",
            "magnetic_keyboard_options",
            "magnetic_rt_separate",
            "magnetic_profiles",
            "womier_cache_sync_pending",
        ):
            field_value = entry.get(field_name)
            if field_value is not None and not isinstance(field_value, dict):
                raise ValueError(f"устройство {device_key!r}: {field_name} должен быть объектом")
        for field_name in ("bindings", "magnetic_snap_pairs"):
            field_value = entry.get(field_name)
            if field_value is not None and not isinstance(field_value, list):
                raise ValueError(f"устройство {device_key!r}: {field_name} должен быть списком")
        battery = entry.get("battery")
        if battery is not None and not isinstance(battery, dict):
            raise ValueError(f"устройство {device_key!r}: battery должен быть объектом")

        payloads = entry.get("payloads") or {}
        for profile_name, profile in payloads.items():
            if not isinstance(profile_name, str) or not profile_name.strip():
                raise ValueError(
                    f"устройство {device_key!r}: имя профиля должно быть непустой строкой"
                )
            if not isinstance(profile, dict):
                raise ValueError(
                    f"устройство {device_key!r}: профиль {profile_name!r} должен быть объектом"
                )
            payload = profile.get("data")
            if payload is not None:
                checked_payload = _valid_profile_payload_bytes(payload)
                if checked_payload is None:
                    raise ValueError(
                        f"устройство {device_key!r}: data профиля {profile_name!r} "
                        f"должен быть списком до {MAX_PROFILE_PAYLOAD_BYTES} байт"
                    )
                # The deep JSON copy above already detached this list.  Assign
                # a fresh list anyway so later normalization cannot retain an
                # alias from a custom JSON decoder.
                profile["data"] = checked_payload
            # Hotkeys belonged to the removed manual-mode implementation;
            # retaining one in an imported file must not revive it.
            profile.pop("hotkey", None)
    return normalized


def _magnetic_settings_config_values(settings: KeyMagneticSettings) -> dict:
    """Serialize one already-validated magnetic settings object."""
    return {
        "actuation": settings.actuation,
        "rapid_trigger": settings.rapid_trigger,
        "rapid_press": settings.rapid_press,
        "rapid_release": settings.rapid_release,
        "lower_dead_zone": settings.lower_dead_zone,
        "upper_dead_zone": settings.upper_dead_zone,
        # ``liftTravel`` / operation 1 is the ordinary-mode point at which a
        # key deactivates.  It is intentionally not the RT release value.
        "deactivation": settings.deactivation,
    }


def _sanitize_magnetic_settings_mapping(values: object) -> dict:
    """Keep local presets within the official current-SK75 UI range.

    This migration is deliberately pure JSON work: it does not construct HID
    packets or write to a keyboard.  Calibration's ``0xFE`` progress words are
    not used here because Womier uses them only to normalise the sensor, not to
    report a physical per-key maximum travel in millimetres.
    """
    if not isinstance(values, dict):
        return {}
    sanitized = _json_copy(values)
    for slot, raw_settings in list(sanitized.items()):
        if not isinstance(raw_settings, dict):
            continue
        try:
            settings = KeyMagneticSettings(
                actuation=raw_settings["actuation"],
                rapid_trigger=raw_settings["rapid_trigger"],
                rapid_press=raw_settings["rapid_press"],
                rapid_release=raw_settings["rapid_release"],
                lower_dead_zone=raw_settings["lower_dead_zone"],
                upper_dead_zone=raw_settings["upper_dead_zone"],
                # Older exported configurations had no independent normal
                # deactivation point.  Starting it at actuation preserves
                # their behaviour until the user adjusts the new ruler.
                deactivation=raw_settings.get(
                    "deactivation",
                    raw_settings.get("lift_travel", raw_settings["actuation"]),
                ),
            )
            bounded = MagneticProtocol.clamp_key_settings_to_official_bounds(
                settings
            )
        except (KeyError, TypeError, ValueError, MagneticProtocolError):
            # Preserve malformed/partial legacy data rather than replacing it
            # with guessed key settings.  It remains inert until a user chooses
            # that key and explicitly changes a value.
            continue
        raw_settings.update(_magnetic_settings_config_values(bounded))
        sanitized[slot] = raw_settings
    return sanitized


def _magnetic_profile_snapshot_from_live(entry: dict) -> dict:
    """Make one portable magnetic-preset snapshot from the live cache.

    The old configuration format had a single set of ``magnetic_*`` fields.
    They remain the cache of what is currently on the keyboard, while this
    helper provides a safe migration seed for the four local presets.
    """
    key_settings = entry.get("magnetic_key_settings")
    key_modes = entry.get("magnetic_key_modes")
    keyboard_options = entry.get("magnetic_keyboard_options")
    rt_separate = entry.get("magnetic_rt_separate")
    snap_pairs = entry.get("magnetic_snap_pairs")
    safe_snap_pairs = _safe_magnetic_snap_pairs(snap_pairs, key_modes)
    return {
        "key_settings": _sanitize_magnetic_settings_mapping(key_settings),
        "key_modes": _json_copy(key_modes) if isinstance(key_modes, dict) else {},
        "keyboard_options": _json_copy(keyboard_options) if isinstance(keyboard_options, dict) else {},
        "rt_separate": _json_copy(rt_separate) if isinstance(rt_separate, dict) else {},
        "snap_pairs": safe_snap_pairs,
        "initialized": bool(
            key_settings or key_modes or keyboard_options or rt_separate or safe_snap_pairs
        ),
    }


def _normalize_magnetic_profile_slots(entry: dict) -> None:
    """Normalize four independent, local magnetic-setting presets.

    A profile snapshot is deliberately JSON-only so it is copied by the
    existing export/import feature.  We preserve the legacy live cache and
    never use a guessed firmware profile field in a HID packet.
    """
    # Canonicalise only local/cache JSON.  This cannot send a HID command; a
    # key is applied only by an explicit slider edit or selected preset.
    entry["magnetic_key_settings"] = _sanitize_magnetic_settings_mapping(
        entry.get("magnetic_key_settings")
    )
    raw_slots = entry.get("magnetic_profiles")
    if not isinstance(raw_slots, dict):
        raw_slots = {}
    live_seed = _magnetic_profile_snapshot_from_live(entry)
    slots = {}
    for index in range(MAGNETIC_PROFILE_COUNT):
        candidate = raw_slots.get(str(index), raw_slots.get(index))
        if not isinstance(candidate, dict):
            candidate = live_seed
        key_settings = candidate.get("key_settings", candidate.get("magnetic_key_settings"))
        key_modes = candidate.get("key_modes", candidate.get("magnetic_key_modes"))
        keyboard_options = candidate.get("keyboard_options", candidate.get("magnetic_keyboard_options"))
        rt_separate = candidate.get("rt_separate", candidate.get("magnetic_rt_separate"))
        snap_pairs = candidate.get("snap_pairs", candidate.get("magnetic_snap_pairs", []))
        safe_snap_pairs = _safe_magnetic_snap_pairs(snap_pairs, key_modes)
        # A few early builds wrote ``initialized: false`` next to an already
        # populated snapshot.  Treat the data itself as authoritative: leaving
        # that stale flag untouched lets the first startup HID read silently
        # seed (and therefore replace) profiles 2–4 with profile 1's live
        # values.  An empty profile still remains uninitialized and is seeded
        # normally on its first real keyboard read.
        has_snapshot_data = bool(
            key_settings or key_modes or keyboard_options or rt_separate or safe_snap_pairs
        )
        initialized = candidate.get("initialized")
        if initialized is not True:
            initialized = has_snapshot_data
        slots[str(index)] = {
            "key_settings": _sanitize_magnetic_settings_mapping(key_settings),
            "key_modes": _json_copy(key_modes) if isinstance(key_modes, dict) else {},
            "keyboard_options": _json_copy(keyboard_options) if isinstance(keyboard_options, dict) else {},
            "rt_separate": _json_copy(rt_separate) if isinstance(rt_separate, dict) else {},
            "snap_pairs": safe_snap_pairs,
            "initialized": initialized,
        }
    entry["magnetic_profiles"] = slots
    try:
        selected = int(entry.get("magnetic_selected_profile", 0))
    except (TypeError, ValueError):
        selected = 0
    entry["magnetic_selected_profile"] = max(0, min(MAGNETIC_PROFILE_COUNT - 1, selected))


def _legacy_source_fingerprint(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _load_json_object(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("корень конфигурации должен быть JSON-объектом")
    return data


def _merge_legacy_config(current_config: dict, legacy_config: dict) -> tuple[dict, dict]:
    """Safely merge the old desktop config into a current multi-device config.

    The current configuration remains authoritative for hardware-derived data
    (HID payload bytes, lighting and magnetic settings).  From the old config
    we add missing process rules and transfer the four profile names plus their
    profile-level options.  This means a migration cannot reset keys or RGB.
    """
    if not isinstance(current_config, dict):
        raise ValueError("текущая конфигурация повреждена")
    if not isinstance(legacy_config, dict) or not isinstance(legacy_config.get("devices"), dict):
        raise ValueError("в старом файле нет объекта devices")

    merged = _json_copy(current_config)
    devices = merged.setdefault("devices", {})
    if not isinstance(devices, dict):
        raise ValueError("в текущем файле нет корректного объекта devices")

    report = {
        "devices_added": 0,
        "devices_merged": 0,
        "bindings_added": 0,
        "profiles_updated": 0,
    }

    for raw_key, source_entry in legacy_config["devices"].items():
        if not isinstance(raw_key, str) or not isinstance(source_entry, dict):
            continue
        source_payloads = source_entry.get("payloads")
        source_bindings = source_entry.get("bindings")
        target_entry = devices.get(raw_key)

        if not isinstance(target_entry, dict):
            new_entry = _json_copy(source_entry)
            # Manual hotkeys were removed from this app.  Do not revive them
            # while importing an otherwise useful old profile.
            for profile in (new_entry.get("payloads") or {}).values():
                if isinstance(profile, dict):
                    profile.pop("hotkey", None)
            devices[raw_key] = new_entry
            report["devices_added"] += 1
            report["bindings_added"] += len([
                binding for binding in (source_bindings or [])
                if isinstance(binding, dict) and str(binding.get("process", "")).strip()
            ])
            report["profiles_updated"] += len(source_payloads) if isinstance(source_payloads, dict) else 0
            continue

        report["devices_merged"] += 1
        # Fill only missing device metadata.  Existing data came from the
        # currently connected keyboard and is safer than stale legacy data.
        for field_name in ("vid", "pid", "usage_page", "label", "transport", "keyboard_type"):
            if target_entry.get(field_name) in (None, "") and field_name in source_entry:
                target_entry[field_name] = _json_copy(source_entry[field_name])
        if not isinstance(target_entry.get("battery"), dict) and isinstance(source_entry.get("battery"), dict):
            target_entry["battery"] = _json_copy(source_entry["battery"])
        if not isinstance(target_entry.get("cooldown_ms"), int) or target_entry.get("cooldown_ms", 0) <= 0:
            source_cooldown = source_entry.get("cooldown_ms")
            if isinstance(source_cooldown, int) and source_cooldown >= 0:
                target_entry["cooldown_ms"] = source_cooldown

        # Profile slots, unlike names, are fixed by firmware.  Merge by slot:
        # retain the current HID payload bytes, then attach the old names and
        # profile options such as polling rate.
        if isinstance(source_payloads, dict) and source_payloads:
            current_items = list((target_entry.get("payloads") or {}).items())
            merged_payloads = {}
            used_names = set()
            for slot, (source_name, source_profile) in enumerate(source_payloads.items()):
                current_name, current_profile = (
                    current_items[slot] if slot < len(current_items) else (f"Профиль {slot + 1}", {})
                )
                profile = _json_copy(current_profile) if isinstance(current_profile, dict) else {}
                if isinstance(source_profile, dict):
                    for option_name, option_value in source_profile.items():
                        if option_name not in ("data", "hotkey"):
                            profile[option_name] = _json_copy(option_value)
                name = str(source_name).strip() or str(current_name).strip() or f"Профиль {slot + 1}"
                # Invalid legacy files can contain duplicate names; preserve
                # all slots by giving only the duplicate a harmless suffix.
                base_name = name
                duplicate = 2
                while name in used_names:
                    name = f"{base_name} ({duplicate})"
                    duplicate += 1
                used_names.add(name)
                merged_payloads[name] = profile
                report["profiles_updated"] += 1
            for current_name, current_profile in current_items[len(merged_payloads):]:
                name = str(current_name).strip() or f"Профиль {len(merged_payloads) + 1}"
                while name in used_names:
                    name = f"{name} (копия)"
                used_names.add(name)
                merged_payloads[name] = _json_copy(current_profile) if isinstance(current_profile, dict) else {}
            target_entry["payloads"] = merged_payloads

        # Current rules win on duplicate process names; rules that are absent
        # locally are appended, so repeated imports are idempotent.
        target_bindings = target_entry.get("bindings")
        if not isinstance(target_bindings, list):
            target_bindings = []
        known_processes = {
            str(binding.get("process", "")).strip().lower()
            for binding in target_bindings
            if isinstance(binding, dict) and str(binding.get("process", "")).strip()
        }
        if isinstance(source_bindings, list):
            for source_binding in source_bindings:
                if not isinstance(source_binding, dict):
                    continue
                process = str(source_binding.get("process", "")).strip().lower()
                profile_index = source_binding.get("profile_index")
                if not process or not isinstance(profile_index, int) or profile_index < 0:
                    continue
                if process in known_processes:
                    continue
                binding = {"process": process, "profile_index": profile_index}
                if isinstance(source_binding.get("enabled"), bool):
                    binding["enabled"] = source_binding["enabled"]
                target_bindings.append(binding)
                known_processes.add(process)
                report["bindings_added"] += 1
        target_entry["bindings"] = target_bindings

    # Retain the user's current startup choices.  A browser path is harmless
    # to fill when absent and is useful for the old sniffer setup.
    current_settings = merged.setdefault("settings", {})
    legacy_settings = legacy_config.get("settings")
    if isinstance(current_settings, dict) and isinstance(legacy_settings, dict):
        if not str(current_settings.get("browser_path", "")).strip() and legacy_settings.get("browser_path"):
            current_settings["browser_path"] = str(legacy_settings["browser_path"])
    merged["mode"] = "auto"
    return merged, report


def _stage_delay_ms(entry: dict | None, stage: str) -> int:
    transport = (entry or {}).get("transport") or "wired"
    delays = WIRELESS_STAGE_DELAYS_MS if transport == "wireless" else WIRED_STAGE_DELAYS_MS
    return delays.get(stage, 0)


def _setup_logging(debug: bool) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    if debug:
        handler = logging.FileHandler(paths.log_path, mode="w", encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        for noisy in ("flet", "flet_core", "flet_runtime", "flet_controls",
                       "flet_transport", "flet_desktop", "PIL", "PIL.Image"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    else:
        root.setLevel(logging.WARNING)


def _load_local_update_state() -> dict:
    return {
        "enabled": False,
        "checked_at": None,
        "latest_version": None,
        "error": None,
    }


def _default_profile_payload(idx: int, opcode: int = 0x04) -> list:
    kb_info = next((v for v in KEYBOARD_TYPES.values() if v["opcode"] == opcode), None)
    checksum_base = kb_info["checksum_base"] if kb_info else 0xFB
    payload = [0] * 64
    payload[0] = opcode
    payload[1] = idx & 0xFF
    payload[7] = (checksum_base - idx) & 0xFF
    return payload

def _release_all_keys():
    user32 = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_EXTENDEDKEY = 0x0001
    _modifiers = [
        (0xA0, 0x2A, False),   # VK_LSHIFT
        (0xA1, 0x36, False),   # VK_RSHIFT
        (0xA2, 0x1D, False),   # VK_LCONTROL
        (0xA3, 0x1D, True),    # VK_RCONTROL (extended)
        (0xA4, 0x38, False),   # VK_LMENU (Alt)
        (0xA5, 0x38, True),    # VK_RMENU (extended)
        (0x5B, 0x5B, True),    # VK_LWIN
        (0x5C, 0x5C, True),    # VK_RWIN
    ]
    for vk, scan, extended in _modifiers:
        flags = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if extended else 0)
        user32.keybd_event(vk, scan, flags, 0)
    for vk in range(0x08, 0xFF):
        if user32.GetAsyncKeyState(vk) & 0x8000:
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def _suppress_keyboard(duration_ms):
    def _suppress_callback(event):
        if event.event_type == keyboard.KEY_UP:
            return True
        return False

    hook = keyboard.hook(_suppress_callback, suppress=True)
    logger.debug("_suppress_keyboard: hook installed for %dms", duration_ms)

    _release_all_keys()

    def _unhook_later():
        time.sleep(duration_ms / 1000.0)
        keyboard.unhook(hook)
        logger.debug("_suppress_keyboard: hook removed")

    threading.Thread(target=_unhook_later, daemon=True).start()


def _suppress_keyboard_start():
    def _suppress_callback(event):
        if event.event_type == keyboard.KEY_UP:
            return True
        return False
    hook = keyboard.hook(_suppress_callback, suppress=True)
    _release_all_keys()
    logger.debug("_suppress_keyboard_start: hook installed (transaction-bound)")
    return hook


try:
    # Keep taskbar grouping and notifications isolated from the generic
    # upstream manager.  ``APP_ID`` is shared with the public SK75 release's
    # single-instance integration, so a renamed EXE cannot accidentally use
    # the old application's identity.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
except Exception:
    pass


class QMKManager:
    @staticmethod
    def _resource_path(rel_path: str) -> str:
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, rel_path)

    @staticmethod
    def _center_icon_canvas(image: Image.Image) -> Image.Image:
        """Center an extracted Windows icon on a transparent square canvas.

        ``GetIconInfo`` exposes the coloured icon bitmap without the mask
        padding which Windows normally applies while drawing it.  The original
        QMK.Top Manager keyboard tile therefore has its visible pixels near
        the top of a 32 px canvas when it is copied directly into an ICO.  It
        looks noticeably higher than neighbouring taskbar icons.  Keep the
        original pixels and alpha, but move their visible bounds to the centre
        of the canvas before Flet and pystray consume the ICO.

        A fully transparent or non-alpha image is left as a valid transparent
        square rather than raising during startup; loading an icon is always a
        cosmetic best-effort operation.
        """
        rgba = image.convert("RGBA")
        alpha_bounds = rgba.getchannel("A").getbbox()
        side = max(rgba.width, rgba.height)
        if side <= 0:
            return rgba

        centered = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        if not alpha_bounds:
            return centered

        left, top, right, bottom = alpha_bounds
        visible = rgba.crop(alpha_bounds)
        x = max(0, (side - (right - left)) // 2)
        y = max(0, (side - (bottom - top)) // 2)
        centered.alpha_composite(visible, (x, y))
        return centered

    @staticmethod
    def _extract_original_qmk_icon(source: Path, destination: Path) -> str | None:
        """Extract the original manager's keyboard tile into a real ``.ico``.

        Flet's Windows ``Window.icon`` only accepts an ICO path.  The source
        checkout historically referenced a missing file in ``docs/`` while
        the user's original manager already has the exact familiar icon in
        its executable.  Extract it once into LocalAppData rather than
        relying on a temporary process icon or creating a black square around
        the transparent keyboard glyph.

        The helper is deliberately best-effort: an absent legacy installation
        must never block starting the keyboard manager.
        """
        if win32ui is None or not source.is_file():
            return None

        icon_handles = []
        icon_info = None
        temp_path = destination.with_suffix(".tmp.ico")
        try:
            large, small = win32gui.ExtractIconEx(str(source), 0)
            icon_handles = [*large, *small]
            if not icon_handles:
                return None

            # The large icon is first when the shell exposes one.  Its color
            # bitmap carries alpha; reading it directly preserves the rounded
            # transparent edges which DrawIconEx would flatten to black.
            icon_info = win32gui.GetIconInfo(icon_handles[0])
            color_bitmap_handle = icon_info[4]
            if not color_bitmap_handle:
                return None
            bitmap = win32ui.CreateBitmapFromHandle(color_bitmap_handle)
            bitmap_info = bitmap.GetInfo()
            width = int(bitmap_info.get("bmWidth", 0))
            height = int(bitmap_info.get("bmHeight", 0))
            if width <= 0 or height <= 0:
                return None
            bits = bitmap.GetBitmapBits(True)
            image = Image.frombuffer(
                "RGBA",
                (width, height),
                bits,
                "raw",
                "BGRA",
                0,
                1,
            ).transpose(Image.Transpose.FLIP_TOP_BOTTOM).copy()

            image = QMKManager._center_icon_canvas(image)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # A temporary same-directory write prevents a racing first frame
            # from seeing a partially written icon file.
            image.save(
                temp_path,
                format="ICO",
                sizes=[(16, 16), (20, 20), (24, 24), (32, 32)],
            )
            os.replace(temp_path, destination)
            return str(destination)
        except Exception:
            logger.debug("could not extract original QMK.Top Manager icon", exc_info=True)
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
            return None
        finally:
            if icon_info is not None:
                # GetIconInfo returns owned mask/color GDI bitmap handles.
                for bitmap_handle in icon_info[3:5]:
                    if bitmap_handle:
                        try:
                            win32gui.DeleteObject(bitmap_handle)
                        except Exception:
                            pass
            for icon_handle in icon_handles:
                try:
                    win32gui.DestroyIcon(icon_handle)
                except Exception:
                    pass

    def _resolve_application_icon_path(self) -> str | None:
        """Return the release-bundled ICO for the window and tray tile."""
        bundled = self._resource_path(QMK_TOP_MANAGER_ICON_RESOURCE)
        if os.path.isfile(bundled):
            return bundled
        # Development-only fallback for an incomplete checkout.  Public EXE
        # builds package the asset above, so no machine-specific original
        # manager path is ever consulted at runtime.
        return None

    def __init__(self, page: ft.Page, *, force_visible: bool = False):
        self.page = page
        # ``--show`` is intentionally a one-launch override.  It lets an
        # explicit manual restart open the driver visibly without changing
        # the user's persistent “start minimised to tray” preference.
        self.force_visible = bool(force_visible)
        self.config = self.load_config()
        _setup_logging(self.config.get("settings", {}).get("debug", False))
        logger.info("app started, config loaded")
        self._ensure_active_device_aliases()
        dev = self.config.get("device") or {}
        if dev and self.config.get("settings", {}).get("debug"):
            vid, pid = dev.get("vid", 0), dev.get("pid", 0)
            logger.debug("=== HID device map for VID=0x%04x PID=0x%04x ===", vid, pid)
            for d in hid.enumerate(vid, pid):
                logger.debug("  path=%s usage_page=0x%04x usage=0x%04x "
                             "interface=%d product=%s",
                             d["path"], d["usage_page"], d["usage"],
                             d.get("interface_number", -1),
                             d.get("product_string", "?"))
            logger.debug("=== end HID device map ===")
            self._diagnose_hid_endpoints()
        self.is_running = False
        self.worker_thread = None
        self.usb_lock = threading.Lock()
        # Only one diagnostic magnetic session may own the firmware at a time.
        # The travel tester uses 0x1B, whereas calibration uses Womier's
        # 0x1C/0x1E sequence.  Sharing this lock means an old tester always
        # restores its mode before calibration starts (and vice versa).
        self._magnetic_travel_session_lock = threading.Lock()
        self._magnetic_calibration_session_lock = self._magnetic_travel_session_lock
        # Lifecycle callbacks can originate from Flet, the tray thread and a
        # late dialog-dismiss event at nearly the same time.  Keep detaching a
        # calibration session atomic so exactly one worker owns its matching
        # firmware stop packet.
        self._magnetic_calibration_lifecycle_lock = threading.RLock()
        self._magnetic_calibration = None
        self._magnetic_calibration_token = 0
        # AlertDialog dismissal is delivered asynchronously by Flet.  Keep a
        # separate generation for the *window* as well as the HID session so
        # a late click/dismiss from a closed calibration dialog can never
        # start or stop a newly opened one.
        self._magnetic_calibration_dialog_token = 0
        # RT and separate-threshold switches change several persistent Flet
        # children at once.  Native callbacks normally arrive on one UI loop,
        # but a queued callback can overlap a direct test/tray transition while
        # the previous parent patch is still being applied.  Serialize that
        # small transaction instead of allowing two handlers to mutate the
        # same control map concurrently.
        self._magnetic_parameter_mode_transition_lock = threading.RLock()
        self._magnetic_parameter_mode_transition = False
        # Magnetic controls must never expose stale cached values as editable
        # values during the first paint.  They stay at a neutral zero display
        # until the read-only startup matrix request has confirmed the SK75.
        self._magnetic_values_ready = False
        self.app_alive = True
        try:
            self.application_icon_path = self._resolve_application_icon_path()
            if self.application_icon_path:
                set_icon_source(self.application_icon_path)
        except Exception:
            self.application_icon_path = None
            pass
        self.tray = TrayIcon(
            on_toggle_window=self._tray_toggle_window,
            on_show=self._tray_show_window,
            on_hide=self._tray_hide_window,
            on_quit=self._tray_quit,
        )
        self.battery_monitor = BatteryMonitor(
            config_battery=self.config["battery"],
            usb_lock=self.usb_lock,
            get_device_path=self.get_keyboard_path_safe,
            get_device_paths=self.get_keyboard_paths,
            on_working_path=self._cache_working_path,
            default_query=DEFAULT_BATTERY_QUERY,
        )
        self.battery_thread = None
        self.current_binding = None
        self.last_active_window = None
        # Foreground-window changes arrive much faster than a HID profile
        # transaction can complete (especially while Alt+Tab is held).  Keep
        # exactly one latest-wins request instead of letting the scanner send
        # a backlog of feature reports.  This coordinator is intentionally
        # separate from the Magnetic Lab preset coordinator below: it owns
        # ordinary keyboard-profile packets and can invalidate an in-flight
        # magnetic batch before a newly focused process needs the keyboard.
        self._auto_profile_switch_lock = threading.RLock()
        self._auto_profile_switch_timer = None
        self._auto_profile_switch_revision = 0
        self._auto_profile_switch_desired = None
        self._auto_profile_switch_worker_active = False
        self._auto_profile_switch_transport_uncertain = False
        self.binds_dict = {}
        self.rule_evaluator = RuleEvaluator()
        _entry = self._active_device()
        _dpi = _entry.get("default_profile_index") if _entry else None
        _pc = self._device_profile_count()
        self.default_profile_index = _dpi if isinstance(_dpi, int) and 0 <= _dpi < _pc else None
        self.devices = []
        self.filtered_devices = []
        self.sniffer = None
        # CDP callbacks arrive on the sniffer listener thread while clipboard
        # export/clear runs on Flet's UI thread.  A lock plus bounded deque
        # keeps those two paths deterministic and caps a noisy page at the
        # same 500 rows that the visible log already retains.
        self._sniff_events_lock = threading.RLock()
        self.sniff_events = deque(maxlen=SNIFF_EVENT_LIMIT)
        self._battery_captured_this_session = False
        self._battery_capture_attempts = 0
        self._battery_locked = False
        self._captured_profile_indices = set()
        # Кэш «рабочего» HID-интерфейса по ключу VID:PID:usage_page.
        # Многие клавиатуры (и в проводе, и в 2.4G) выставляют несколько
        # путей под одним usage_page; пишем во ВСЕ — а потом запоминаем тот,
        # что отвечает на feature-write успехом, чтобы дальше не перебирать.
        self._working_hid_path = {}
        # Sniffer "learn mode": when ON, _on_sniff_event bypasses the strict
        # pattern filter and logs every TX frame (and feature-report RX) with a
        # classification tag. Per spec §4 — per-session, never persisted.
        self._sniff_learn_mode = False
        self._battery_probe_queue = queue.Queue()
        self._battery_probe_thread = None
        self._battery_probe_stop = threading.Event()
        self.bt_report_id = None
        self.bt_response_length = None
        self.bt_response_offset = None
        self.bt_response_scale = None
        self.bt_charging_offset = None
        self.bt_charging_mask = None
        self.bt_result = None
        self.detected_browser_path = _find_chrome()
        self.update_check_state = _load_local_update_state()

        self._build_page()
        self._build_ui()
        self.refresh_devices()
        self.update_payloads_list()
        self.update_bindings_list()

        # Populate the visual keyboard from the real SK75 matrix as soon as
        # the first paint/device refresh has settled.  These are read-only HID
        # requests; no key setting is written during startup.
        def _initial_magnetic_read():
            time.sleep(0.7)
            self._read_magnetic_matrix(silent=True)
            # Keep RTStab populated even if a particular bulk-matrix read is
            # unavailable on a future firmware revision.
            time.sleep(0.5)
            self._magnetic_read_keyboard_options(silent=True)
        threading.Thread(target=_initial_magnetic_read, daemon=True, name="initial-magnetic-read").start()

        # Lighting settings are also persisted by the firmware.  Read them
        # once after the first paint so a previous Womier Dazzle/rainbow mode
        # cannot be displayed as a misleading solid HEX preview.
        def _initial_lighting_read():
            time.sleep(1.45)
            self._read_lighting_settings_from_keyboard(silent=True)
        threading.Thread(target=_initial_lighting_read, daemon=True, name="initial-lighting-read").start()

        self.tray.start()

        def _initial_battery():
            time.sleep(2)
            self._refresh_battery_for_tray()
        threading.Thread(target=_initial_battery, daemon=True).start()

        if (
            self.config.get("settings", {}).get("start_minimized", False)
            and not self.force_visible
        ):
            self.tray.set_window_visible(False)

        if self.config.get("settings", {}).get("autostart_service", False):
            def _deferred_auto_start():
                # Let Flet finish first paint before touching global keyboard
                # Let the desktop client finish its first paint before the
                # automatic window scanner starts.
                time.sleep(0.4)
                try:
                    if self.config.get("device") and self.device_dropdown.value is not None:
                        self._ui_call(self.toggle_service)
                except Exception as exc:
                    print(f"[AutoStart] failed: {exc}")
            threading.Thread(target=_deferred_auto_start, daemon=True).start()

        self.battery_thread = threading.Thread(target=self.battery_poll_loop, daemon=True)
        self.battery_thread.start()

    # ---------- Config ----------
    @staticmethod
    def _device_key(vid, pid, usage_page):
        return f"{int(vid):04x}:{int(pid):04x}:{int(usage_page):04x}"

    @staticmethod
    def _device_key_of(hid_dev):
        return f"{hid_dev['vendor_id']:04x}:{hid_dev['product_id']:04x}:{hid_dev['usage_page']:04x}"

    @staticmethod
    def _device_label_for(hid_dev):
        return f"{(hid_dev.get('manufacturer_string') or 'Unknown').strip()} {(hid_dev.get('product_string') or 'Device').strip()}"

    @staticmethod
    def _detect_transport(hid_dev) -> str:
        text = " ".join([
            hid_dev.get("product_string") or "",
            hid_dev.get("manufacturer_string") or "",
        ]).lower()
        wireless_markers = ("2.4g", "2.4 g", "wireless", "dongle", "rf receiver")
        return "wireless" if any(marker in text for marker in wireless_markers) else "wired"

    def _device_profile_count(self) -> int:
        entry = self._active_device()
        kb_type = entry.get("keyboard_type") if entry else None
        info = KEYBOARD_TYPES.get(kb_type)
        return info["profiles"] if info else KEYBOARD_TYPES["magnetic"]["profiles"]

    def _device_opcode(self) -> int:
        entry = self._active_device()
        kb_type = entry.get("keyboard_type") if entry else None
        info = KEYBOARD_TYPES.get(kb_type)
        return info["opcode"] if info else KEYBOARD_TYPES["magnetic"]["opcode"]

    def _profile_payload_at(self, index: int) -> list:
        info = self._profile_info_at(index)
        if isinstance(info, dict):
            data = _valid_profile_payload_bytes(info.get("data"))
            if data:
                return data
        return _default_profile_payload(index, self._device_opcode())

    def _detect_transport_for_active(self) -> str:
        dev = self.config.get("device") or {}
        if not dev:
            return "wired"
        vid, pid = dev.get("vid", 0), dev.get("pid", 0)
        for d in hid.enumerate(vid, pid):
            return self._detect_transport(d)
        return "wired"

    def _probe_battery_percent(self, hid_dev):
        """Synchronously query battery on a specific HID device and return percent (0..100) or None.

        Used by refresh_devices() to classify wired vs wireless: a working battery
        response means wireless; no response / no sane percent means wired.
        Tries every HID interface for this VID:PID:usage_page (some are deaf).
        """
        key = self._device_key_of(hid_dev)
        entry = self.config["devices"].get(key) or {}
        if entry.get("keyboard_type") is None:
            return None
        batt = entry.get("battery") or {}
        query = batt.get("query") or []
        if not query:
            return None
        report_id = batt.get("report_id", 0)
        response_length = batt.get("response_length", 65)
        response_offset = batt.get("response_offset", 2)
        response_scale = batt.get("response_scale", 1)

        try:
            import hid as _hid
        except Exception:
            return None

        vid, pid, up = hid_dev["vendor_id"], hid_dev["product_id"], hid_dev["usage_page"]
        paths = [d["path"] for d in _hid.enumerate(vid, pid) if d.get("usage_page") == up]

        with self.usb_lock:
            for path in paths:
                device = None
                try:
                    device = _hid.device()
                    device.open_path(path)
                    device.set_nonblocking(1)
                    device.send_feature_report([report_id] + list(query))
                    response = device.get_feature_report(report_id, response_length)
                except Exception:
                    try:
                        if device is not None:
                            device.close()
                    except Exception:
                        pass
                    continue
                try:
                    device.close()
                except Exception:
                    pass
                try:
                    raw = response[response_offset]
                    percent = max(0, min(100, int(raw * response_scale)))
                except (IndexError, TypeError, ValueError):
                    continue
                # 0% can legitimately mean a flat battery, but in wired mode the
                # device commonly echoes zeros — accept only strictly > 0 as a
                # reliable wireless signal.
                if percent > 0:
                    return percent
        return None

    @staticmethod
    def _pick_active_target(current_active, present_keys, devices_cfg):
        """Decide which device key should be active after a refresh.
        1. Keep current_active if it's still present AND has a config entry.
        2. Else prefer a present device whose config transport == 'wired'.
        3. Else first present device. Else None."""
        if current_active and current_active in present_keys and current_active in devices_cfg:
            return current_active
        for k in present_keys:
            if devices_cfg.get(k, {}).get("transport") == "wired":
                return k
        return present_keys[0] if present_keys else None

    def _empty_device_entry(self, vid, pid, usage_page, label=""):
        return {
            "vid": int(vid),
            "pid": int(pid),
            "usage_page": int(usage_page),
            "label": label or "",
            "transport": None,
            "keyboard_type": None,
            "cooldown_ms": 0,
            "payloads": {},
            "bindings": [],
            "default_profile_index": None,
            "lighting_lab": LightingSettings().to_config(),
            "magnetic_key_settings": {},
            "magnetic_key_modes": {},
            "magnetic_keyboard_options": {},
            "magnetic_rt_separate": {},
            "magnetic_snap_pairs": [],
            "magnetic_profiles": {},
            "magnetic_selected_profile": 0,
            "battery": {
                "query": [],
                "report_id": 0,
                "response_length": 65,
                "response_offset": 2,
                "response_scale": 1,
                "charging_offset": None,
                "charging_mask": 0,
            },
        }

    def _normalize_device_entry(self, entry):
        entry.setdefault("label", "")
        entry.setdefault("transport", None)
        entry.setdefault("keyboard_type", None)
        entry.setdefault("cooldown_ms", 0)
        entry["profile_switch_delay_ms"] = _resolved_profile_switch_delay_ms(entry)
        # Per-key values and modes are populated from safe startup reads and
        # refreshed after automatic writes.  Keep both maps distinct: a Snap
        # Key must not be mistaken for a normal per-key RT configuration.
        if not isinstance(entry.get("magnetic_key_settings"), dict):
            entry["magnetic_key_settings"] = {}
        if not isinstance(entry.get("magnetic_key_modes"), dict):
            entry["magnetic_key_modes"] = {}
        if not isinstance(entry.get("magnetic_keyboard_options"), dict):
            entry["magnetic_keyboard_options"] = {}
        if not isinstance(entry.get("magnetic_rt_separate"), dict):
            entry["magnetic_rt_separate"] = {}
        if not isinstance(entry.get("magnetic_snap_pairs"), list):
            entry["magnetic_snap_pairs"] = []
        # HID-success deltas waiting for the official Womier Driver to close.
        # This survives a QMK.Top Manager restart; it is intentionally a
        # narrow per-key journal, never a full copy of Womier's cache.
        if not isinstance(entry.get("womier_cache_sync_pending"), dict):
            entry["womier_cache_sync_pending"] = {}
        _normalize_magnetic_profile_slots(entry)
        # Retire obsolete experimental lighting settings from imports, exports
        # and future launches; normal Womier effects use only ``lighting_lab``.
        entry.pop("lighting_custom_colors", None)
        entry["lighting_lab"] = LightingSettings.from_config(entry.get("lighting_lab")).to_config()
        kb_type = entry.get("keyboard_type")
        if kb_type is not None and kb_type not in KEYBOARD_TYPES:
            logger.warning("Unknown keyboard_type '%s', reset to null", kb_type)
            entry["keyboard_type"] = None
            kb_type = None
        if kb_type is None:
            entry.setdefault("payloads", {})
            entry.setdefault("bindings", [])
            entry.setdefault("default_profile_index", None)
            entry.setdefault("battery", {
                "query": [], "report_id": 0, "response_length": 65,
                "response_offset": 2, "response_scale": 1,
                "charging_offset": None, "charging_mask": 0,
            })
            return
        kb_info = KEYBOARD_TYPES[kb_type]
        pc = kb_info["profiles"]
        payloads = entry.get("payloads") or {}
        if not isinstance(payloads, dict):
            payloads = {}
        items = list(payloads.items())[:pc]
        while len(items) < pc:
            items.append((f"Профиль {len(items) + 1}", {"hotkey": ""}))
        new_payloads = {}
        for slot_idx, (name, info) in enumerate(items):
            if not isinstance(info, dict):
                info = {"hotkey": ""}
            raw_payload = info.get("data")
            if raw_payload is not None:
                checked_payload = _valid_profile_payload_bytes(raw_payload)
                if checked_payload is None:
                    # Existing local configs can predate clipboard validation
                    # or be hand-edited.  Do not let one bad item crash the
                    # profile list / HID sender on every launch; falling back
                    # to the generated safe report preserves the profile slot.
                    logger.warning("discarded invalid HID payload in profile %r", name)
                    info.pop("data", None)
                else:
                    info["data"] = checked_payload
            info.setdefault("hotkey", "")
            new_payloads[name] = info
        entry["payloads"] = new_payloads

        name_to_idx = {n: i for i, n in enumerate(new_payloads.keys())}
        new_bindings = []
        for b in entry.get("bindings", []) or []:
            if not isinstance(b, dict) or "process" not in b:
                continue
            if "profile_index" in b and isinstance(b["profile_index"], int) and 0 <= b["profile_index"] < pc:
                normalized = {"process": str(b["process"]).strip().lower(), "profile_index": b["profile_index"]}
                if not normalized["process"]:
                    continue
                if isinstance(b.get("enabled"), bool):
                    normalized["enabled"] = b["enabled"]
                new_bindings.append(normalized)
                continue
            old_name = b.get("profile_name")
            if old_name in name_to_idx:
                normalized = {"process": str(b["process"]).strip().lower(), "profile_index": name_to_idx[old_name]}
                if not normalized["process"]:
                    continue
                if isinstance(b.get("enabled"), bool):
                    normalized["enabled"] = b["enabled"]
                new_bindings.append(normalized)
        entry["bindings"] = new_bindings

        # Migrate legacy "default" pseudo-binding into a dedicated field.
        dpi = entry.get("default_profile_index")
        if not isinstance(dpi, int) or not (0 <= dpi < pc):
            dpi = None
            for b in list(entry["bindings"]):
                if b.get("process") == "default" and isinstance(b.get("profile_index"), int):
                    dpi = b["profile_index"]
                    entry["bindings"].remove(b)
                    break
        entry["default_profile_index"] = dpi

        battery = entry.get("battery") or {}
        defaults = {
            "query": [], "report_id": 0, "response_length": 65, "response_offset": 2,
            "response_scale": 1, "charging_offset": None, "charging_mask": 0,
        }
        for k, v in defaults.items():
            battery.setdefault(k, v)
        # Heal legacy entries that were created with broken WebHID-style
        # defaults (length=32, offset=0). Those values produce 0% on hidapi
        # because byte[0] is the report_id, not the percent. Bump them only
        # if they still match the old broken pair AND no real query was
        # captured yet — leave user-tuned values alone.
        if (
            battery.get("response_length") == 32
            and battery.get("response_offset") == 0
        ):
            battery["response_length"] = 65
            battery["response_offset"] = 2
        entry["battery"] = battery

    @staticmethod
    def _legacy_migration_metadata(report: dict) -> dict:
        """Metadata proves the source was read, never modified."""
        return {
            "source": str(LEGACY_CONFIG_FILE) if LEGACY_CONFIG_FILE else None,
            "fingerprint": _legacy_source_fingerprint(LEGACY_CONFIG_FILE)
            if LEGACY_CONFIG_FILE
            else None,
            "imported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            **report,
        }

    def _migrate_legacy_config_data(self, data: dict, *, automatic: bool) -> tuple[dict, dict | None]:
        """Merge the known previous-install config into ``data``.

        Automatic migration runs once per installation.  The explicit UI action
        may be used later to add rules that were created in the old app after
        the first migration.
        """
        if LEGACY_CONFIG_FILE is None or not LEGACY_CONFIG_FILE.is_file():
            return data, None
        settings = data.setdefault("settings", {})
        if not isinstance(settings, dict):
            settings = {}
            data["settings"] = settings
        marker = settings.get("legacy_config_migration")
        if automatic and isinstance(marker, dict) and marker.get("source") == str(LEGACY_CONFIG_FILE):
            return data, None
        legacy = _load_json_object(LEGACY_CONFIG_FILE)
        merged, report = _merge_legacy_config(data, legacy)
        merged_settings = merged.setdefault("settings", {})
        if not isinstance(merged_settings, dict):
            merged_settings = {}
            merged["settings"] = merged_settings
        merged_settings["legacy_config_migration"] = self._legacy_migration_metadata(report)
        return merged, report

    def _import_womier_magnetic_profiles_data(
        self, data: dict, *, automatic: bool = True
    ) -> tuple[dict, dict | None]:
        """Import the official driver's cached SK75 profiles without HID I/O.

        Womier Driver stores its four magnetic profile matrices in Chromium
        Local Storage, not in its JSON profile file.  Reading that cache is
        the only way to recover all four existing Womier configurations at
        once without switching a keyboard profile or writing a single packet.
        The one-time marker prevents a later app launch from overwriting edits
        made in this driver.
        """
        settings = data.setdefault("settings", {})
        if not isinstance(settings, dict):
            settings = {}
            data["settings"] = settings
        # Clipboard imports explicitly declare their magnetic profiles
        # authoritative.  Check this before touching the official driver's
        # storage so a later normal startup cannot overwrite a pasted set of
        # profiles (or even needlessly open/read Womier's LevelDB cache).
        if automatic and settings.get(CONFIG_TRANSFER_WOMIER_GUARD_KEY) is True:
            return data, None
        fingerprint = womier_storage_fingerprint()
        marker = settings.get("womier_magnetic_import")
        if (
            automatic
            and isinstance(marker, dict)
            and marker.get("source") == str(WOMIER_DRIVER_LEVELDB)
        ):
            return data, None
        candidate = find_womier_magnetic_import()
        if candidate is None:
            return data, None
        active_key = data.get("active_device")
        active_entry = data.get("devices", {}).get(active_key)
        # Never guess a target by a generic Womier model id.  The currently
        # active, explicitly configured SK75 is the only safe destination.
        if not isinstance(active_entry, dict) or active_entry.get("keyboard_type") != "magnetic":
            return data, None

        _normalize_magnetic_profile_slots(active_entry)
        previous = _json_copy(active_entry.get("magnetic_profiles") or {})
        merged_profiles = active_entry["magnetic_profiles"]
        for profile_index, imported_profile in candidate.profiles.items():
            merged_profiles[profile_index] = _json_copy(imported_profile)
        _normalize_magnetic_profile_slots(active_entry)

        selected = active_entry.get("magnetic_selected_profile", 0)
        try:
            selected = max(0, min(MAGNETIC_PROFILE_COUNT - 1, int(selected)))
        except (TypeError, ValueError):
            selected = 0
        visible = active_entry["magnetic_profiles"].get(str(selected), {})
        # The legacy live maps feed the already-built key deck.  Point them at
        # the selected imported profile immediately; this remains an in-memory
        # cache and does not touch the keyboard.
        active_entry["magnetic_key_settings"] = _json_copy(visible.get("key_settings") or {})
        active_entry["magnetic_key_modes"] = _json_copy(visible.get("key_modes") or {})
        active_entry["magnetic_rt_separate"] = _json_copy(visible.get("rt_separate") or {})
        # Keep a small recoverable snapshot only for this one migration.  It
        # lets a user restore the old application presets from the exported
        # JSON if they later decide the official cache was not the one wanted.
        if "magnetic_profiles_before_womier_import" not in active_entry:
            active_entry["magnetic_profiles_before_womier_import"] = previous
        report = {
            "source": str(WOMIER_DRIVER_LEVELDB),
            "fingerprint": fingerprint,
            "storage_key": candidate.storage_key,
            "profiles_imported": candidate.imported_profile_count,
            "imported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        settings["womier_magnetic_import"] = report
        logger.info(
            "imported %s Womier magnetic profiles from %s",
            candidate.imported_profile_count,
            candidate.storage_key,
        )
        return data, report

    @staticmethod
    def _legacy_migration_summary(report: dict) -> str:
        return (
            f"Добавлено правил: {report.get('bindings_added', 0)} · "
            f"профилей: {report.get('profiles_updated', 0)} · "
            f"устройств: {report.get('devices_added', 0)}"
        )

    def _import_legacy_configuration(self):
        """Explicit, non-destructive import from the previous installation."""
        try:
            candidate, report = self._migrate_legacy_config_data(self.config, automatic=False)
            if report is None:
                self._snack("Старый файл конфигурации не найден")
                return
            # Active-device aliases are runtime references, not portable data.
            for alias in ("payloads", "bindings", "battery", "device"):
                candidate.pop(alias, None)
            candidate["mode"] = "auto"
            for key, entry in list(candidate.get("devices", {}).items()):
                if not isinstance(entry, dict):
                    del candidate["devices"][key]
                    continue
                self._normalize_device_entry(entry)
            if candidate.get("active_device") not in candidate.get("devices", {}):
                candidate["active_device"] = next(iter(candidate.get("devices", {})), None)
            self.config = candidate
            self._ensure_active_device_aliases()
            self.save_config()
            self.refresh_devices()
            self.update_payloads_list()
            self.update_bindings_list()
            if hasattr(self, "profile_switch_delay_dropdown"):
                self.profile_switch_delay_dropdown.value = str(_resolved_profile_switch_delay_ms(self._active_device()))
                self.profile_switch_delay_dropdown.update()
            summary = self._legacy_migration_summary(report)
            if hasattr(self, "legacy_import_status"):
                self.legacy_import_status.value = f"Перенесено. {summary}"
                self.legacy_import_status.update()
            self._snack(f"Конфигурация перенесена. {summary}")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.exception("legacy configuration import failed")
            self._snack(f"Не удалось перенести старую конфигурацию: {exc}")

    def load_config(self, *, include_external_migrations: bool = True):
        """Load and normalize the local configuration.

        ``include_external_migrations`` is false only for an explicit
        copy/paste import.  A pasted document is authoritative: re-reading
        the official Womier cache at that moment could silently replace the
        magnetic profiles that were just imported.  Old QMK.Top Manager
        configuration is never imported automatically in a public release.
        """
        logger.debug("loading config from %s", CONFIG_FILE)
        default_config = {
            "mode": "auto",
            "settings": {
                "start_minimized": False,
                # Public installs must opt in to automatic process-profile
                # switching.  ``setdefault`` below deliberately preserves an
                # existing user's explicit True/False choice.
                "autostart_service": False,
                "autostart": False,
                "startup_delay_sec": 5,
                "browser_path": "",
                "debug": False,
            },
            "devices": {},
            "active_device": None,
        }
        # Never turn a transient OneDrive/antivirus lock or malformed external
        # edit into data loss.  The old broad ``except: data = {}`` continued
        # into the normal startup write below, replacing every profile and
        # binding with defaults.  Keep a recoverable exact copy and deliberately
        # skip automatic migrations/writes for this load instead.
        config_read_error = None
        recovery_backup = None
        self._config_recovery_backup = None
        self._config_recovery_write_blocked = False
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("корень конфигурации должен быть JSON-объектом")
            except Exception as exc:
                config_read_error = exc
                recovery_backup = _preserve_unreadable_config_file(CONFIG_FILE)
                self._config_recovery_backup = recovery_backup
                # If even the recovery copy could not be made (for example
                # due to a sharing violation), do not let a later ordinary
                # save overwrite the still-unreadable source file.
                self._config_recovery_write_blocked = recovery_backup is None
                if recovery_backup is not None:
                    logger.error(
                        "configuration could not be read; original was preserved at %s",
                        recovery_backup,
                        exc_info=True,
                    )
                else:
                    logger.error(
                        "configuration could not be read and recovery copy failed; "
                        "writes are temporarily blocked",
                        exc_info=True,
                    )
                data = {}
        else:
            data = {}

        # settings + mode
        if "settings" not in data or not isinstance(data.get("settings"), dict):
            data["settings"] = dict(default_config["settings"])
        else:
            for k, v in default_config["settings"].items():
                data["settings"].setdefault(k, v)
        # The app operates exclusively through automatic rules. Keep the
        # legacy key stable for existing configs, always as auto.
        data["mode"] = "auto"

        # legacy → multi-device migration
        if "devices" not in data or not isinstance(data.get("devices"), dict):
            data["devices"] = {}
        legacy_dev = data.pop("device", None)
        legacy_payloads = data.pop("payloads", None)
        legacy_bindings = data.pop("bindings", None)
        legacy_battery = data.pop("battery", None)
        if legacy_dev and isinstance(legacy_dev, dict):
            try:
                key = self._device_key(legacy_dev["vid"], legacy_dev["pid"], legacy_dev["usage_page"])
                entry = data["devices"].get(key) or {
                    "vid": legacy_dev["vid"],
                    "pid": legacy_dev["pid"],
                    "usage_page": legacy_dev["usage_page"],
                    "label": "",
                }
                if legacy_payloads is not None:
                    entry["payloads"] = legacy_payloads
                if legacy_bindings is not None:
                    entry["bindings"] = legacy_bindings
                if legacy_battery is not None:
                    entry["battery"] = legacy_battery
                data["devices"][key] = entry
                data.setdefault("active_device", key)
            except Exception:
                pass

        for key, entry in list(data["devices"].items()):
            if not isinstance(entry, dict):
                del data["devices"][key]
                continue
            self._normalize_device_entry(entry)

        active = data.get("active_device")
        if active not in data["devices"]:
            data["active_device"] = next(iter(data["devices"].keys()), None)

        # Pull the existing official Womier Driver magnetic cache once, after
        # device normalization has established a known magnetic SK75 target.
        # This is strictly a local-file read; the importer never opens HID.
        if include_external_migrations and config_read_error is None:
            try:
                data, womier_report = self._import_womier_magnetic_profiles_data(
                    data, automatic=True
                )
                if womier_report is not None:
                    logger.info(
                        "Womier magnetic cache imported: %s profile(s)",
                        womier_report["profiles_imported"],
                    )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                # A locked/corrupt official-driver cache must not block launch or
                # change the currently saved app-side magnetic presets.
                logger.warning("Womier magnetic cache was skipped: %s", exc)

        # A successful normal load may write migrations/defaults atomically.
        # After a failed read, leave the original file untouched for recovery;
        # an explicit later save is allowed only when its backup succeeded.
        if config_read_error is None:
            try:
                _write_json_atomically(CONFIG_FILE, data)
            except Exception:
                logger.warning("could not persist normalized configuration", exc_info=True)
        return data

    # ---------- Active device aliases ----------
    def _active_device(self):
        key = self.config.get("active_device")
        if not key:
            return None
        return self.config.get("devices", {}).get(key)

    def _ensure_active_device_aliases(self):
        """Bind self.config['payloads'/'bindings'/'battery'/'device'] as references
        to the active device's sub-dicts, so existing call sites Just Work."""
        entry = self._active_device()
        if entry is None:
            self.config["payloads"] = {}
            self.config["bindings"] = []
            self.config["battery"] = {}
            self.config["device"] = None
        else:
            self.config["payloads"] = entry["payloads"]
            self.config["bindings"] = entry["bindings"]
            self.config["battery"] = entry["battery"]
            self.config["device"] = {
                "vid": entry["vid"], "pid": entry["pid"], "usage_page": entry["usage_page"],
            }

    def _activate_device(self, key):
        if key not in self.config.get("devices", {}):
            return False
        entry = self.config["devices"][key]
        if entry.get("keyboard_type") is None:
            self.config["active_device"] = key
            self._ensure_active_device_aliases()
            self.save_config()
            self._show_setup_wizard(key)
            return True
        was_running = self.is_running
        if was_running:
            self.is_running = False
            self._stop_auto_profile_switching()
            try:
                keyboard.unhook_all()
            except Exception:
                pass
        self.config["active_device"] = key
        self._ensure_active_device_aliases()
        self.current_binding = None
        self.last_active_window = None
        if hasattr(self, "profile_switch_delay_dropdown"):
            self.profile_switch_delay_dropdown.value = str(_resolved_profile_switch_delay_ms(entry))
            self.profile_switch_delay_dropdown.disabled = False
            try:
                self.profile_switch_delay_dropdown.update()
            except Exception:
                pass
        # Recreate battery monitor pointed at new device's battery dict
        try:
            self.battery_monitor = BatteryMonitor(
                config_battery=self.config["battery"],
                usb_lock=self.usb_lock,
                get_device_path=self.get_keyboard_path_safe,
                get_device_paths=self.get_keyboard_paths,
                on_working_path=self._cache_working_path,
                default_query=DEFAULT_BATTERY_QUERY,
            )
        except Exception:
            pass
        self.save_config()
        try:
            self.update_payloads_list()
        except Exception:
            pass
        try:
            self.update_bindings_list()
        except Exception:
            pass
        try:
            self._sync_magnetic_profile_controls_for_active_device()
        except Exception:
            logger.debug("could not refresh magnetic presets after device change", exc_info=True)
        if was_running:
            self.is_running = True
            self.reload_runtime_state()
            self._set_status(True)
            if not self.worker_thread or not self.worker_thread.is_alive():
                self.worker_thread = threading.Thread(target=self.background_task, daemon=True)
                self.worker_thread.start()
        threading.Thread(target=self._refresh_battery_now, daemon=True).start()
        try:
            self._battery_test_sync_from_active()
        except Exception:
            pass
        try:
            self._update_transport_icon()
        except Exception:
            pass
        return True

    def _ensure_device_entry(self, hid_dev):
        """Create an empty config entry for an HID device if missing. Returns key.
        Also lazily fills the `transport` field from device metadata.
        Saves config only when something actually changed."""
        key = self._device_key_of(hid_dev)
        dirty = False
        if key not in self.config["devices"]:
            self.config["devices"][key] = self._empty_device_entry(
                hid_dev["vendor_id"], hid_dev["product_id"], hid_dev["usage_page"],
                label=self._device_label_for(hid_dev),
            )
            dirty = True
        entry = self.config["devices"][key]
        if entry.get("transport") is None:
            entry["transport"] = self._detect_transport(hid_dev)
            dirty = True
        if dirty:
            self.save_config()
        return key

    # ---------- Profile helpers ----------
    def _profile_items(self):
        return list(self.config.get("payloads", {}).items())

    def _profile_name_at(self, index):
        items = self._profile_items()
        if 0 <= index < len(items):
            return items[index][0]
        return None

    def _profile_info_at(self, index):
        items = self._profile_items()
        if 0 <= index < len(items):
            return items[index][1]
        return None

    def _profile_info_at_by_name(self, name):
        return self.config.get("payloads", {}).get(name)

    def _profile_index_by_name(self, name):
        """Return the current device-profile index for a payload name."""
        for index, (profile_name, _info) in enumerate(self._profile_items()):
            if profile_name == name:
                return index
        return None

    def _rename_profile_at(self, index, new_name):
        items = self._profile_items()
        if not (0 <= index < len(items)):
            return False
        new_name = (new_name or "").strip()
        if not new_name:
            return False
        existing = {n for i, (n, _) in enumerate(items) if i != index}
        if new_name in existing:
            return False
        items[index] = (new_name, items[index][1])
        new_payloads = {n: info for n, info in items}
        self.config["payloads"] = new_payloads
        entry = self._active_device()
        if entry is not None:
            entry["payloads"] = new_payloads
        return True

    def _current_mode(self):
        return "auto"

    def save_config(self, *, reload_runtime=True):
        logger.debug("saving config to %s", CONFIG_FILE)
        if getattr(self, "_config_recovery_write_blocked", False):
            logger.error(
                "configuration write skipped because the unreadable original could not be backed up"
            )
            return False
        # A switch event is synchronous, while its debounced HID/cache write
        # runs on a timer.  Both paths modify the same nested magnetic-profile
        # dictionaries.  Snapshotting only the top-level mapping outside this
        # lock allowed json.dump() to walk a nested dict while the timer added
        # a key, producing ``dictionary changed size during iteration``.
        #
        # Keep the lock across the short detached JSON copy and atomic write.
        # This does not add a new UI wait: saving already performed the same
        # synchronous disk write on the event path.  It merely makes the
        # in-memory snapshot and its on-disk version one transaction.
        with _CONFIG_WRITE_LOCK:
            try:
                self.config["mode"] = "auto"
            except Exception:
                pass
            # Don't persist legacy alias keys; they are references into
            # devices[active].  A JSON round-trip also detaches every nested
            # dict before the file writer starts iterating it.
            snapshot = _json_copy(
                {
                    k: v
                    for k, v in self.config.items()
                    if k not in CONFIG_RUNTIME_ALIAS_KEYS
                }
            )
            _write_json_atomically(CONFIG_FILE, snapshot)
        # A foreground rule can update only its selected *local* magnetic
        # preset.  Persist that choice without tearing down/reloading the
        # foreground coordinator that is currently handling the same Alt+Tab.
        # Reloading here used to unhook all keyboard listeners and invalidate
        # the just-created latest-wins request, which made rapid switching look
        # like a stuck keyboard despite the HID packet itself being tiny.
        if reload_runtime and self.is_running:
            self.reload_runtime_state()
        if reload_runtime and self.is_running:
            self._set_status(True)
        return True

    def _cancel_magnetic_persistence_for_config_replacement(self):
        """Invalidate a queued JSON save before replacing all config data."""
        lock = self._ensure_magnetic_persistence_state()
        with lock:
            self._magnetic_persistence_revision = (
                int(getattr(self, "_magnetic_persistence_revision", 0)) + 1
            )
            timer = getattr(self, "_magnetic_persistence_timer", None)
            self._magnetic_persistence_timer = None
            self._magnetic_persistence_pending = False
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    logger.debug("could not cancel magnetic persistence timer", exc_info=True)

    def _discard_womier_cache_sync_for_config_replacement(self):
        """Drop only in-memory deltas that belong to the replaced config.

        The imported document carries its own persisted Womier journal.  A
        pre-import queue must never be allowed to mirror an older local preset
        into the official driver after the new document is already active.
        """
        lock = self._ensure_womier_cache_sync_state()
        with lock:
            self._womier_cache_sync_revision = (
                int(getattr(self, "_womier_cache_sync_revision", 0)) + 1
            )
            timer = getattr(self, "_womier_cache_sync_timer", None)
            self._womier_cache_sync_timer = None
            self._womier_cache_sync_pending = {}
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    logger.debug("could not cancel Womier cache sync timer", exc_info=True)

    def _prepare_configuration_replacement(self):
        """Fence stale timers/workers before an explicit full-config import.

        Clipboard import replaces profile maps atomically.  Existing slider,
        options, auto-profile and cache-mirror work belongs to the old maps,
        so it must be invalidated *before* the new file is committed.  Waiting
        once for the USB critical section also prevents a queued worker from
        observing a half-replaced active-device mapping.
        """
        try:
            self._stop_auto_profile_switching(cancel_magnetic=False)
        except (AttributeError, RuntimeError):
            pass
        try:
            self._stop_magnetic_profile_switching()
        except (AttributeError, RuntimeError):
            pass
        try:
            self._cancel_pending_magnetic_writes()
        except (AttributeError, RuntimeError):
            pass
        self._cancel_magnetic_persistence_for_config_replacement()
        self._discard_womier_cache_sync_for_config_replacement()

        usb_lock = getattr(self, "usb_lock", None)
        if usb_lock is not None:
            # This is a barrier, not a new HID operation.  A worker already
            # in a feature-report call finishes first; cancelled revisions
            # prevent it from caching/requeuing an obsolete intent afterward.
            with usb_lock:
                pass

    def _exportable_config(self):
        """Return a historical full backup for internal compatibility only.

        The visible Copy button intentionally calls ``_exportable_profile_rules``
        instead.  Keeping this helper lets an older internal backup still be
        tested/read without accidentally putting keyboard state into a public
        clipboard transfer.
        """
        # A copy request can coincide with a debounced magnetic HID write.
        # Take the same ownership lock as ``save_config`` so the deep JSON copy
        # never walks a nested profile map while that timer updates it.
        with _CONFIG_WRITE_LOCK:
            snapshot = {
                key: value for key, value in self.config.items()
                if key not in CONFIG_RUNTIME_ALIAS_KEYS
            }
            # JSON round-trip both detaches nested dictionaries and validates
            # that a pasted export is portable without Python-specific objects.
            portable = _json_copy(snapshot)
        portable["mode"] = "auto"
        settings = portable.get("settings")
        if not isinstance(settings, dict):
            settings = {}
            portable["settings"] = settings
        # Make the exported document self-contained.  If it is pasted on a
        # machine with a different official-driver cache, that cache must not
        # replace the values the user deliberately copied.
        settings[CONFIG_TRANSFER_WOMIER_GUARD_KEY] = True
        devices = portable.get("devices")
        for entry in devices.values() if isinstance(devices, dict) else ():
            if not isinstance(entry, dict):
                continue
            for profile in entry.get("payloads", {}).values():
                if isinstance(profile, dict):
                    profile.pop("hotkey", None)
        return {
            "format": LEGACY_CONFIG_TRANSFER_FORMAT,
            "version": LEGACY_CONFIG_TRANSFER_VERSION,
            "config": portable,
        }

    def _exportable_config_transfer(self, sections: object) -> dict:
        """Export precisely the selected safe sections, never profile payloads."""
        selected = _normalise_transfer_sections(sections)
        with _CONFIG_WRITE_LOCK:
            snapshot = {
                key: value
                for key, value in self.config.items()
                if key not in CONFIG_RUNTIME_ALIAS_KEYS
            }
            detached = _json_copy(snapshot)
        return {
            "format": CONFIG_TRANSFER_FORMAT,
            "version": CONFIG_TRANSFER_VERSION,
            "categories": list(selected),
            "devices": _portable_config_transfer_devices_from_config(detached, selected),
        }

    def _exportable_profile_rules(self):
        """Compatibility export for the former names/rules-only button."""
        return self._exportable_config_transfer(
            (TRANSFER_SECTION_PROFILE_NAMES, TRANSFER_SECTION_PROCESS_BINDINGS)
        )

    @staticmethod
    def _profile_rules_target_key(rule: dict, devices: dict) -> str | None:
        """Find one receiving keyboard without trusting a machine-local path."""
        requested_key = rule.get("device_key")
        identity = rule.get("identity") if isinstance(rule.get("identity"), dict) else {}

        def matches(entry: object) -> bool:
            if not isinstance(entry, dict):
                return False
            return all(entry.get(name) == value for name, value in identity.items())

        exact = devices.get(requested_key)
        if exact is not None and matches(exact):
            return requested_key
        candidates = [key for key, entry in devices.items() if matches(entry)]
        return candidates[0] if len(candidates) == 1 else None

    def _apply_config_transfer(
        self, transfer: dict, *, sections: object = None
    ) -> tuple[int, int]:
        """Merge selected portable settings without sending HID commands.

        The parsed clipboard advertises its available sections.  The local UI
        selection is intersected with that set, so a document cannot smuggle a
        category a user did not tick.  All writes happen on one detached JSON
        snapshot; profile payload bytes/cache journals are retained locally.
        """
        # Treat this as a trust boundary even when the Flet dialog has
        # already parsed the clipboard.  Internal/compatibility callers must
        # not be able to skip profile-byte or Magnetic Lab validation and
        # persist an arbitrary nested object.
        transfer = _normalise_config_transfer_for_apply(transfer)
        incoming_rules = transfer.get("devices") if isinstance(transfer, dict) else None
        available = transfer.get("categories") if isinstance(transfer, dict) else None
        if not isinstance(incoming_rules, list) or not isinstance(available, list):
            raise ValueError("некорректная конфигурация для импорта")
        available_sections = _normalise_transfer_sections(available)
        requested_sections = _normalise_transfer_sections(
            sections, default=available_sections
        )
        missing_sections = [
            section for section in requested_sections if section not in available_sections
        ]
        if missing_sections:
            raise ValueError(
                "в буфере нет выбранных разделов: " + ", ".join(missing_sections)
            )
        selected = requested_sections

        affects_rules = (
            TRANSFER_SECTION_PROFILE_NAMES in selected
            or TRANSFER_SECTION_PROCESS_BINDINGS in selected
        )
        affects_magnetic = TRANSFER_SECTION_MAGNETIC_LAB in selected
        # A rename/default change must fence an older automatic request.  This
        # does not reload or unhook the global input hook.
        if affects_magnetic:
            # A timer may already have claimed the USB lock immediately
            # before the user clicked Import.  Cancellation alone prevents
            # its cache write, but does not wait for an in-flight HID packet;
            # without this fence an old slider value could land *after* the
            # imported configuration became visible.  The shared helper
            # invalidates all local magnetic/cache revisions and waits for
            # the current USB transaction to finish before the replacement is
            # committed.  It never sends a packet or reloads global hooks.
            self._prepare_configuration_replacement()
        elif affects_rules:
            try:
                self._stop_auto_profile_switching(cancel_magnetic=False)
            except (AttributeError, RuntimeError):
                pass

        with _CONFIG_WRITE_LOCK:
            base = {
                key: value
                for key, value in self.config.items()
                if key not in CONFIG_RUNTIME_ALIAS_KEYS
            }
            candidate = _json_copy(base)
            devices = candidate.get("devices")
            if not isinstance(devices, dict):
                raise ValueError("в этом приложении пока нет подключённой клавиатуры")

            applied = 0
            skipped = 0
            for rule in incoming_rules:
                target_key = self._profile_rules_target_key(rule, devices)
                if target_key is None:
                    skipped += 1
                    continue
                entry = devices.get(target_key)
                if not isinstance(entry, dict):
                    skipped += 1
                    continue
                payloads = entry.get("payloads")
                if affects_rules and (not isinstance(payloads, dict) or not payloads):
                    raise ValueError(
                        "в получающей клавиатуре нет профилей; выберите её тип перед импортом"
                    )
                current_items = list(payloads.items()) if isinstance(payloads, dict) else []
                if TRANSFER_SECTION_PROFILE_NAMES in selected:
                    names = rule.get("profile_names")
                    if not isinstance(names, list) or len(names) > len(current_items):
                        raise ValueError(
                            "в получающей клавиатуре меньше профилей; выберите её тип перед импортом"
                        )
                    merged_items = []
                    for index, (current_name, profile) in enumerate(current_items):
                        target_name = names[index] if index < len(names) else current_name
                        merged_items.append((target_name, profile))
                    if len({name.casefold() for name, _profile in merged_items}) != len(merged_items):
                        raise ValueError("импорт создаёт повторяющиеся имена профилей")
                    entry["payloads"] = {name: profile for name, profile in merged_items}
                    entry["default_profile_index"] = rule.get("default_profile_index")
                if TRANSFER_SECTION_PROCESS_BINDINGS in selected:
                    bindings = rule.get("bindings")
                    if not isinstance(bindings, list) or any(
                        item.get("profile_index", -1) >= len(current_items)
                        for item in bindings if isinstance(item, dict)
                    ):
                        raise ValueError("привязка ссылается на отсутствующий профиль")
                    entry["bindings"] = _json_copy(bindings)
                if TRANSFER_SECTION_LIGHTING_LAB in selected:
                    lighting = rule.get("lighting_lab")
                    if not isinstance(lighting, dict):
                        raise ValueError("в буфере нет корректных настроек Lighting Lab")
                    entry["lighting_lab"] = _json_copy(lighting)
                if TRANSFER_SECTION_MAGNETIC_LAB in selected:
                    magnetic = rule.get("magnetic_lab")
                    if not isinstance(magnetic, dict):
                        raise ValueError("в буфере нет корректных настроек Magnetic Lab")
                    profiles = magnetic.get("profiles")
                    selected_profile = magnetic.get("selected_profile")
                    if not isinstance(profiles, dict) or not isinstance(selected_profile, int):
                        raise ValueError("в буфере нет корректных настроек Magnetic Lab")
                    entry["magnetic_profiles"] = _json_copy(profiles)
                    entry["magnetic_selected_profile"] = selected_profile
                    visible = profiles[str(selected_profile)]
                    # These legacy maps are only the live UI cache for the
                    # selected preset.  Rebuild them from imported safe state;
                    # do not copy a stale duplicate or cache-sync journal.
                    entry["magnetic_key_settings"] = _json_copy(visible["key_settings"])
                    entry["magnetic_key_modes"] = _json_copy(visible["key_modes"])
                    entry["magnetic_keyboard_options"] = _json_copy(visible["keyboard_options"])
                    entry["magnetic_rt_separate"] = _json_copy(visible["rt_separate"])
                    entry["magnetic_snap_pairs"] = _json_copy(visible["snap_pairs"])
                    _normalize_magnetic_profile_slots(entry)
                applied += 1

            if not applied:
                raise ValueError(
                    "не нашёл подходящую клавиатуру: подключите SK75 и выберите «Магнитная»"
                )
            if affects_magnetic:
                # A deliberate Magnetic Lab import must win over a later
                # automatic Womier cache discovery on this PC.  This marker is
                # local metadata, not a keyboard command or copied HID data.
                settings = candidate.setdefault("settings", {})
                if isinstance(settings, dict):
                    settings[CONFIG_TRANSFER_WOMIER_GUARD_KEY] = True
            self.config = candidate
            self._ensure_active_device_aliases()
            # This needs one durable local write but must not reload the global
            # foreground hook / keyboard input runtime.
            self.save_config(reload_runtime=False)
        # Rules are copied without a full runtime reload: that older helper
        # calls keyboard.unhook_all(), which can disrupt physical input.
        if affects_rules:
            self._refresh_process_rule_evaluator()
        return applied, skipped

    def _apply_profile_rules_transfer(self, rules: dict) -> tuple[int, int]:
        """Compatibility import for former rules-only callers/tests."""
        if not isinstance(rules, dict):
            raise ValueError("некорректные правила профилей")
        if "categories" not in rules:
            rules = {
                "categories": [
                    TRANSFER_SECTION_PROFILE_NAMES,
                    TRANSFER_SECTION_PROCESS_BINDINGS,
                ],
                "devices": rules.get("devices"),
            }
        return self._apply_config_transfer(
            rules,
            sections=(TRANSFER_SECTION_PROFILE_NAMES, TRANSFER_SECTION_PROCESS_BINDINGS),
        )

    @staticmethod
    def _set_system_clipboard_text(text: str) -> bool:
        """Put the complete text into the Windows clipboard, with a short retry.

        The Flet desktop clipboard service occasionally truncates a large JSON
        export to its first character.  The native Windows clipboard accepts
        the whole Unicode string atomically, so use it whenever it is present.
        """
        if win32clipboard is None:
            return False

        last_error = None
        for attempt in range(5):
            opened = False
            try:
                win32clipboard.OpenClipboard()
                opened = True
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
                # Verify the complete payload while the clipboard is still
                # open: a successful export must never silently become "{".
                if win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) == text:
                    return True
                last_error = RuntimeError("native clipboard returned an incomplete value")
            except Exception as exc:
                last_error = exc
                time.sleep(0.04 * (attempt + 1))
            finally:
                if opened:
                    try:
                        win32clipboard.CloseClipboard()
                    except Exception:
                        pass

        logger.debug("native clipboard write failed: %s", last_error)
        return False

    @staticmethod
    def _get_system_clipboard_text() -> str | None:
        """Read Unicode text from Windows clipboard, if it is available."""
        if win32clipboard is None:
            return None

        for attempt in range(5):
            opened = False
            try:
                win32clipboard.OpenClipboard()
                opened = True
                if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    return None
                text = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                return text if isinstance(text, str) else None
            except Exception:
                time.sleep(0.04 * (attempt + 1))
            finally:
                if opened:
                    try:
                        win32clipboard.CloseClipboard()
                    except Exception:
                        pass
        return None

    def _copy_configuration(self, *, sections: object = None):
        """Copy the explicitly selected portable sections to the clipboard."""
        try:
            selected = _normalise_transfer_sections(
                sections,
                default=(
                    TRANSFER_SECTION_PROFILE_NAMES,
                    TRANSFER_SECTION_PROCESS_BINDINGS,
                ),
            )
            # Compact separators keep a four-profile transfer small.  HID
            # profile bytes are intentionally absent even when Magnetic Lab is
            # selected: state changes only after an explicit user action.
            text = json.dumps(
                self._exportable_config_transfer(selected),
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except Exception as exc:
            logger.exception("configuration export failed")
            self._snack(f"Не удалось скопировать конфигурацию: {exc}")
            return

        labels = {
            TRANSFER_SECTION_PROFILE_NAMES: "имена профилей",
            TRANSFER_SECTION_PROCESS_BINDINGS: "привязки процессов",
            TRANSFER_SECTION_LIGHTING_LAB: "подсветка",
            TRANSFER_SECTION_MAGNETIC_LAB: "Magnetic Lab",
        }
        copied_message = f"Скопировано: {', '.join(labels[item] for item in selected)} ({len(text):,} символов)"
        if self._set_system_clipboard_text(text):
            self._snack(copied_message)
            return

        async def copy():
            try:
                await self.clipboard.set(text)
                copied = await self.clipboard.get()
                if copied != text:
                    raise RuntimeError("буфер вернул неполный JSON")
                self._snack(copied_message)
            except Exception as exc:
                logger.exception("configuration export failed")
                self._snack(f"Не удалось скопировать конфигурацию: {exc}")

        self.page.run_task(copy)

    def _open_config_import_dialog(self):
        """Open the compact, section-based CFG transfer dialog.

        Clipboard JSON deliberately stays in ``pending_import`` and never in a
        Flet text field.  Selection is local to this dialog: closing it starts
        the next transfer with a clean, deliberate choice.
        """
        pending_import = {"text": None}
        # Start intentionally empty.  A user must consciously opt into every
        # category, especially the keyboard-state categories.
        selected_sections = {
            "profile_names": False,
            "lighting_lab": False,
            "magnetic_lab": False,
            "process_bindings": False,
        }
        section_checks: dict[str, ft.Checkbox] = {}
        section_cards: dict[str, ft.Container] = {}
        import_status = ft.Text(
            "Отметьте хотя бы один раздел, затем скопируйте или вставьте CFG.",
            size=11,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        warning = ft.Text(
            "Профили включают имена и отмеченный профиль по умолчанию. "
            "Данные из буфера не отображаются в окне, поэтому большой JSON не "
            "может замедлить интерфейс.",
            size=10,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )

        def cancel(event):
            self.page.pop_dialog()

        def selected_section_ids() -> tuple[str, ...]:
            return tuple(
                section_id
                for section_id, is_selected in selected_sections.items()
                if is_selected
            )

        def refresh_dialog():
            try:
                # Controls are created once and only their stable parent is
                # patched.  Rebuilding the dialog for each checkmark can race
                # with Flet's child map during a quick series of clicks.
                dialog.update()
            except Exception:
                # The dialog might already be closing; no page-wide fallback
                # is needed for a local selection change.
                pass

        def refresh_actions():
            sections = selected_section_ids()
            selected = bool(sections)
            copy_button.disabled = not selected
            paste_button.disabled = not selected
            # Applying has one extra prerequisite: an already-read clipboard.
            apply_button.disabled = not selected or pending_import["text"] is None

        def set_section_selected(section_id: str, value: bool):
            selected_sections[section_id] = bool(value)
            checkbox = section_checks[section_id]
            card = section_cards[section_id]
            checkbox.value = bool(value)
            card.bgcolor = (
                ft.Colors.SECONDARY_CONTAINER
                if value
                else ft.Colors.SURFACE_CONTAINER
            )
            card.border = ft.Border.all(
                1.5 if value else 1,
                ft.Colors.PRIMARY if value else ft.Colors.OUTLINE_VARIANT,
            )
            refresh_actions()
            refresh_dialog()

        def toggle_section(section_id: str):
            set_section_selected(section_id, not selected_sections[section_id])

        def section_card(
            section_id: str,
            title: str,
            subtitle: str,
            icon: str,
            accent: str,
        ) -> ft.Container:
            checkbox = ft.Checkbox(
                value=False,
                tooltip=f"Выбрать: {title}",
                active_color=ft.Colors.PRIMARY,
                on_change=lambda event, key=section_id: set_section_selected(
                    key, bool(event.control.value)
                ),
            )
            section_checks[section_id] = checkbox
            # Only the text/leading portion handles card taps; the checkbox
            # keeps its own event so a click never toggles twice through event
            # bubbling on some Flet desktop builds.
            selectable_area = ft.Container(
                content=ft.Row(
                    [
                        ft.Container(
                            content=ft.Icon(icon, size=18, color=accent),
                            width=36,
                            height=36,
                            alignment=ft.Alignment.CENTER,
                            bgcolor=ft.Colors.with_opacity(0.14, accent),
                            border_radius=12,
                        ),
                        ft.Column(
                            [
                                ft.Text(title, size=12, weight=ft.FontWeight.W_600),
                                ft.Text(
                                    subtitle,
                                    size=10,
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                    max_lines=2,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                expand=True,
                padding=ft.Padding.only(left=12, top=10, bottom=10),
                ink=True,
                border_radius=15,
                on_click=lambda event, key=section_id: toggle_section(key),
            )
            card = ft.Container(
                content=ft.Row(
                    [selectable_area, checkbox],
                    spacing=4,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                height=76,
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                border_radius=16,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                col={"xs": 12, "sm": 6},
            )
            section_cards[section_id] = card
            return card

        def set_import_text(text: str | None):
            if not text:
                pending_import["text"] = None
                import_status.value = "В буфере нет текстовой конфигурации JSON."
            elif len(text) > MAX_CONFIG_IMPORT_CHARS:
                # Do not leave an older, valid-looking candidate around:
                # otherwise an oversized clipboard paste could lead to an
                # accidental import of the previous configuration.
                pending_import["text"] = None
                import_status.value = (
                    f"Конфигурация слишком большая: максимум "
                    f"{MAX_CONFIG_IMPORT_CHARS:,} символов."
                )
            else:
                pending_import["text"] = text
                # Never mount clipboard JSON in a Flutter TextField. Even
                # 8–20k characters are needless UI work here, while legacy
                # full documents can be hundreds of kilobytes. The complete
                # validated candidate remains in pending_import only.
                import_status.value = (
                    f"В буфере {len(text):,} символов. Готово к импорту."
                )
            refresh_actions()
            refresh_dialog()

        def paste_from_clipboard(event):
            native_text = self._get_system_clipboard_text()
            if native_text is not None:
                set_import_text(native_text)
                return

            async def paste():
                try:
                    set_import_text(await self.clipboard.get())
                except Exception as exc:
                    logger.exception("configuration clipboard read failed")
                    import_status.value = f"Не удалось прочитать буфер: {exc}"
                    refresh_actions()
                    refresh_dialog()

            self.page.run_task(paste)

        def copy_configuration(event):
            sections = selected_section_ids()
            if not sections:
                self._snack("Отметьте хотя бы один раздел CFG")
                return
            self._copy_configuration(sections=sections)

        def apply_import(event):
            sections = selected_section_ids()
            if not sections:
                self._snack("Отметьте хотя бы один раздел CFG")
                return
            text = pending_import.get("text")
            if text is None:
                text = ""
            text = text.strip()
            if not text:
                self._snack("Вставьте JSON-конфигурацию")
                return
            if len(text) > MAX_CONFIG_IMPORT_CHARS:
                self._snack("Конфигурация слишком большая")
                return
            try:
                transfer = _parse_config_transfer_text(text)
                applied, skipped = self._apply_config_transfer(
                    transfer, sections=sections
                )
            except (OSError, ValueError, TypeError, RecursionError, json.JSONDecodeError) as exc:
                self._snack(f"Не удалось импортировать конфигурацию: {exc}")
                return

            # The import itself does not rebuild the Flet page or send HID.
            # Reflect only the sections the user selected, once, after the
            # detached config has been committed.  These stable-panel updates
            # keep the dialog responsive and avoid a competing 81-key repaint
            # while the clipboard is being processed.
            if "profile_names" in sections:
                try:
                    self.update_payloads_list()
                except Exception:
                    logger.debug("could not refresh imported profile labels", exc_info=True)
            if "process_bindings" in sections:
                try:
                    self.update_bindings_list()
                except Exception:
                    logger.debug("could not refresh imported process bindings", exc_info=True)
            if "lighting_lab" in sections:
                try:
                    entry = self._active_device() or {}
                    self._sync_lighting_controls_from_settings(
                        LightingSettings.from_config(entry.get("lighting_lab"))
                    )
                except Exception:
                    logger.debug("could not refresh imported Lighting Lab controls", exc_info=True)
            if "magnetic_lab" in sections:
                try:
                    # This is an explicit, one-time refresh after import.  It
                    # changes only cached controls/keycaps and never queues a
                    # magnetic HID write.
                    if hasattr(self, "magnetic_profile_dropdown"):
                        self._refresh_magnetic_profile_dropdown(update=False)
                    if hasattr(self, "magnetic_actuation_slider"):
                        self._load_magnetic_controls(
                            getattr(self, "magnetic_selected_slot", None),
                            update=True,
                        )
                    if hasattr(self, "keyboard_picker_root"):
                        self._refresh_sk75_keyboard_picker()
                except Exception:
                    logger.debug("could not refresh imported Magnetic Lab controls", exc_info=True)
            self.page.pop_dialog()
            suffix = f"; пропущено устройств: {skipped}" if skipped else ""
            self._snack(f"Импортировано CFG-разделов: {applied}{suffix}.")

        copy_button = ft.FilledTonalButton(
            "Скопировать CFG",
            icon=ft.Icons.CONTENT_COPY_ROUNDED,
            on_click=copy_configuration,
            disabled=True,
        )
        paste_button = ft.OutlinedButton(
            "Вставить CFG",
            icon=ft.Icons.CONTENT_PASTE_ROUNDED,
            on_click=paste_from_clipboard,
            disabled=True,
        )
        apply_button = ft.FilledButton(
            "Применить CFG",
            icon=ft.Icons.UPLOAD_FILE_ROUNDED,
            on_click=apply_import,
            disabled=True,
        )

        transfer_actions = ft.Row(
            [copy_button, paste_button, apply_button],
            spacing=8,
            wrap=True,
        )
        section_grid = ft.ResponsiveRow(
            [
                section_card(
                    "profile_names",
                    "Профили",
                    "Имена и профиль по умолчанию.",
                    ft.Icons.KEYBOARD_ALT_ROUNDED,
                    ft.Colors.PRIMARY,
                ),
                section_card(
                    "lighting_lab",
                    "Подсветка",
                    "Эффект, цвет, яркость и скорость.",
                    ft.Icons.LIGHTBULB_OUTLINE_ROUNDED,
                    ft.Colors.TERTIARY,
                ),
                section_card(
                    "magnetic_lab",
                    "Magnetic Lab",
                    "Параметры магнитных клавиш.",
                    ft.Icons.TUNE_ROUNDED,
                    ft.Colors.SECONDARY,
                ),
                section_card(
                    "process_bindings",
                    "Привязки к процессам",
                    "Правила автоматического выбора профиля.",
                    ft.Icons.LINK_ROUNDED,
                    ft.Colors.ORANGE,
                ),
            ],
            spacing=10,
            run_spacing=10,
        )
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Конфигурация CFG"),
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            "Выберите разделы для копирования или импорта.",
                            size=12,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        section_grid,
                        transfer_actions,
                        warning,
                        import_status,
                    ],
                    spacing=10,
                    tight=True,
                ),
                width=660,
            ),
            shape=ft.RoundedRectangleBorder(radius=20),
        )
        dialog.actions = [
            ft.TextButton("Закрыть", on_click=cancel),
        ]
        self.page.show_dialog(dialog)

    def _set_setting(self, key, value):
        self.config.setdefault("settings", {})[key] = bool(value)
        self.save_config()

    def _on_profile_switch_delay_changed(self, event):
        entry = self._active_device()
        if entry is None:
            self._snack("Сначала выберите клавиатуру")
            return
        entry["profile_switch_delay_ms"] = _resolved_profile_switch_delay_ms({
            "profile_switch_delay_ms": event.control.value,
        })
        # Keep the selected safe value visible even if a pasted config used an
        # arbitrary number between two menu choices.
        event.control.value = str(entry["profile_switch_delay_ms"])
        self.save_config()
        self._snack(f"Задержка перед сменой профиля: {entry['profile_switch_delay_ms']} мс")

    def _on_autostart_windows_changed(self, e):
        from autostart import set_autostart
        enable = e.control.value
        set_autostart(enable)
        self.config.setdefault("settings", {})["autostart"] = enable
        self.save_config()

    def _refresh_process_rule_evaluator(self):
        """Reload only the rule index; never install/remove a global hook."""
        evaluator = getattr(self, "rule_evaluator", None)
        if evaluator is None:
            return
        try:
            evaluator.load(self.config.get("bindings", []))
            self.binds_dict = evaluator._active_index
            entry = self._active_device()
            dpi = entry.get("default_profile_index") if entry else None
            self.default_profile_index = (
                dpi
                if isinstance(dpi, int) and 0 <= dpi < self._device_profile_count()
                else None
            )
            self.last_active_window = None
        except Exception:
            logger.exception("could not refresh imported process rules")

    def reload_runtime_state(self):
        # Invalidate any request made for the previous set of bindings before
        # replacing the evaluator.  A delayed callback must never apply a
        # profile whose rule the user has just removed or reassigned.
        self._stop_auto_profile_switching(cancel_magnetic=False)
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        self._refresh_process_rule_evaluator()
        print(f"[Reload] Автоматических привязок: {len(getattr(self, 'binds_dict', {}))}")

    # ---------- Foreground profile-switch coordinator ----------
    def _ensure_auto_profile_switch_state(self):
        """Create the latest-wins foreground-switch state for old test rigs.

        Most production instances initialise this in ``__init__``.  A number
        of small regression tests intentionally use ``QMKManager.__new__`` to
        isolate HID behaviour, so keeping this lazy fallback makes the
        coordinator safe in both paths without exposing partially initialised
        attributes to a background callback.
        """
        lock = getattr(self, "_auto_profile_switch_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._auto_profile_switch_lock = lock
            self._auto_profile_switch_timer = None
            self._auto_profile_switch_revision = 0
            self._auto_profile_switch_desired = None
            self._auto_profile_switch_worker_active = False
            self._auto_profile_switch_transport_uncertain = False
        return lock

    def _cancel_auto_profile_switch_timer_locked(self):
        timer = getattr(self, "_auto_profile_switch_timer", None)
        self._auto_profile_switch_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                logger.debug("could not cancel foreground profile timer", exc_info=True)

    def _auto_profile_switch_is_current(self, revision, request):
        """Whether an auto-profile worker still owns the latest request."""
        lock = self._ensure_auto_profile_switch_state()
        with lock:
            return (
                bool(getattr(self, "is_running", False))
                and getattr(self, "_auto_profile_switch_revision", -1) == revision
                and getattr(self, "_auto_profile_switch_desired", None) is request
                and self.config.get("active_device") == request.get("device_key")
            )

    @staticmethod
    def _foreground_process_matches(expected_process):
        """Recheck an Alt+Tab target just before writing HID.

        A process can disappear between the scanner poll and the timer.  A
        failed recheck is deliberately treated as *not disproven* rather than
        as a permanent failure: protected windows otherwise leave a stable
        binding unapplied until the user changes focus once more.  The normal
        path remains an exact process-name comparison.
        """
        if not expected_process:
            return True
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return False
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return psutil.Process(pid).name().casefold() == str(expected_process).casefold()
        except Exception:
            logger.debug("could not recheck foreground process", exc_info=True)
            return True

    def _arm_auto_profile_switch_locked(self):
        """Arm the one pending foreground request. Caller owns its lock."""
        desired = getattr(self, "_auto_profile_switch_desired", None)
        if (
            not getattr(self, "is_running", False)
            or not isinstance(desired, dict)
            or not desired.get("profile_name")
            or getattr(self, "_auto_profile_switch_worker_active", False)
        ):
            return

        # If no stale transaction could have changed the physical profile,
        # returning to the already-applied profile needs no HID packet.  When
        # a cancelled worker might have sent its first packet, keep the
        # request alive and re-assert the final profile once; this avoids the
        # classic A -> B -> A race leaving the keyboard on B.
        if (
            desired["profile_name"] == getattr(self, "current_binding", None)
            and not getattr(self, "_auto_profile_switch_transport_uncertain", False)
        ):
            return

        self._cancel_auto_profile_switch_timer_locked()
        remaining = max(0.0, desired.get("ready_at", 0.0) - time.monotonic())
        revision = getattr(self, "_auto_profile_switch_revision", 0)
        timer = threading.Timer(
            remaining,
            self._start_auto_profile_switch,
            args=(revision, desired),
        )
        timer.daemon = True
        self._auto_profile_switch_timer = timer
        timer.start()

    def _request_auto_profile_switch(
        self,
        profile_name,
        payload_data=None,
        *,
        process_name=None,
        entry=None,
    ):
        """Latest-wins scheduling for a foreground rule.

        This replaces the old scanner-side ``sleep(); apply_payload()``.
        Scanning therefore stays responsive while an older timer/transaction
        is invalidated immediately by another Alt+Tab.  ``profile_name=None``
        is meaningful: it cancels a previously queued binding when focus
        moves to an unbound process.
        """
        lock = self._ensure_auto_profile_switch_state()
        with lock:
            previous = getattr(self, "_auto_profile_switch_desired", None)
            self._auto_profile_switch_revision += 1
            revision = self._auto_profile_switch_revision
            self._cancel_auto_profile_switch_timer_locked()

            if profile_name is None or payload_data is None:
                desired = None
            else:
                active_entry = entry if isinstance(entry, dict) else self._active_device()
                delay_ms = _resolved_profile_switch_delay_ms(active_entry)
                desired = {
                    "profile_name": str(profile_name),
                    # A profile editor can mutate its list while a timer is
                    # waiting.  Detach the bytes now so the HID worker sees
                    # exactly the profile that was selected for this window.
                    "payload": list(payload_data),
                    "process_name": str(process_name).casefold() if process_name else None,
                    "device_key": self.config.get("active_device"),
                    "ready_at": time.monotonic() + (delay_ms / 1000.0),
                }
            self._auto_profile_switch_desired = desired
            self._arm_auto_profile_switch_locked()

        # A rapid focus change must invalidate an already-running app-side
        # magnetic preset immediately.  The normal profile command remains
        # latest-wins above; this prevents a 75-key magnetic write from
        # continuing for a window the user has already left.
        previous_name = previous.get("profile_name") if isinstance(previous, dict) else None
        next_name = desired.get("profile_name") if isinstance(desired, dict) else None
        if previous_name != next_name:
            try:
                self._stop_magnetic_profile_switching()
            except Exception:
                logger.debug("could not invalidate magnetic preset switch", exc_info=True)
        return revision

    def _start_auto_profile_switch(self, revision, request):
        """Move a due timer into the one foreground HID worker."""
        lock = self._ensure_auto_profile_switch_state()
        with lock:
            if (
                getattr(self, "_auto_profile_switch_revision", -1) != revision
                or getattr(self, "_auto_profile_switch_desired", None) is not request
                or not getattr(self, "is_running", False)
            ):
                return
            # A Timer can fire fractionally early on some Windows clocks.
            # Re-arm exactly once rather than starting before the configured
            # settle delay has elapsed.
            remaining = max(0.0, request.get("ready_at", 0.0) - time.monotonic())
            if remaining > 0.001:
                self._cancel_auto_profile_switch_timer_locked()
                timer = threading.Timer(
                    remaining,
                    self._start_auto_profile_switch,
                    args=(revision, request),
                )
                timer.daemon = True
                self._auto_profile_switch_timer = timer
                timer.start()
                return
            if getattr(self, "_auto_profile_switch_worker_active", False):
                return
            if (
                request.get("profile_name") == getattr(self, "current_binding", None)
                and not getattr(self, "_auto_profile_switch_transport_uncertain", False)
            ):
                self._auto_profile_switch_timer = None
                return
            self._auto_profile_switch_timer = None
            self._auto_profile_switch_worker_active = True
            # Once a worker begins, a cancelled profile packet may have
            # reached the keyboard.  Do not use ``current_binding`` as proof
            # of hardware state until the current worker completes.
            self._auto_profile_switch_transport_uncertain = True

        def worker():
            succeeded = False
            try:
                if not self._auto_profile_switch_is_current(revision, request):
                    return
                if not self._foreground_process_matches(request.get("process_name")):
                    logger.debug(
                        "foreground profile request skipped: process changed before HID (%s)",
                        request.get("process_name"),
                    )
                    return

                succeeded = bool(
                    self.apply_payload(
                        request["profile_name"],
                        request["payload"],
                        should_continue=lambda: (
                            self._auto_profile_switch_is_current(revision, request)
                            and self._foreground_process_matches(request.get("process_name"))
                        ),
                        suppress_input=False,
                        automatic=True,
                    )
                )
            except Exception:
                logger.exception("foreground profile switch failed")
            finally:
                with lock:
                    is_latest = (
                        getattr(self, "_auto_profile_switch_revision", -1) == revision
                        and getattr(self, "_auto_profile_switch_desired", None) is request
                    )
                    self._auto_profile_switch_worker_active = False
                    if is_latest and succeeded:
                        self._auto_profile_switch_transport_uncertain = False
                    # Only a newer foreground request warrants another run.
                    # A stable hardware failure should not retry in a tight
                    # loop against the HID endpoint.
                    if not is_latest:
                        self._arm_auto_profile_switch_locked()

        threading.Thread(
            target=worker,
            daemon=True,
            name="foreground-profile-switch",
        ).start()

    def _stop_auto_profile_switching(self, *, cancel_magnetic=True):
        """Invalidate timers/workers during service or device lifecycle changes."""
        lock = self._ensure_auto_profile_switch_state()
        with lock:
            self._auto_profile_switch_revision += 1
            self._auto_profile_switch_desired = None
            self._cancel_auto_profile_switch_timer_locked()
        if cancel_magnetic:
            try:
                self._stop_magnetic_profile_switching()
            except Exception:
                logger.debug("could not stop magnetic preset worker", exc_info=True)

    # ---------- Page setup ----------
    def _build_page(self):
        # This native title is also what Windows shows in the taskbar and
        # Alt+Tab.  Keep the supported keyboard visible there, not only in
        # the in-app heading.
        self.page.title = "QMK.Top Manager for SK75 TMR"
        # The main workflow is a keyboard layout plus detailed controls, so a
        # landscape window is the useful default.  The left navigation stays
        # available while the central panel scrolls.
        self.page.window.width = 1360
        self.page.window.height = 820
        # This interface has one measured 75% keyboard layout rather than a
        # fluid document view.  Keep the native shell at that measured size:
        # it prevents a resize from clipping the navigation cluster or making
        # the magnetic controls jump between layouts.
        self.page.window.min_width = 1360
        self.page.window.min_height = 820
        self.page.window.max_width = 1360
        self.page.window.max_height = 820
        self.page.window.resizable = False
        # Keep the application in its intended desktop layout. A maximized
        # native window stretches the fixed 75% deck and makes its physical
        # spacing misleading; this also disables maximize by double-clicking
        # the small native drag header.
        self.page.window.maximizable = False
        self.page.window.maximized = False
        self.page.window.full_screen = False
        try:
            icon_path = getattr(self, "application_icon_path", None) or self._resolve_application_icon_path()
            if icon_path and os.path.isfile(icon_path):
                self.page.window.icon = icon_path
        except Exception:
            pass
        self.page.padding = 0
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.theme = ft.Theme(
            color_scheme_seed=ft.Colors.DEEP_PURPLE,
            use_material3=True,
        )
        self.page.dark_theme = ft.Theme(
            color_scheme_seed=ft.Colors.DEEP_PURPLE,
            use_material3=True,
        )
        self.page.bgcolor = ft.Colors.SURFACE
        # ``Page.on_keyboard_event`` is the one Flet event that receives a
        # key regardless of which form control currently owns focus.  The
        # desktop app deliberately reserves Tab/Shift+Tab for its three
        # top-level workspaces; otherwise a profile field or a slider turns
        # the shortcut into an unpredictable, long focus walk through a hidden
        # part of the UI.
        self.page.on_keyboard_event = self._on_page_keyboard_event

        self.page.window.prevent_close = True
        self.page.window.on_event = self._handle_window_event

        # ``--show`` is an explicit user action from the tray/launcher.  It
        # must override the persisted start-minimised preference here too;
        # otherwise the Flet shell is created successfully but immediately
        # hides itself before Windows can bring it to the foreground.
        if (
            self.config.get("settings", {}).get("start_minimized", False)
            and not self.force_visible
        ):
            self.page.window.visible = False
            self.page.window.skip_task_bar = True
            # Re-assert hidden state on the UI loop after Flet finishes its
            # initial paint — otherwise Flet sometimes shows the window anyway
            # because `visible=False` set in __init__ races with the first
            # frame being pushed to the native shell.
            def _enforce_hidden():
                time.sleep(0.3)
                def do():
                    try:
                        self.page.window.visible = False
                        self.page.window.skip_task_bar = True
                        try:
                            self.page.window.minimized = True
                        except Exception:
                            pass
                        self.page.update()
                    except Exception:
                        pass
                self._ui_call(do)
            threading.Thread(target=_enforce_hidden, daemon=True).start()

    # ---------- UI ----------
    def _build_ui(self):
        self.battery_chip_icon = ft.Icon(ft.Icons.BATTERY_UNKNOWN, size=16, color=ft.Colors.ON_SURFACE_VARIANT)
        self.battery_chip_text = ft.Text("—", size=12, weight=ft.FontWeight.W_500)
        self.battery_chip_refresh = ft.IconButton(
            icon=ft.Icons.REFRESH_ROUNDED,
            icon_size=14,
            tooltip="Обновить уровень батареи",
            on_click=lambda e: threading.Thread(target=self._refresh_battery_now, daemon=True).start(),
        )
        self.battery_chip = ft.Container(
            content=ft.Row(
                [self.battery_chip_icon, self.battery_chip_text, self.battery_chip_refresh],
                spacing=4, tight=True,
            ),
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            border_radius=100,
            tooltip="Уровень заряда клавиатуры",
        )

        # This is deliberately a *connection recovery*, not Womier's factory
        # reset command.  The official 0x01 reset packet erases keyboard
        # configuration, so the action below only stops diagnostic modes,
        # clears stale HID handles and re-enumerates the device.
        self.keyboard_recovery_button = ft.IconButton(
            icon=ft.Icons.RESTART_ALT_ROUNDED,
            icon_size=19,
            width=42,
            height=42,
            tooltip=(
                "Восстановить связь с клавиатурой: остановить зависшие режимы "
                "и обновить HID. Настройки не стираются."
            ),
            on_click=lambda _event: self._recover_keyboard_connection(),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=14),
                bgcolor={"": ft.Colors.SURFACE_CONTAINER_HIGHEST},
            ),
        )

        self.transport_icon = ft.Icon(
            ft.Icons.USB,
            size=16,
            color=ft.Colors.ON_SURFACE_VARIANT,
            tooltip="Тип подключения активного устройства",
            visible=False,
        )

        # There is no manual operating mode any more.  Keep the service
        # action available, but make it a compact icon in the header rather
        # than a misleading text status plus a second button at the bottom.
        self.toggle_button = ft.IconButton(
            icon=ft.Icons.PLAY_ARROW_ROUNDED,
            icon_size=21,
            tooltip="Запустить службу",
            width=42,
            height=42,
            mouse_cursor=ft.MouseCursor.CLICK,
            on_click=lambda e: self.toggle_service(),
            style=self._service_button_style(running=False),
        )

        header = ft.Container(
            content=ft.Row(
                [
                    ft.Row(
                        [
                            ft.Container(
                                content=ft.Icon(ft.Icons.KEYBOARD_ALT_ROUNDED, size=26, color=ft.Colors.ON_PRIMARY),
                                width=44, height=44,
                                bgcolor=ft.Colors.PRIMARY,
                                border_radius=14,
                                alignment=ft.Alignment.CENTER,
                            ),
                            ft.Column(
                                [
                                    ft.Text("QMK.Top Manager · SK75 TMR", size=20, weight=ft.FontWeight.W_600),
                                    ft.Text("Профили, автоматизация и подсветка SK75 TMR", size=12,
                                            color=ft.Colors.ON_SURFACE_VARIANT),
                                ],
                                spacing=0,
                                tight=True,
                            ),
                        ],
                        spacing=12,
                    ),
                    # Adding recovery before the service control naturally
                    # moves the connection/battery group left, leaving a clear
                    # dedicated place for the non-destructive recovery action.
                    ft.Row(
                        [
                            self.transport_icon,
                            self.battery_chip,
                            self.keyboard_recovery_button,
                            self.toggle_button,
                        ],
                        spacing=8,
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            padding=ft.Padding.symmetric(horizontal=24, vertical=20),
        )

        self.device_dropdown = self._app_dropdown(
            label="HID устройство",
            expand=True,
            border_radius=12,
            filled=True,
            options=[],
            on_select=lambda e: self._on_device_dropdown_changed(),
        )
        self.transport_override = None
        refresh_btn = ft.IconButton(
            icon=ft.Icons.REFRESH_ROUNDED,
            tooltip="Обновить список",
            on_click=lambda e: self.refresh_devices(),
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.SECONDARY_CONTAINER,
                color=ft.Colors.ON_SECONDARY_CONTAINER,
                shape=ft.RoundedRectangleBorder(radius=12),
                padding=14,
            ),
        )
        sniffer_open_btn = ft.FilledTonalButton(
            "Sniffer / настройка",
            icon=ft.Icons.SENSORS_ROUNDED,
            on_click=lambda e: self.open_sniffer_modal(),
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)),
        )

        device_card_controls = [
            ft.Row([self.device_dropdown, refresh_btn], spacing=8),
        ]
        if self.config.get("settings", {}).get("debug", False):
            device_card_controls.append(
                ft.Row([sniffer_open_btn], alignment=ft.MainAxisAlignment.END),
            )

        self.payloads_column = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO)
        # The keyboard picker and its four application profiles belong to one
        # workflow.  Keeping them in a single workspace prevents a user from
        # having to bounce between two nearly identical top-level sections.
        device_profiles_card = self._card(
            icon=ft.Icons.KEYBOARD_ALT_ROUNDED,
            title="Клавиатура и профили",
            subtitle="Выберите QMK-клавиатуру и настройте четыре профиля.",
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text("Устройство", size=13, weight=ft.FontWeight.W_600),
                        ft.Column(device_card_controls, spacing=10),
                        ft.Divider(height=16, opacity=0.28),
                        ft.Text("Профили", size=13, weight=ft.FontWeight.W_600),
                        ft.Text(
                            "Четыре фиксированных профиля: имя и параметры можно менять.",
                            size=11,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        self.payloads_column,
                    ],
                    spacing=8,
                ),
                margin=ft.Margin.only(top=12),
            ),
        )

        lighting_lab_card = self._build_lighting_lab_card()
        magnetic_lab_card = self._build_magnetic_lab_card()

        # Keep rule actions close to their process name, but use the remaining
        # desktop width for a real status/guide panel instead of an empty
        # right-hand area.
        binding_list_width = 690
        binding_summary_width = 382
        self.bindings_list_width = binding_list_width
        self.bindings_column = ft.Column(
            spacing=8,
            scroll=ft.ScrollMode.AUTO,
            width=binding_list_width,
            tight=True,
        )
        add_binding_btn = ft.FilledTonalButton(
            "Создать привязку",
            icon=ft.Icons.ADD_LINK_ROUNDED,
            on_click=lambda e: self.open_binding_dialog("Новая привязка"),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=12),
                elevation={
                    ft.ControlState.DEFAULT: 1,
                    ft.ControlState.HOVERED: 4,
                    ft.ControlState.PRESSED: 0,
                },
            ),
        )
        # Filtering is deliberately local-only: typing here must never change
        # a rule or trigger a profile switch.  It is useful with a long list of
        # game executables, while the original index is preserved for edit/
        # delete/toggle callbacks.
        self.binding_search_clear_button = ft.IconButton(
            icon=ft.Icons.CLOSE_ROUNDED,
            icon_size=18,
            tooltip="Очистить поиск",
            on_click=self._clear_binding_search,
            # Keep this a bare, always-available affordance.  A disabled
            # IconButton is hard to see in a dense search field, and clearing
            # an already empty query is a harmless no-op.  Explicitly making
            # every state transparent also avoids the purple rounded tile that
            # used to appear under the cross on hover.
            width=32,
            height=32,
            padding=0,
            style=ft.ButtonStyle(
                color={
                    ft.ControlState.DEFAULT: ft.Colors.ON_SURFACE_VARIANT,
                    ft.ControlState.HOVERED: ft.Colors.PRIMARY,
                    ft.ControlState.PRESSED: ft.Colors.PRIMARY,
                },
                bgcolor=ft.Colors.TRANSPARENT,
                overlay_color=ft.Colors.TRANSPARENT,
                shape=ft.RoundedRectangleBorder(radius=0),
            ),
        )
        self.binding_search_field = ft.TextField(
            hint_text="Поиск процесса или профиля",
            prefix_icon=ft.Icons.SEARCH_ROUNDED,
            suffix=self.binding_search_clear_button,
            height=42,
            expand=True,
            dense=True,
            on_change=self._on_binding_search_changed,
            border_radius=12,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=8),
        )
        self.bindings_total_value = ft.Text("0", size=24, weight=ft.FontWeight.W_700)
        self.bindings_enabled_value = ft.Text("0", size=24, weight=ft.FontWeight.W_700)
        self.bindings_profiles_value = ft.Text("0", size=24, weight=ft.FontWeight.W_700)
        self.bindings_summary_status = ft.Text(
            "Правила появятся здесь после создания первой привязки.",
            size=11,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )

        def binding_metric(value, label, color):
            return ft.Container(
                content=ft.Column(
                    [
                        ft.Container(
                            content=value,
                            width=40,
                            height=34,
                            alignment=ft.Alignment.CENTER_LEFT,
                        ),
                        ft.Text(label, size=10, color=ft.Colors.ON_SURFACE_VARIANT),
                    ],
                    spacing=0,
                    tight=True,
                ),
                width=108,
                padding=10,
                border_radius=12,
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                border=ft.Border.all(1, color),
            )

        binding_summary = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Container(
                                content=ft.Icon(
                                    ft.Icons.AUTO_AWESOME_ROUNDED,
                                    size=18,
                                    color=ft.Colors.ON_TERTIARY_CONTAINER,
                                ),
                                width=34,
                                height=34,
                                border_radius=11,
                                bgcolor=ft.Colors.TERTIARY_CONTAINER,
                                alignment=ft.Alignment.CENTER,
                            ),
                            ft.Column(
                                [
                                    ft.Text("Автопереключение", size=14, weight=ft.FontWeight.W_600),
                                    ft.Text(
                                        "Сводка правил для активного устройства",
                                        size=10,
                                        color=ft.Colors.ON_SURFACE_VARIANT,
                                    ),
                                ],
                                spacing=1,
                                tight=True,
                            ),
                        ],
                        spacing=9,
                    ),
                    ft.Row(
                        [
                            binding_metric(self.bindings_total_value, "всего", ft.Colors.PRIMARY),
                            binding_metric(self.bindings_enabled_value, "активны", ft.Colors.TERTIARY),
                            binding_metric(self.bindings_profiles_value, "профили", ft.Colors.SECONDARY),
                        ],
                        spacing=12,
                    ),
                    self.bindings_summary_status,
                    ft.Divider(height=1, opacity=0.3),
                    ft.Text("Как это работает", size=12, weight=ft.FontWeight.W_600),
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.LOOKS_ONE_ROUNDED, size=17, color=ft.Colors.PRIMARY),
                            ft.Text("Укажите только имя процесса: например, cs2.exe.", size=10),
                        ],
                        spacing=7,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.LOOKS_TWO_ROUNDED, size=17, color=ft.Colors.PRIMARY),
                            ft.Text("Выберите профиль, который нужен для этой программы.", size=10),
                        ],
                        spacing=7,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.LOOKS_3_ROUNDED, size=17, color=ft.Colors.PRIMARY),
                            ft.Text("Запустите службу кнопкой ▶ в верхней панели.", size=10),
                        ],
                        spacing=7,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=10,
                tight=True,
            ),
            width=binding_summary_width,
            padding=16,
            bgcolor=ft.Colors.SURFACE_CONTAINER,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=16,
            shadow=ft.BoxShadow(blur_radius=16, spread_radius=-9, color="#443F51B5"),
        )
        bindings_card = self._card(
            icon=ft.Icons.LINK_ROUNDED,
            title="Привязки к процессам",
            subtitle="При активации окна с этим процессом автоматически применяется профиль.",
            content=ft.Container(
                content=ft.Row(
                    [
                        ft.Column(
                            [
                                ft.Row(
                                    [add_binding_btn, self.binding_search_field],
                                    alignment=ft.MainAxisAlignment.START,
                                    width=binding_list_width,
                                    spacing=10,
                                ),
                                self.bindings_column,
                            ],
                            width=binding_list_width,
                            spacing=8,
                            tight=True,
                        ),
                        binding_summary,
                    ],
                    spacing=16,
                    wrap=True,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                margin=ft.Margin.only(top=12),
            ),
        )
        bindings_card.width = binding_list_width + binding_summary_width + 16 + 40

        settings = self.config.get("settings", {})

        self.start_minimized_switch = ft.Switch(
            value=settings.get("start_minimized", False),
            on_change=lambda e: self._set_setting("start_minimized", e.control.value),
        )
        self.autostart_switch = ft.Switch(
            value=settings.get("autostart_service", False),
            on_change=lambda e: self._set_setting("autostart_service", e.control.value),
        )
        from autostart import autostart_enabled, set_autostart
        self.autostart_windows_switch = ft.Switch(
            value=autostart_enabled(),
            on_change=self._on_autostart_windows_changed,
        )
        active_entry = self._active_device()
        self.profile_switch_delay_dropdown = self._app_dropdown(
            # The tile itself already says this applies after Alt+Tab.  A
            # compact field label keeps the selected delay on one quiet line
            # instead of making this otherwise equal-height tile grow.
            label="Задержка, мс",
            value=str(_resolved_profile_switch_delay_ms(active_entry)),
            options=[
                ft.dropdown.Option(key=str(value), text=f"{value} мс")
                for value in PROFILE_SWITCH_DELAY_VALUES_MS
            ],
            width=156,
            border_radius=12,
            filled=True,
            disabled=active_entry is None,
            on_select=self._on_profile_switch_delay_changed,
        )
        # One deliberate entry point keeps copying and importing together.
        # The dialog holds a fresh local category selection every time, so no
        # previous clipboard choice silently leaks into a later transfer.
        configuration_transfer = ft.FilledTonalButton(
            "CFG",
            icon=ft.Icons.SETTINGS_BACKUP_RESTORE_ROUNDED,
            tooltip="Экспорт и импорт выбранных разделов CFG",
            on_click=lambda e: self._open_config_import_dialog(),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=14),
            ),
        )

        def settings_item(icon, title, subtitle, control, accent):
            """A full-width responsive preference tile for the Keyboard page."""
            return ft.Container(
                content=ft.Row(
                    [
                        ft.Container(
                            content=ft.Icon(icon, size=18, color=accent),
                            width=34,
                            height=34,
                            alignment=ft.Alignment.CENTER,
                            bgcolor=ft.Colors.with_opacity(0.14, accent),
                            border_radius=11,
                        ),
                        ft.Column(
                            [
                                ft.Text(
                                    title,
                                    size=13,
                                    weight=ft.FontWeight.W_600,
                                    max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                                ft.Text(
                                    subtitle,
                                    size=10,
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                    max_lines=2,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                        control,
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                col={"sm": 12, "md": 6},
                # Each preference is a tile in one two-by-two grid.  Fixing
                # their inner height avoids a long subtitle or Dropdown
                # label pushing just one card lower than its neighbour.
                height=82,
                padding=12,
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                border_radius=14,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            )

        settings_card = self._card(
            icon=ft.Icons.SETTINGS_ROUNDED,
            title="Настройки",
            subtitle="Параметры запуска и поведения приложения.",
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Column(
                                [
                                    ft.Text("Конфигурация", size=13, weight=ft.FontWeight.W_600),
                                    ft.Text(
                                        "Выберите разделы для экспорта или импорта CFG.",
                                        size=10,
                                        color=ft.Colors.ON_SURFACE_VARIANT,
                                    ),
                                ],
                                spacing=2,
                                expand=True,
                            ),
                            configuration_transfer,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.ResponsiveRow(
                        [
                            settings_item(
                                ft.Icons.VERTICAL_ALIGN_BOTTOM_ROUNDED,
                                "Запускать свёрнутым в трей",
                                "При старте окно будет скрыто, останется только значок в трее.",
                                self.start_minimized_switch,
                                ft.Colors.PRIMARY,
                            ),
                            settings_item(
                                ft.Icons.PLAY_CIRCLE_OUTLINE_ROUNDED,
                                "Автоматически запускать службу",
                                "Фоновое переключение профилей включится сразу после запуска.",
                                self.autostart_switch,
                                ft.Colors.TERTIARY,
                            ),
                            settings_item(
                                ft.Icons.WINDOW_ROUNDED,
                                "Запускать с Windows",
                                "Приложение будет открываться при входе в систему.",
                                self.autostart_windows_switch,
                                ft.Colors.SECONDARY,
                            ),
                            settings_item(
                                ft.Icons.SWAP_HORIZ_ROUNDED,
                                "Переключение после Alt+Tab",
                                "Пауза для стабилизации активного окна. 0 мс — самый быстрый вариант.",
                                self.profile_switch_delay_dropdown,
                                ft.Colors.ORANGE,
                            ),
                        ],
                        spacing=12,
                        run_spacing=12,
                    ),
                ],
                spacing=12,
            ),
        )
        # Device selection, lighting and app preferences are parts of one
        # everyday keyboard workspace.  Keep them in one scrolling panel so
        # the user does not need to jump through three top-level sections to
        # prepare the keyboard.  The magnetic editor and process bindings
        # remain independent workspaces because they have their own focused
        # interaction model.
        keyboard_workspace = ft.Column(
            [
                device_profiles_card,
                lighting_lab_card,
                settings_card,
            ],
            spacing=12,
            # Do not cap the Settings card at the key-deck width.  This page
            # owns the entire workspace beside the navigation rail, and the
            # responsive settings grid should use every available column.
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

        self.sniff_log = ft.ListView(
            spacing=6, padding=8, auto_scroll=False,
            on_scroll=self._on_sniff_scroll,
        )
        self._sniff_auto_scroll = True
        self.sniff_status = ft.Text("Сниффер остановлен.", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        self.sniff_button = ft.FilledTonalButton(
            "Запустить sniff",
            icon=ft.Icons.SENSORS_ROUNDED,
            on_click=lambda e: self.toggle_sniffer(),
            disabled=OFFLINE_MODE,
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)),
        )
        self.sniff_clear_button = ft.OutlinedButton(
            "Очистить",
            icon=ft.Icons.CLEAR_ALL_ROUNDED,
            on_click=lambda e: self.clear_sniffer_log(),
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)),
        )
        self.sniff_copy_button = ft.OutlinedButton(
            "Скопировать JSON",
            icon=ft.Icons.COPY_ROUNDED,
            on_click=lambda e: self.copy_sniffer_log(),
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)),
        )
        self.sniff_learn_switch = ft.Switch(
            label="Learn mode (показать все TX)",
            value=False,
            on_change=self._toggle_learn_mode,
        )

        self.browser_picker = ft.FilePicker()
        self.clipboard = ft.Clipboard()
        self.page.services.append(self.browser_picker)
        self.page.services.append(self.clipboard)
        self.browser_path_text = ft.Text(
            self._browser_label(), size=11, color=ft.Colors.ON_SURFACE_VARIANT, italic=True,
        )
        self.browser_pick_button = ft.OutlinedButton(
            "Указать браузер…",
            icon=ft.Icons.FOLDER_OPEN_ROUNDED,
            on_click=lambda e: self._open_browser_picker(),
            disabled=OFFLINE_MODE,
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)),
        )

        section_cards = [
            ("keyboard", "Клавиатура & RGB", ft.Icons.KEYBOARD_ALT_ROUNDED, keyboard_workspace),
            ("magnetic", "Magnetic Lab", ft.Icons.TOLL_ROUNDED, magnetic_lab_card),
            ("rules", "Привязки", ft.Icons.LINK_ROUNDED, bindings_card),
        ]
        # This is a desktop control centre, not a long landing page.  Keep all
        # sections attached to the control tree (so background refreshes can
        # still update their controls), but show exactly one of them at once.
        # It makes the left-side navigation a real section switcher instead
        # of a collection of fragile scroll-to anchors.
        self.section_nav_order = [anchor for anchor, _label, _icon, _card in section_cards]
        self.section_nav_active = "keyboard"
        self._visible_section = "keyboard"
        self._section_switch_revision = 0
        self.section_panels = {}
        panel_controls = []
        for anchor, _label, _icon, card in section_cards:
            is_initial = anchor == self.section_nav_active
            panel = ft.Container(
                content=card,
                key=f"section-panel-{anchor}",
                visible=is_initial,
                opacity=1.0 if is_initial else 0.0,
                offset=ft.Offset(0, 0),
                animate_opacity=ft.Animation(
                    duration=150, curve=ft.AnimationCurve.EASE_OUT_CUBIC
                ),
                animate_offset=ft.Animation(
                    duration=150, curve=ft.AnimationCurve.EASE_OUT_CUBIC
                ),
            )
            self.section_panels[anchor] = panel
            panel_controls.append(panel)

        # ``visible=False`` keeps a panel out of layout while preserving the
        # Flet control objects.  That matters for values read from the keyboard
        # and for profile/process edits that arrive while another section is
        # open.
        self.section_content = ft.Column(
            panel_controls,
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

        # Keep the current page position for section switching.  Magnetic
        # controls intentionally no longer consume wheel input: scrolling
        # while the pointer is over a scale must scroll this Column normally.
        self._main_scroll_position = 0.0
        self.main_scroll = ft.Column(
            [self.section_content],
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            # Retain Flet's native desktop physics for the selected section.
            # The rail does not redraw on each wheel packet any more, which
            # keeps long sections (especially magnetic settings) smooth.
            scroll=ft.Scrollbar(
                thumb_visibility=False,
                track_visibility=False,
                thickness=6,
                radius=4,
                interactive=True,
            ),
            auto_scroll=False,
            # Flet's desktop Column owns the actual wheel physics.  We only
            # remember its position, so do not send a Python event for every
            # individual wheel packet: that was enough to make a long page
            # visibly stutter on some Windows systems.  Native painting stays
            # at the platform frame rate while bookkeeping is throttled.
            scroll_interval=64,
            on_scroll=self._on_main_scroll,
            expand=True,
        )

        # A permanent, labelled left-side menu makes the destination obvious
        # without relying on hover tooltips.  These remain native buttons so
        # mouse, touch and keyboard focus all use the same accessible action.
        self.section_nav_buttons = {}
        navigation_buttons = []
        for anchor, label, icon, _card in section_cards:
            button = ft.FilledTonalButton(
                label,
                icon=icon,
                width=144,
                height=48,
                tooltip=label,
                on_click=self._make_section_nav_handler(anchor),
                # Tab/Shift+Tab is handled centrally by
                # ``_on_page_keyboard_event``.  Do not attach an ``on_focus``
                # section switch here: Flutter's default focus traversal can
                # otherwise race the global shortcut and skip a workspace.
                on_hover=self._on_section_nav_hover,
                animate_scale=ft.Animation(
                    duration=140, curve=ft.AnimationCurve.EASE_OUT_BACK
                ),
                animate_offset=ft.Animation(
                    duration=140, curve=ft.AnimationCurve.EASE_OUT
                ),
                style=self._section_nav_button_style(anchor == self.section_nav_active),
            )
            self.section_nav_buttons[anchor] = button
            navigation_buttons.append(button)
        # The stock driver actions are deliberately paired, compact Material 3
        # tiles.  Each icon has its label above it so the two actions stay
        # understandable without relying on a hover tooltip.  They live
        # immediately above the travel test, where a user reaches for them
        # when handing the keyboard between applications.
        self.womier_driver_open_label = ft.Text(
            "Открыть",
            size=10,
            weight=ft.FontWeight.W_600,
            color=ft.Colors.PRIMARY,
            width=64,
            text_align=ft.TextAlign.CENTER,
            no_wrap=True,
        )
        self.womier_driver_open_nav_button = ft.IconButton(
            icon=ft.Icons.OPEN_IN_NEW_ROUNDED,
            icon_size=22,
            tooltip="Открыть официальный WOMIER Driver",
            width=64,
            height=52,
            padding=0,
            on_click=lambda _event: self._open_official_womier_driver(),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=14),
                color={
                    ft.ControlState.DEFAULT: ft.Colors.ON_PRIMARY_CONTAINER,
                    ft.ControlState.HOVERED: ft.Colors.ON_PRIMARY_CONTAINER,
                    ft.ControlState.PRESSED: ft.Colors.ON_PRIMARY_CONTAINER,
                    ft.ControlState.DISABLED: ft.Colors.ON_SURFACE_VARIANT,
                },
                bgcolor={
                    ft.ControlState.DEFAULT: ft.Colors.PRIMARY_CONTAINER,
                    ft.ControlState.HOVERED: ft.Colors.SECONDARY_CONTAINER,
                    ft.ControlState.PRESSED: ft.Colors.SECONDARY_CONTAINER,
                    ft.ControlState.DISABLED: ft.Colors.SURFACE_CONTAINER_HIGHEST,
                },
                elevation={
                    ft.ControlState.DEFAULT: 1,
                    ft.ControlState.HOVERED: 5,
                    ft.ControlState.PRESSED: 0,
                },
            ),
        )
        self.womier_driver_close_label = ft.Text(
            "Закрыть",
            size=10,
            weight=ft.FontWeight.W_600,
            color=ft.Colors.ERROR,
            width=64,
            text_align=ft.TextAlign.CENTER,
            no_wrap=True,
        )
        self.womier_driver_close_nav_button = ft.IconButton(
            icon=ft.Icons.POWER_SETTINGS_NEW_ROUNDED,
            icon_size=22,
            tooltip=(
                "Закрыть только официальный WOMIER Driver и его iot_driver "
                "по точному пути установки"
            ),
            width=64,
            height=52,
            padding=0,
            on_click=lambda _event: self._confirm_close_womier_driver_processes(),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=14),
                color={
                    ft.ControlState.DEFAULT: ft.Colors.ERROR,
                    ft.ControlState.HOVERED: ft.Colors.ON_ERROR_CONTAINER,
                    ft.ControlState.PRESSED: ft.Colors.ON_ERROR_CONTAINER,
                    ft.ControlState.DISABLED: ft.Colors.ON_SURFACE_VARIANT,
                },
                bgcolor={
                    ft.ControlState.DEFAULT: ft.Colors.SURFACE_CONTAINER_HIGH,
                    ft.ControlState.HOVERED: ft.Colors.ERROR_CONTAINER,
                    ft.ControlState.PRESSED: ft.Colors.ERROR_CONTAINER,
                    ft.ControlState.DISABLED: ft.Colors.SURFACE_CONTAINER_HIGHEST,
                },
                elevation={
                    ft.ControlState.DEFAULT: 1,
                    ft.ControlState.HOVERED: 5,
                    ft.ControlState.PRESSED: 0,
                },
            ),
        )
        self.womier_driver_actions_row = ft.Row(
            [
                ft.Column(
                    [
                        self.womier_driver_open_label,
                        self.womier_driver_open_nav_button,
                    ],
                    spacing=3,
                    tight=True,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Column(
                    [
                        self.womier_driver_close_label,
                        self.womier_driver_close_nav_button,
                    ],
                    spacing=3,
                    tight=True,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
            width=144,
            spacing=8,
            alignment=ft.MainAxisAlignment.CENTER,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
        self.magnetic_tester_nav_button = ft.OutlinedButton(
            "Проверка",
            icon=ft.Icons.SPEED_ROUNDED,
            tooltip="Проверить живой ход любой магнитной клавиши",
            width=144,
            height=48,
            on_click=lambda _event: self._open_magnetic_travel_tester(),
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=13),
                padding=ft.Padding.symmetric(horizontal=12, vertical=0),
            ),
        )
        section_drag_label = ft.Container(
            content=ft.Text(
                "РАЗДЕЛЫ",
                size=10,
                weight=ft.FontWeight.W_700,
                color=ft.Colors.ON_SURFACE_VARIANT,
            ),
            width=144,
            height=24,
            alignment=ft.Alignment.CENTER,
            border_radius=8,
            tooltip="Перетащите окно за эту область",
        )
        # Native title-bar dragging is supported by Flet 0.85.  Limiting the
        # drag area to this small rail header keeps every navigation button
        # beneath it a normal click target.  The fallback keeps the menu
        # usable if an older portable runtime is launched by accident.
        try:
            section_drag_header = ft.WindowDragArea(
                content=section_drag_label,
                maximizable=False,
            )
        except (AttributeError, TypeError):
            section_drag_header = section_drag_label
        navigation = ft.Container(
            content=ft.Column(
                [
                    ft.Column(
                        [
                            section_drag_header,
                            ft.Divider(height=1, opacity=0.28),
                            *navigation_buttons,
                        ],
                        spacing=8,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Container(expand=True),
                    ft.Column(
                        [
                            ft.Divider(height=1, opacity=0.28),
                            self.womier_driver_actions_row,
                            self.magnetic_tester_nav_button,
                        ],
                        spacing=8,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=0,
                expand=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=164,
            # The rail is a fixed part of the window, not a card embedded
            # below the header.  Its parent Row stretches it from top to
            # bottom, so the menu always starts at the top-left corner.
            padding=ft.Padding.only(left=10, top=18, right=10, bottom=18),
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.only(right=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
        )

        body = ft.Container(
            content=self.main_scroll,
            # A narrow outer gutter leaves enough real estate for the full
            # 75% key deck while keeping the labelled navigation on screen.
            padding=ft.Padding.only(left=12, top=8, bottom=8),
            expand=True,
        )

        # Keep the navigation rail outside the main workspace column: this
        # makes its background and border span the full window, including the
        # header area, instead of beginning below it.  It deliberately lives
        # on the left, matching the usual desktop control-centre reading flow.
        self.page.add(
            ft.Row(
                [
                    navigation,
                    ft.Column(
                        [header, body],
                        spacing=0,
                        expand=True,
                    ),
                ],
                spacing=0,
                expand=True,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            )
        )

        # A desktop client can keep a stale ScrollController position while
        # reconnecting after a restart.  Always begin at the top of the
        # keyboard workspace, never halfway through a magnetic panel.
        async def start_at_keyboard_section():
            await asyncio.sleep(0.12)
            try:
                await self._show_section("keyboard", animated=False)
                self._main_scroll_position = 0.0
            except Exception:
                logger.debug("could not set initial section position", exc_info=True)

        try:
            self.page.run_task(start_at_keyboard_section)
        except Exception:
            pass

    @staticmethod
    def _service_button_style(running: bool) -> ft.ButtonStyle:
        """Rounded, icon-only service control used in the app header."""
        if running:
            return ft.ButtonStyle(
                bgcolor=ft.Colors.ERROR_CONTAINER,
                color=ft.Colors.ON_ERROR_CONTAINER,
                shape=ft.RoundedRectangleBorder(radius=13),
                overlay_color={ft.ControlState.HOVERED: ft.Colors.ERROR},
            )
        return ft.ButtonStyle(
            bgcolor=ft.Colors.PRIMARY_CONTAINER,
            color=ft.Colors.ON_PRIMARY_CONTAINER,
            shape=ft.RoundedRectangleBorder(radius=13),
            overlay_color={ft.ControlState.HOVERED: ft.Colors.PRIMARY},
        )

    def _section_nav_button_style(self, is_active=False):
        """Return the labelled rectangular style for a left-side nav button."""
        default_background = (
            ft.Colors.PRIMARY_CONTAINER
            if is_active
            else ft.Colors.SURFACE_CONTAINER_HIGH
        )
        default_foreground = (
            ft.Colors.ON_PRIMARY_CONTAINER
            if is_active
            else ft.Colors.ON_SURFACE_VARIANT
        )
        return ft.ButtonStyle(
            color={
                ft.ControlState.DEFAULT: default_foreground,
                ft.ControlState.HOVERED: ft.Colors.ON_PRIMARY_CONTAINER,
                ft.ControlState.PRESSED: ft.Colors.ON_PRIMARY,
            },
            bgcolor={
                ft.ControlState.DEFAULT: default_background,
                ft.ControlState.HOVERED: ft.Colors.SECONDARY_CONTAINER,
                ft.ControlState.PRESSED: ft.Colors.PRIMARY,
            },
            overlay_color={ft.ControlState.HOVERED: ft.Colors.TRANSPARENT},
            side={
                ft.ControlState.DEFAULT: ft.BorderSide(
                    1,
                    ft.Colors.PRIMARY if is_active else ft.Colors.OUTLINE_VARIANT,
                ),
                ft.ControlState.HOVERED: ft.BorderSide(1, ft.Colors.PRIMARY),
            },
            elevation={
                ft.ControlState.DEFAULT: 2 if is_active else 0,
                ft.ControlState.HOVERED: 6,
                ft.ControlState.PRESSED: 1,
            },
            animation_duration=140,
            shape=ft.RoundedRectangleBorder(radius=13),
            padding=ft.Padding.symmetric(horizontal=12, vertical=0),
        )

    def _make_section_nav_handler(self, section):
        """Create an awaited handler for switching the visible workspace."""
        async def handle_click(_event):
            await self._show_section(section)

        return handle_click

    def _make_section_nav_focus_handler(self, section):
        """Compatibility handler for an explicit focus request on a rail item.

        Global Tab navigation is intentionally handled by
        :meth:`_on_page_keyboard_event`; this helper remains for callers and
        tests that explicitly focus a section button.
        """
        async def handle_focus(_event):
            if section != getattr(self, "_visible_section", None):
                await self._show_section(section)

        return handle_focus

    def _next_section_nav_target(self, *, reverse=False):
        """Return the next top-level workspace, wrapping at both ends.

        The selected workspace, rather than the currently focused widget, is
        the navigation source of truth.  A profile name field, a dropdown, or
        a slider can therefore have focus without changing the meaning of Tab.
        """
        order = tuple(getattr(self, "section_nav_order", ()) or ())
        if not order:
            return None
        current = getattr(self, "_visible_section", None)
        if current not in order:
            current = getattr(self, "section_nav_active", None)
        try:
            index = order.index(current)
        except ValueError:
            index = 0 if reverse else -1
        step = -1 if reverse else 1
        return order[(index + step) % len(order)]

    async def _focus_section_nav_button(self, section):
        """Restore a visible, stable focus target after a Tab section change."""
        button = getattr(self, "section_nav_buttons", {}).get(section)
        focus = getattr(button, "focus", None)
        if not callable(focus):
            return
        try:
            result = focus()
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Focus is cosmetic here.  The workspace itself has already
            # switched and must not be rolled back because a detached button
            # could not accept focus during a page rebuild.
            logger.debug("could not focus section navigation button", exc_info=True)

    async def _cycle_section_navigation(self, *, reverse=False):
        """Switch to the next/previous section and wrap around the rail."""
        target = self._next_section_nav_target(reverse=reverse)
        if target is None:
            return None
        await self._show_section(target)
        await self._focus_section_nav_button(target)
        return target

    async def _on_page_keyboard_event(self, event):
        """Reserve Tab and Shift+Tab for cyclic top-level navigation.

        Flet delivers this page event even when a TextField, a profile control,
        or a magnetic slider has native focus.  We intentionally ignore
        modifier shortcuts (Ctrl/Alt/Win) so platform combinations retain
        their normal behaviour; plain Tab and Shift+Tab only select the next
        or previous visible application section.
        """
        key = str(getattr(event, "key", "")).strip().lower()
        if key != "tab":
            return
        if any(bool(getattr(event, name, False)) for name in ("ctrl", "alt", "meta")):
            return
        await self._cycle_section_navigation(reverse=bool(getattr(event, "shift", False)))

    def _set_section_nav_active(self, section):
        # Do not redraw the menu when the already-visible workspace is chosen
        # again; that keeps repeated clicks and normal scrolling lightweight.
        if section == getattr(self, "section_nav_active", None):
            return
        self.section_nav_active = section
        # Flet can reconcile the rail while a focus transition is in flight;
        # take a stable snapshot so a late rebuild cannot mutate this iterator.
        for anchor, button in list(getattr(self, "section_nav_buttons", {}).items()):
            button.style = self._section_nav_button_style(anchor == section)
            try:
                button.update()
            except Exception:
                # The helper is also used during initial construction, before
                # the controls are attached to the Flet page.
                pass

    def _on_section_nav_hover(self, event):
        """Give a menu button a small, responsive lift on hover."""
        hovered = str(event.data).lower() == "true"
        event.control.scale = 1.025 if hovered else 1.0
        # The rail is on the left, therefore its hover nudge goes into the
        # workspace instead of toward the outer window edge.
        event.control.offset = ft.Offset(0.012, 0) if hovered else ft.Offset(0, 0)
        try:
            event.control.update()
        except Exception:
            pass

    async def _on_main_scroll(self, event):
        """Remember the selected workspace's native scroll position.

        Wheel input is deliberately left to the outer scrolling Column, even
        when it is above a magnetic scale.  This keeps standard desktop
        scrolling predictable and avoids fighting the native scroll physics.
        """
        try:
            position = float(event.pixels)
        except (AttributeError, TypeError, ValueError):
            return
        self._main_scroll_position = position

    async def _scroll_to_section(self, section):
        """Compatibility entry point for callers that used the old rail API."""
        await self._show_section(section)

    async def _show_section(self, section, *, animated=True):
        """Show one workspace section and reset that section's local scroll.

        All section panels remain in the page tree, but invisible panels take
        no layout space.  This prevents a hidden settings panel from breaking
        background updates while ensuring the user never has to scroll past
        unrelated pages to reach the requested workspace.
        """
        panels = getattr(self, "section_panels", {})
        panel = panels.get(section)
        if panel is None:
            logger.warning("unknown section navigation target: %s", section)
            return

        previous = getattr(self, "_visible_section", None)
        if previous == section:
            self._set_section_nav_active(section)
            try:
                await self.main_scroll.scroll_to(offset=0, duration=0)
                self._main_scroll_position = 0.0
            except Exception:
                logger.debug("could not reset current section scroll", exc_info=True)
            return

        # A tiny fade-in makes a section change feel deliberate without a
        # permanent animation task.  The revision token prevents a fast pair
        # of clicks from allowing an older click to fade the wrong panel in.
        self._section_switch_revision = getattr(self, "_section_switch_revision", 0) + 1
        revision = self._section_switch_revision
        # A section may be rebuilt by a background device refresh while Tab
        # navigation is changing focus.  Iterating a snapshot prevents the
        # exact ``dictionary changed size during iteration`` failure reported
        # during a Rapid Trigger toggle + scroll.
        for anchor, other_panel in list(panels.items()):
            if anchor == section:
                continue
            other_panel.visible = False
            other_panel.opacity = 0.0
            other_panel.offset = ft.Offset(0, 0)

        panel.visible = True
        panel.opacity = 0.0 if animated else 1.0
        panel.offset = ft.Offset(0, 0.012) if animated else ft.Offset(0, 0)
        self._visible_section = section
        self._set_section_nav_active(section)
        try:
            self.section_content.update()
        except Exception:
            # During startup the controls may not yet be mounted.  The
            # properties above are still retained for Flet's first render.
            pass
        try:
            await self.main_scroll.scroll_to(offset=0, duration=0)
            self._main_scroll_position = 0.0
        except Exception:
            logger.debug("could not reset section scroll", exc_info=True)

        if not animated:
            return
        await asyncio.sleep(0.016)
        if revision != getattr(self, "_section_switch_revision", 0):
            return
        panel.opacity = 1.0
        panel.offset = ft.Offset(0, 0)
        try:
            panel.update()
        except Exception:
            pass

    @staticmethod
    def _dropdown_menu_style():
        """Return the shared, bounded Material menu presentation.

        Flet 0.85 deliberately leaves the Dropdown popup transition to the
        native Material implementation; it does not expose an animation
        duration property.  A fixed visual style and a bounded menu avoid a
        large, abrupt full-screen expansion while preserving that native
        fade/scale transition and keyboard navigation.
        """
        return ft.MenuStyle(
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
            shadow_color=ft.Colors.with_opacity(0.38, ft.Colors.BLACK),
            elevation=8,
            padding=ft.Padding.symmetric(vertical=4),
            side=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT),
            shape=ft.RoundedRectangleBorder(radius=12),
        )

    def _app_dropdown(self, **kwargs):
        """Create a consistent desktop dropdown without an oversized popup."""
        kwargs.setdefault("menu_height", 280)
        kwargs.setdefault("menu_style", self._dropdown_menu_style())
        return ft.Dropdown(**kwargs)

    def _card(self, icon, title, subtitle, content):
        return ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Container(
                                content=ft.Icon(icon, size=20, color=ft.Colors.ON_SECONDARY_CONTAINER),
                                width=36, height=36,
                                bgcolor=ft.Colors.SECONDARY_CONTAINER,
                                border_radius=12,
                                alignment=ft.Alignment.CENTER,
                            ),
                            ft.Column(
                                [
                                    ft.Text(title, size=16, weight=ft.FontWeight.W_600),
                                    ft.Text(subtitle, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                                ],
                                spacing=2,
                                tight=True,
                            ),
                        ],
                        spacing=12,
                    ),
                    content,
                ],
                spacing=4,
            ),
            padding=20,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border_radius=20,
        )

    # ---------- Lighting Lab ----------
    def _build_lighting_lab_card(self):
        """Build controls for the built-in Womier lighting effects."""
        entry = self._active_device() or {}
        settings = LightingSettings.from_config(entry.get("lighting_lab"))

        stock_effects = dict(EFFECTS)
        displayed_effect = settings.effect if settings.effect in stock_effects else 1
        if displayed_effect != settings.effect:
            settings = LightingSettings(
                effect=displayed_effect,
                color=settings.color,
                brightness=settings.brightness,
                speed=settings.speed,
                option=0,
                rainbow=False,
            )

        self.lighting_effect_dropdown = self._app_dropdown(
            label="Эффект прошивки",
            value=str(displayed_effect),
            options=[
                ft.dropdown.Option(key=str(code), text=name)
                for code, name in stock_effects.items()
            ],
            width=235,
            border_radius=12,
            filled=True,
        )
        self.lighting_direction_dropdown = self._app_dropdown(
            label="Направление / вариант",
            value=str(settings.option),
            options=[
                ft.dropdown.Option(key=str(index), text=label)
                for index, label in enumerate(EFFECT_OPTIONS.get(settings.effect, ("По умолчанию",)))
            ],
            width=210,
            border_radius=12,
            filled=True,
        )

        def _refresh_effect_options(event):
            effect = int(event.control.value or 0)
            options = EFFECT_OPTIONS.get(effect, ("По умолчанию",))
            self.lighting_direction_dropdown.options = [
                ft.dropdown.Option(key=str(index), text=label)
                for index, label in enumerate(options)
            ]
            self.lighting_direction_dropdown.value = "0"
            # Updating the whole page here makes the colour picker and the
            # rest of the current workspace redraw unnecessarily.  Only the
            # variant dropdown and the local visual preview changed.
            try:
                self.lighting_direction_dropdown.update()
            except Exception:
                pass
            self._refresh_lighting_color_capability(effect)
            self._refresh_lighting_effect_preview()

        self.lighting_effect_dropdown.on_select = _refresh_effect_options
        # Both colour editors occupy the same fixed 254 px value slot.  This
        # keeps the copy action locked to the right edge when switching
        # between HEX and RGB instead of letting the RGB labels push the row
        # down or sideways.
        self.lighting_primary_color = ft.TextField(
            label="HEX",
            value=rgb_to_hex(settings.color),
            hint_text=NEUTRAL_LIGHTING_COLOR_HEX,
            width=202,
            border_radius=12,
            filled=True,
        )
        self.lighting_color_format_dropdown = self._app_dropdown(
            label="Формат цвета",
            value="hex",
            options=[
                ft.dropdown.Option(key="hex", text="HEX · #RRGGBB"),
                ft.dropdown.Option(key="rgb", text="RGB · 0–255"),
            ],
            width=180,
            border_radius=12,
            filled=True,
        )
        # Keep three channels compact enough for the copy action to stay on
        # the same line.  Their built-in R/G/B labels are deliberately used
        # instead of a second outer ``RGB`` caption, which means their input
        # boxes share the exact baseline of the HEX field and format dropdown.
        self.lighting_primary_rgb_fields = [
            ft.TextField(label=channel, value=str(value), width=64, border_radius=12, filled=True)
            for channel, value in zip(("R", "G", "B"), settings.color)
        ]
        # Keep a copy affordance next to both representations.  The selected
        # format decides the text put into the clipboard, but separate small
        # buttons mean the action never moves when the user switches HEX/RGB.
        self.lighting_hex_copy_button = ft.IconButton(
            icon=ft.Icons.CONTENT_COPY_ROUNDED,
            icon_size=18,
            tooltip="Скопировать HEX",
            on_click=self._copy_lighting_color,
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10)),
        )
        self.lighting_rgb_copy_button = ft.IconButton(
            icon=ft.Icons.CONTENT_COPY_ROUNDED,
            icon_size=18,
            tooltip="Скопировать RGB",
            on_click=self._copy_lighting_color,
            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=10)),
        )
        copy_button_glow = ft.BoxShadow(
            blur_radius=10,
            spread_radius=-3,
            color="#664B7BEC",
        )
        self.lighting_hex_copy_halo = ft.Container(
            content=self.lighting_hex_copy_button,
            width=44,
            height=44,
            alignment=ft.Alignment.CENTER,
            border_radius=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            shadow=copy_button_glow,
        )
        self.lighting_rgb_copy_halo = ft.Container(
            content=self.lighting_rgb_copy_button,
            width=44,
            height=44,
            alignment=ft.Alignment.CENTER,
            border_radius=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            shadow=copy_button_glow,
        )
        self.lighting_color_preview = ft.Container(
            width=44,
            height=44,
            bgcolor=rgb_to_hex(settings.color),
            border=ft.Border.all(2, ft.Colors.OUTLINE_VARIANT),
            border_radius=10,
            shadow=ft.BoxShadow(
                blur_radius=12,
                spread_radius=-3,
                color=self._lighting_preview_alpha(rgb_to_hex(settings.color), 150),
            ),
            tooltip="Нажмите, чтобы выбрать цвет мышью",
            ink=True,
            on_click=lambda _event: self._open_lighting_color_picker(),
        )
        self.lighting_color_value = ft.Text(
            rgb_to_hex(settings.color), size=12, weight=ft.FontWeight.W_600
        )
        # Keep this small value mirror for the picker/readback code.  The
        # editable HEX/RGB field below is now the visible value, so showing a
        # second copy here would only consume space.
        self.lighting_color_summary = ft.Column(
            [
                self.lighting_color_value,
                ft.Text(
                    "Включите кастомные цвета, чтобы выбрать цвет мышью",
                    size=10,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
            ],
            spacing=2,
        )
        # This is Womier's own NORMAL/DAZZLE flag, not the former
        # experimental two-colour wave.  In Dazzle mode the firmware controls
        # the palette, so the visual preview must not pretend the HEX swatch
        # is the colour currently shown by the keyboard.
        self.lighting_custom_color_switch = ft.Switch(value=not settings.rainbow)
        self.lighting_custom_color_mode_text = ft.Text(
            "Кастомные цвета: вкл" if not settings.rainbow else "Кастомные цвета: выкл",
            size=12,
            weight=ft.FontWeight.W_600,
        )

        def _update_custom_color_mode(event):
            enabled = bool(event.control.value)
            self.lighting_custom_color_mode_text.value = (
                "Кастомные цвета: вкл" if enabled else "Кастомные цвета: выкл"
            )
            try:
                self.lighting_custom_color_mode_text.update()
            except Exception:
                pass
            self._refresh_lighting_custom_color_swatch()
            self._refresh_lighting_effect_preview()

        self.lighting_custom_color_switch.on_change = _update_custom_color_mode
        self.lighting_custom_color_mode_row = ft.Row(
            # The colour cube comes first, then the Material switch.  It
            # makes the state scan left-to-right: colour -> enabled/disabled
            # -> explanation.  When disabled the cube becomes neutral.
            [
                self.lighting_color_preview,
                self.lighting_custom_color_switch,
                self.lighting_custom_color_mode_text,
            ],
            spacing=8,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.lighting_hex_colors_row = ft.Row(
            [self.lighting_primary_color, self.lighting_hex_copy_halo],
            spacing=8,
            width=254,
            wrap=False,
            # Align the 44 px IconButton's lower edge with the 56 px text
            # input rather than centring it vertically and making it look as
            # though it belongs to a second row.
            vertical_alignment=ft.CrossAxisAlignment.END,
        )
        self.lighting_rgb_fields_row = ft.Row(
            self.lighting_primary_rgb_fields,
            spacing=5,
            tight=True,
        )
        self.lighting_rgb_colors_row = ft.Row(
            [
                self.lighting_rgb_fields_row,
                self.lighting_rgb_copy_halo,
            ],
            # Do not wrap this row: the button belongs directly to the right
            # of R/G/B, exactly like the HEX row above it.
            width=254,
            spacing=8,
            wrap=False,
            vertical_alignment=ft.CrossAxisAlignment.END,
            visible=False,
        )
        self.lighting_color_header_row = ft.Row(
            # A single fixed grid preserves the requested order for either
            # representation: format -> value -> copy.  Both representations
            # use equal widths and alignment, so format switching never loses
            # the typed colour, moves the copy action, or drops RGB below the
            # format field.
            [
                self.lighting_color_format_dropdown,
                self.lighting_hex_colors_row,
                self.lighting_rgb_colors_row,
            ],
            spacing=10,
            width=444,
            wrap=False,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )

        def _switch_color_format(event):
            color_format = event.control.value or "hex"
            try:
                if color_format == "rgb":
                    color = parse_hex_color(self.lighting_primary_color.value)
                else:
                    color = tuple(
                        int((field.value or "").strip())
                        for field in self.lighting_primary_rgb_fields
                    )
                    color = parse_hex_color(rgb_to_hex(color))
                self._set_lighting_primary_color(color, update_controls=False)
            except (TypeError, ValueError, LightingProtocolError):
                pass
            # The selected representation is an editing preference, not a
            # second colour source.  Keep exactly one editable form mounted
            # at a time; both values stay synchronized for the picker and
            # clipboard helpers below.
            self._refresh_lighting_color_capability(update_controls=False)
            self._refresh_lighting_color_preview("primary", update_page=False)
            for control in (
                self.lighting_hex_colors_row,
                self.lighting_rgb_colors_row,
                self.lighting_color_summary,
            ):
                try:
                    control.update()
                except Exception:
                    pass

        self.lighting_color_format_dropdown.on_select = _switch_color_format
        self.lighting_primary_color.on_change = lambda e: self._refresh_lighting_color_preview("primary")
        for field in self.lighting_primary_rgb_fields:
            field.on_change = lambda e: self._refresh_lighting_color_preview("primary")

        self.lighting_brightness_label = ft.Text(f"Яркость: {settings.brightness + 1}/5", size=12)
        self.lighting_speed_label = ft.Text(f"Скорость: {settings.speed + 1}/5", size=12)

        def _update_brightness(event):
            self.lighting_brightness_label.value = f"Яркость: {int(event.control.value) + 1}/5"
            try:
                self.lighting_brightness_label.update()
            except Exception:
                pass
            self._refresh_lighting_effect_preview()

        def _update_speed(event):
            self.lighting_speed_label.value = f"Скорость: {int(event.control.value) + 1}/5"
            try:
                self.lighting_speed_label.update()
            except Exception:
                pass
            self._refresh_lighting_effect_preview()

        self.lighting_brightness_slider = ft.Slider(
            min=0, max=4, divisions=4, value=settings.brightness,
            width=225, on_change=_update_brightness,
        )
        self.lighting_speed_slider = ft.Slider(
            min=0, max=4, divisions=4, value=settings.speed,
            width=225, on_change=_update_speed,
        )
        self.lighting_status = ft.Text(
            "",
            size=11,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        apply_button = ft.FilledButton(
            "Применить",
            icon=ft.Icons.LIGHTBULB_ROUNDED,
            on_click=lambda e: self._lighting_apply_effect(),
            elevation=3,
        )
        self.lighting_apply_glow = ft.Container(
            content=apply_button,
            padding=1,
            border_radius=12,
            gradient=ft.LinearGradient(
                begin=ft.Alignment(-1, -1),
                end=ft.Alignment(1, 1),
                colors=[
                    self._lighting_preview_alpha(rgb_to_hex(settings.color), 90),
                    "#004B7BEC",
                ],
            ),
            shadow=ft.BoxShadow(
                blur_radius=14,
                spread_radius=-5,
                color=self._lighting_preview_alpha(rgb_to_hex(settings.color), 125),
            ),
        )
        # Use the formerly empty half of the wide desktop card for a compact
        # lighting preview.  It is intentionally a preview only: the HID
        # write still happens solely after the user presses "Применить".
        self.lighting_preview_color_chip = ft.Container(
            width=40,
            height=40,
            border_radius=12,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            bgcolor=rgb_to_hex(settings.color),
            shadow=ft.BoxShadow(
                blur_radius=12,
                spread_radius=-3,
                color=self._lighting_preview_alpha(rgb_to_hex(settings.color), 145),
            ),
        )
        self.lighting_preview_color_text = ft.Text(
            rgb_to_hex(settings.color), size=15, weight=ft.FontWeight.W_700
        )
        self.lighting_preview_effect_text = ft.Text(size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        self.lighting_preview_tiles = []

        def preview_key(width=32):
            tile = ft.Container(
                width=width,
                height=23,
                border_radius=6,
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            )
            self.lighting_preview_tiles.append(tile)
            return tile

        preview_keyboard = ft.Container(
            content=ft.Column(
                [
                    ft.Row([preview_key() for _ in range(10)], spacing=5, tight=True),
                    ft.Row([preview_key() for _ in range(9)], spacing=5, tight=True),
                    ft.Row([preview_key() for _ in range(7)] + [preview_key(68)], spacing=5, tight=True),
                    ft.Row([preview_key(48), preview_key(160), preview_key(48), preview_key(32)], spacing=5, tight=True),
                ],
                spacing=5,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=16,
            border_radius=16,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
            alignment=ft.Alignment.CENTER,
        )
        self.lighting_preview_hint = ft.Text(
            "Предпросмотр цвета и эффекта — клавиатура изменится только после «Применить».",
            size=10,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        self.lighting_preview_surface = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self.lighting_preview_color_chip,
                            ft.Column(
                                [self.lighting_preview_color_text, self.lighting_preview_effect_text], spacing=2),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    preview_keyboard,
                    self.lighting_preview_hint,
                ],
                spacing=14,
            ),
            padding=16,
            border_radius=16,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            height=282,
            shadow=ft.BoxShadow(
                blur_radius=16,
                spread_radius=-8,
                color=self._lighting_preview_alpha(rgb_to_hex(settings.color), 100),
            ),
        )
        self._refresh_lighting_custom_color_swatch(update=False)
        self._refresh_lighting_effect_preview(update_controls=False)
        self._refresh_lighting_color_capability(displayed_effect, update_controls=False)

        controls_column = ft.Column(
            [
                ft.Row(
                    [self.lighting_effect_dropdown, self.lighting_direction_dropdown],
                    spacing=10, wrap=True,
                ),
                self.lighting_color_header_row,
                self.lighting_custom_color_mode_row,
                ft.Row(
                    [
                        ft.Column([self.lighting_brightness_label, self.lighting_brightness_slider], spacing=0),
                        ft.Column([self.lighting_speed_label, self.lighting_speed_slider], spacing=0),
                    ],
                    spacing=12, wrap=True, vertical_alignment=ft.CrossAxisAlignment.END,
                ),
                ft.Row([self.lighting_apply_glow], spacing=10, wrap=True),
                self.lighting_status,
            ],
            spacing=10,
            tight=True,
        )
        content = ft.ResponsiveRow(
            [
                ft.Container(content=controls_column, col={"sm": 12, "md": 6}),
                ft.Container(content=self.lighting_preview_surface, col={"sm": 12, "md": 6}),
            ],
            spacing=16,
            run_spacing=16,
        )
        return self._card(
            icon=ft.Icons.COLORIZE_ROUNDED,
            title="Lighting Lab · SK75 TMR",
            subtitle="Эффект, цвет, яркость и скорость.",
            content=ft.Container(content, margin=ft.Margin.only(top=12)),
        )

    @staticmethod
    def _lighting_effect_has_firmware_palette(effect_code: int) -> bool:
        """Return true for effects whose colours are fixed by SK75 firmware.

        Local inspection of the installed official Womier SK75 layout shows
        that LightNeon (3) has no ``rgb`` or ``dazzle`` capability.  It is
        always a firmware-run rainbow effect, irrespective of spare RGB bytes
        carried by the generic HID packet.
        """
        return int(effect_code) == 3

    def _refresh_lighting_color_capability(self, effect_code=None, *, update_controls=True):
        """Show one colour editor, unless the firmware owns the palette.

        ``lighting_color_format_dropdown`` chooses the single editable
        representation: HEX *or* RGB.  A capability refresh used to make both
        rows visible again after changing an effect, which was confusing and
        could leave a stale hidden representation looking editable.
        """
        try:
            if effect_code is None:
                effect_code = int(self.lighting_effect_dropdown.value or 0)
            fixed_palette = self._lighting_effect_has_firmware_palette(int(effect_code))
        except (TypeError, ValueError):
            fixed_palette = False

        color_controls_visible = not fixed_palette
        color_format = getattr(
            getattr(self, "lighting_color_format_dropdown", None), "value", "hex"
        ) or "hex"
        show_rgb = color_controls_visible and color_format == "rgb"
        visibility = (
            (getattr(self, "lighting_color_header_row", None), color_controls_visible),
            (getattr(self, "lighting_custom_color_mode_row", None), color_controls_visible),
            (getattr(self, "lighting_hex_colors_row", None), color_controls_visible and not show_rgb),
            (getattr(self, "lighting_rgb_colors_row", None), show_rgb),
        )
        for control, visible in visibility:
            if control is None:
                continue
            control.visible = visible
            if update_controls:
                try:
                    control.update()
                except Exception:
                    pass

    def _lighting_settings_from_controls(self):
        try:
            effect = int(self.lighting_effect_dropdown.value or 0)
            custom_color_switch = getattr(self, "lighting_custom_color_switch", None)
            return LightingSettings(
                effect=effect,
                color=self._lighting_color_from_controls("primary"),
                brightness=int(self.lighting_brightness_slider.value),
                speed=int(self.lighting_speed_slider.value),
                option=int(self.lighting_direction_dropdown.value or 0),
                # NORMAL mode uses the selected RGB value.  DAZZLE is the
                # firmware's own multi-colour palette, so it intentionally
                # ignores that swatch just like the official Womier client.
                rainbow=(
                    not self._lighting_effect_has_firmware_palette(effect)
                    and not bool(getattr(custom_color_switch, "value", True))
                ),
            )
        except (TypeError, ValueError, LightingProtocolError) as exc:
            raise LightingProtocolError(f"Проверьте параметры подсветки: {exc}") from exc

    def _lighting_color_clipboard_text(self):
        """Return the selected Lighting Lab colour in its visible format."""
        color = self._lighting_color_from_controls("primary")
        if getattr(self.lighting_color_format_dropdown, "value", "hex") == "rgb":
            return f"rgb({color[0]}, {color[1]}, {color[2]})", "RGB"
        return rgb_to_hex(color), "HEX"

    def _copy_lighting_color(self, _event=None):
        """Copy the current HEX/RGB value using the reliable native path first."""
        try:
            text, label = self._lighting_color_clipboard_text()
        except LightingProtocolError as exc:
            self._snack(f"Не удалось скопировать цвет: {exc}")
            return

        if self._set_system_clipboard_text(text):
            self._snack(f"{label} скопирован: {text}")
            return

        clipboard = getattr(self, "clipboard", None)
        if clipboard is None:
            self._snack("Буфер обмена недоступен")
            return

        async def copy():
            try:
                await clipboard.set(text)
                copied = await clipboard.get()
                if copied != text:
                    raise RuntimeError("буфер вернул неполное значение")
                self._snack(f"{label} скопирован: {text}")
            except Exception as exc:
                logger.exception("lighting color clipboard write failed")
                self._snack(f"Не удалось скопировать цвет: {exc}")

        try:
            self.page.run_task(copy)
        except Exception as exc:
            logger.exception("lighting color clipboard task failed")
            self._snack(f"Не удалось скопировать цвет: {exc}")

    def _lighting_color_from_controls(self, which="primary"):
        if which != "primary":
            raise LightingProtocolError("неизвестный выбор цвета")
        if self.lighting_color_format_dropdown.value != "rgb":
            return parse_hex_color(self.lighting_primary_color.value)
        fields = self.lighting_primary_rgb_fields
        try:
            values = tuple(int((field.value or "").strip()) for field in fields)
        except (TypeError, ValueError) as exc:
            raise LightingProtocolError("RGB должен состоять из трёх чисел от 0 до 255") from exc
        # rgb_to_hex validates both the channel count and the 0–255 range.
        return parse_hex_color(rgb_to_hex(values))

    def _refresh_lighting_custom_color_swatch(self, *, update=True):
        """Paint the switch-adjacent colour tile only when custom colour is on."""
        preview = getattr(self, "lighting_color_preview", None)
        switch = getattr(self, "lighting_custom_color_switch", None)
        if preview is None:
            return
        enabled = bool(getattr(switch, "value", False))
        if enabled:
            try:
                hex_color = rgb_to_hex(self._lighting_color_from_controls("primary"))
            except LightingProtocolError:
                hex_color = "#000000"
            preview.bgcolor = hex_color
            preview.border = ft.Border.all(2, ft.Colors.OUTLINE_VARIANT)
            preview.shadow = ft.BoxShadow(
                blur_radius=12,
                spread_radius=-3,
                color=self._lighting_preview_alpha(hex_color, 150),
            )
            preview.tooltip = f"Выбранный цвет: {hex_color}. Нажмите, чтобы изменить."
            preview.ink = True
            preview.on_click = lambda _event: self._open_lighting_color_picker()
        else:
            preview.bgcolor = ft.Colors.SURFACE_CONTAINER_HIGHEST
            preview.border = ft.Border.all(1, ft.Colors.OUTLINE_VARIANT)
            preview.shadow = None
            preview.tooltip = "Кастомные цвета выключены"
            preview.ink = False
            preview.on_click = None
        if update:
            try:
                preview.update()
            except Exception:
                pass

    def _refresh_lighting_color_preview(self, which="primary", update_page=True):
        try:
            color = self._lighting_color_from_controls(which)
        except LightingProtocolError:
            return
        hex_color = rgb_to_hex(color)
        value = self.lighting_color_value
        if self.lighting_color_format_dropdown.value == "rgb":
            self.lighting_primary_color.value = hex_color
        else:
            for field, channel in zip(self.lighting_primary_rgb_fields, color):
                field.value = str(channel)
        value.value = hex_color
        self._refresh_lighting_custom_color_swatch(update=False)
        self._refresh_lighting_effect_preview(color=color, update_controls=update_page)
        if update_page:
            # The HEX/RGB field that the user is actively editing already
            # paints itself.  Patch only its small summary instead of the
            # whole workspace; this keeps opening and dragging the picker
            # responsive on a busy page.
            try:
                self.lighting_color_summary.update()
            except Exception:
                pass
            try:
                self.lighting_custom_color_mode_row.update()
            except Exception:
                pass
            apply_glow = getattr(self, "lighting_apply_glow", None)
            if apply_glow is not None:
                try:
                    apply_glow.update()
                except Exception:
                    pass

    @staticmethod
    def _lighting_preview_alpha(hex_color, alpha):
        """Return a #AARRGGBB shade accepted by Flet gradients/shadows."""
        return f"#{max(0, min(255, int(alpha))):02X}{str(hex_color).lstrip('#')}"

    @staticmethod
    def _lighting_firmware_palette():
        """Compact visual stand-in for Womier's DAZZLE/rainbow palette.

        The firmware animates the palette itself.  The preview deliberately
        uses several colours rather than falsely painting the user HEX swatch
        while the keyboard is in Womier's multi-colour mode.
        """
        return ("#FF5F7E", "#FFB84D", "#E8E85B", "#54DC8A", "#41C9FF", "#7D79FF", "#D975FF")

    def _refresh_lighting_effect_preview(self, color=None, *, update_controls=True):
        """Refresh the local, non-HID lighting preview in one small patch.

        This panel deliberately does not emulate all firmware animations.  It
        gives the otherwise unused desktop area a useful at-a-glance preview
        of the chosen colour, effect, brightness and speed, without falsely
        claiming that the keyboard has already been written to.
        """
        surface = getattr(self, "lighting_preview_surface", None)
        if surface is None:
            return
        if color is None:
            try:
                color = self._lighting_color_from_controls("primary")
            except LightingProtocolError:
                return
        try:
            hex_color = rgb_to_hex(color)
            effect_code = int(self.lighting_effect_dropdown.value or 0)
            brightness = int(self.lighting_brightness_slider.value or 0) + 1
            speed = int(self.lighting_speed_slider.value or 0) + 1
        except (TypeError, ValueError, LightingProtocolError):
            return

        custom_color_switch = getattr(self, "lighting_custom_color_switch", None)
        # ``rainbow`` is the official firmware DAZZLE setting.  The switch
        # means the inverse: enabled = use the user-selected RGB colour.
        # LightNeon is a separate official firmware effect which is always
        # multi-colour and does not expose either setting in Womier Driver.
        firmware_palette = (
            self._lighting_effect_has_firmware_palette(effect_code)
            or not bool(getattr(custom_color_switch, "value", True))
        )
        palette = self._lighting_firmware_palette()
        effect_name = EFFECTS.get(effect_code, "Эффект")
        if firmware_palette:
            start_color, end_color = palette[0], palette[-1]
            self.lighting_preview_color_chip.bgcolor = None
            self.lighting_preview_color_chip.gradient = ft.LinearGradient(
                begin=ft.Alignment(-1, -1),
                end=ft.Alignment(1, 1),
                colors=[palette[0], palette[2], palette[4], palette[6]],
            )
            self.lighting_preview_color_chip.shadow = ft.BoxShadow(
                blur_radius=12,
                spread_radius=-3,
                color=self._lighting_preview_alpha(palette[4], 145),
            )
            self.lighting_preview_color_text.value = "Палитра прошивки"
            if self._lighting_effect_has_firmware_palette(effect_code):
                self.lighting_preview_effect_text.value = (
                    f"{effect_name} · встроенный радужный эффект · яркость {brightness}/5 · скорость {speed}/5"
                )
                hint_text = "«Неон» — встроенный радужный эффект Womier; выбранный HEX для него не используется."
            else:
                self.lighting_preview_effect_text.value = (
                    f"{effect_name} · многоцветный режим · яркость {brightness}/5 · скорость {speed}/5"
                )
                hint_text = (
                    "Встроенная палитра Womier: выбранный HEX не управляет этим режимом до включения «Кастомные цвета»."
                )
        else:
            start_color = end_color = hex_color
            self.lighting_preview_color_chip.bgcolor = hex_color
            self.lighting_preview_color_chip.gradient = None
            self.lighting_preview_color_chip.shadow = ft.BoxShadow(
                blur_radius=12,
                spread_radius=-3,
                color=self._lighting_preview_alpha(hex_color, 145),
            )
            self.lighting_preview_color_text.value = hex_color
            self.lighting_preview_effect_text.value = (
                f"{effect_name} · яркость {brightness}/5 · скорость {speed}/5"
            )
            hint_text = "Предпросмотр цвета и эффекта — клавиатура изменится только после «Применить»."

        preview_hint = getattr(self, "lighting_preview_hint", None)
        if preview_hint is not None:
            preview_hint.value = hint_text
        surface.gradient = ft.LinearGradient(
            begin=ft.Alignment(-1, -1),
            end=ft.Alignment(1, 1),
            colors=[
                self._lighting_preview_alpha(start_color, 40),
                self._lighting_preview_alpha(end_color, 24),
                ft.Colors.SURFACE_CONTAINER_LOW,
            ],
        )
        surface.shadow = ft.BoxShadow(
            blur_radius=16,
            spread_radius=-8,
            color=self._lighting_preview_alpha(start_color, 100),
        )
        apply_glow = getattr(self, "lighting_apply_glow", None)
        if apply_glow is not None:
            apply_glow.gradient = ft.LinearGradient(
                begin=ft.Alignment(-1, -1),
                end=ft.Alignment(1, 1),
                colors=[self._lighting_preview_alpha(start_color, 90), self._lighting_preview_alpha(end_color, 90)],
            )
            apply_glow.shadow = ft.BoxShadow(
                blur_radius=14,
                spread_radius=-5,
                color=self._lighting_preview_alpha(start_color, 125),
            )
        for index, tile in enumerate(self.lighting_preview_tiles):
            tile_color = palette[index % len(palette)] if firmware_palette else hex_color
            tile.border = ft.Border.all(1, self._lighting_preview_alpha(tile_color, 145))
            tile.shadow = ft.BoxShadow(
                blur_radius=8,
                spread_radius=-2,
                color=self._lighting_preview_alpha(tile_color, 105),
            )
            tile.gradient = None
            tile.bgcolor = ft.Colors.SURFACE_CONTAINER_HIGHEST

        if update_controls:
            try:
                # A single parent patch is much cheaper than updating every
                # decorative key one by one.
                surface.update()
            except Exception:
                pass

    def _set_lighting_primary_color(self, color, *, update_controls=True):
        """Keep the picker, HEX input, RGB inputs and preview in sync."""
        color = parse_hex_color(rgb_to_hex(color))
        hex_color = rgb_to_hex(color)
        self.lighting_primary_color.value = hex_color
        for field, channel in zip(self.lighting_primary_rgb_fields, color):
            field.value = str(channel)
        self._refresh_lighting_color_preview("primary", update_page=False)
        if not update_controls:
            return
        for control in (
            self.lighting_primary_color,
            *self.lighting_primary_rgb_fields,
            getattr(self, "lighting_color_summary", self.lighting_color_preview),
            getattr(self, "lighting_custom_color_mode_row", None),
            getattr(self, "lighting_preview_surface", None),
        ):
            try:
                control.update()
            except Exception:
                # Controls are not attached while the page is still building.
                pass

    def _open_lighting_color_picker(self):
        """Open a native-Flet HSV picker for the ordinary Womier colour.

        Flet 0.85 does not ship a desktop ColorPicker control.  The picker is
        therefore made from ordinary gradients, a slider and a GestureDetector
        so it works offline and does not add another package to the app.
        Nothing is written to the keyboard from this dialog: **Применить** is
        still the explicit hardware action.
        """
        try:
            original_color = self._lighting_color_from_controls("primary")
        except LightingProtocolError:
            original_color = LightingSettings().color
        hue, saturation, value = rgb_to_hsv_degrees(original_color)
        state = {
            "hue": hue,
            "saturation": saturation,
            "value": value,
            "accepted": False,
            "closed": False,
            # The colour plane and its gradient are relatively expensive for
            # Flet to patch.  The slider/pointer can emit many more events
            # than the desktop client can draw, especially while the cursor
            # crosses the plane quickly.  Keep the native thumb moving, but
            # redraw this preview at a deliberately modest rate so a click on
            # "Отмена" is never left behind a long queue of colour patches.
            "last_visual_update": 0.0,
        }
        picker_width = 276
        picker_height = 184
        marker_size = 16

        sv_base = ft.Container(
            width=picker_width,
            height=picker_height,
            border_radius=14,
            gradient=ft.LinearGradient(
                begin=ft.Alignment(-1, -1),
                end=ft.Alignment(1, -1),
                colors=["#FFFFFFFF", rgb_to_hex(hsv_degrees_to_rgb(hue, 1, 1))],
            ),
        )
        sv_value_overlay = ft.Container(
            width=picker_width,
            height=picker_height,
            border_radius=14,
            gradient=ft.LinearGradient(
                begin=ft.Alignment(-1, -1),
                end=ft.Alignment(-1, 1),
                colors=["#00000000", "#FF000000"],
            ),
        )
        marker = ft.Container(
            width=marker_size,
            height=marker_size,
            border=ft.Border.all(2, ft.Colors.WHITE),
            border_radius=marker_size / 2,
            bgcolor="#22000000",
            shadow=ft.BoxShadow(blur_radius=3, color="#AA000000"),
        )
        selected_preview = ft.Container(
            width=46,
            height=46,
            border_radius=12,
            border=ft.Border.all(2, ft.Colors.OUTLINE_VARIANT),
        )
        selected_value = ft.Text(size=14, weight=ft.FontWeight.W_700)
        hue_value = ft.Text(size=11, color=ft.Colors.ON_SURFACE_VARIANT)
        hue_slider = ft.Slider(
            min=0,
            max=359,
            divisions=359,
            value=round(hue),
            width=picker_width,
        )

        def render_picker(*, force=False, initial=False):
            """Paint only the picker fragment, never the whole page.

            Setting ``hue_slider.value`` from its own ``on_change`` used to
            echo each pointer event back into Flet.  That feedback loop made
            the hue thumb feel as if it was catching on every drag.  The
            native slider now owns its thumb while dragging; the custom
            visual area is patched at most about 16 times per second and is
            forced once when the drag ends.  This is visually smooth for a
            colour picker while leaving the dialog actions immediately
            responsive on a busy desktop page.
            """
            if state["closed"]:
                return
            now = time.monotonic()
            if not initial and not force and now - state["last_visual_update"] < 1 / 16:
                return
            color = hsv_degrees_to_rgb(state["hue"], state["saturation"], state["value"])
            hue_color = rgb_to_hex(hsv_degrees_to_rgb(state["hue"], 1, 1))
            sv_base.gradient = ft.LinearGradient(
                begin=ft.Alignment(-1, -1),
                end=ft.Alignment(1, -1),
                colors=["#FFFFFFFF", hue_color],
            )
            marker.left = max(
                0,
                min(picker_width - marker_size, round(picker_width * state["saturation"] - marker_size / 2)),
            )
            marker.top = max(
                0,
                min(picker_height - marker_size, round(picker_height * (1 - state["value"]) - marker_size / 2)),
            )
            selected_preview.bgcolor = rgb_to_hex(color)
            selected_value.value = rgb_to_hex(color)
            hue_value.value = f"Оттенок: {round(state['hue']) % 360}°"
            state["last_visual_update"] = now
            if initial:
                hue_slider.value = round(state["hue"]) % 360
                hue_slider.active_color = hue_color
                return
            try:
                # ``picker_live_region`` contains the colour plane, marker
                # and labels.  One patch replaces the former series of six
                # control updates during every pointer move.
                picker_live_region.update()
            except Exception:
                pass
            if force:
                hue_slider.active_color = hue_color
                try:
                    hue_slider.update()
                except Exception:
                    pass

        def update_from_pointer(event, *, force=False):
            position = getattr(event, "local_position", None)
            if position is None:
                return
            try:
                saturation_value, brightness_value = picker_position_to_sv(
                    position.x, position.y, picker_width, picker_height
                )
            except (AttributeError, LightingProtocolError):
                return
            state["saturation"] = saturation_value
            state["value"] = brightness_value
            render_picker(force=force)

        def update_hue(event, *, force=False):
            try:
                state["hue"] = float(event.control.value)
            except (TypeError, ValueError):
                return
            render_picker(force=force)

        hue_slider.on_change = update_hue
        hue_slider.on_change_end = lambda event: update_hue(event, force=True)
        sv_plane = ft.GestureDetector(
            content=ft.Stack(
                [sv_base, sv_value_overlay, marker],
                width=picker_width,
                height=picker_height,
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
            ),
            width=picker_width,
            height=picker_height,
            # Flet coalesces the raw mouse stream before it reaches Python;
            # render_picker coalesces the resulting visual patches as well.
            # Throttle at the source too.  It is important not to send a
            # backlog of tiny drag events from Flutter to Python: stale events
            # can otherwise delay the Cancel/Done button on slower PCs.
            drag_interval=72,
            on_tap_down=lambda event: update_from_pointer(event, force=True),
            on_pan_start=lambda event: update_from_pointer(event, force=True),
            on_pan_update=update_from_pointer,
            on_pan_end=lambda _event: render_picker(force=True),
        )

        picker_live_region = ft.Column(
            [
                ft.Row(
                    [
                        selected_preview,
                        ft.Column(
                            [
                                selected_value,
                                ft.Text(
                                    "Нажмите или проведите мышью по полю",
                                    size=11,
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                ),
                            ],
                            spacing=2,
                        ),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                sv_plane,
                hue_value,
            ],
            spacing=10,
            tight=True,
        )

        def retire_picker_callbacks():
            """Make late queued pointer events into no-ops before dismissal."""
            state["closed"] = True
            # These assignments are intentionally local.  ``state['closed']``
            # is the main guard for events already in flight; unhooking the
            # Python callbacks also prevents a second local redraw while the
            # native close animation is still finishing.
            hue_slider.on_change = None
            hue_slider.on_change_end = None
            sv_plane.on_tap_down = None
            sv_plane.on_pan_start = None
            sv_plane.on_pan_update = None
            sv_plane.on_pan_end = None

        def close_picker(accepted=False):
            if state["closed"]:
                return
            retire_picker_callbacks()
            state["accepted"] = bool(accepted)
            if accepted:
                # The editor behind the modal receives one final update only
                # after confirmation.  Selecting a colour never writes HID;
                # the separate "Применить" button remains the hardware step.
                self._set_lighting_primary_color(
                    hsv_degrees_to_rgb(state["hue"], state["saturation"], state["value"])
                )
            if getattr(self, "_lighting_color_picker_close", None) is close_picker:
                self._lighting_color_picker_close = None
            self.page.pop_dialog()

        def dismissed(_event):
            if not state["closed"]:
                retire_picker_callbacks()
                # All live changes were local to the dialog, so cancellation
                # has nothing to restore in the parent controls.
            if getattr(self, "_lighting_color_picker_close", None) is close_picker:
                self._lighting_color_picker_close = None

        render_picker(initial=True)
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Выбор цвета"),
            content=ft.Container(
                content=ft.Column(
                    [
                        picker_live_region,
                        hue_slider,
                    ],
                    spacing=10,
                    tight=True,
                ),
                width=picker_width + 24,
            ),
            actions=[
                ft.TextButton("Отмена", on_click=lambda _event: close_picker(False)),
                ft.FilledButton("Готово", on_click=lambda _event: close_picker(True)),
            ],
            on_dismiss=dismissed,
            shape=ft.RoundedRectangleBorder(radius=20),
        )
        # A rapid double-click on the colour chip used to be able to leave a
        # modal picker behind the new one.  Retire a previous picker first;
        # it has no unsaved hardware changes, so this is equivalent to
        # cancelling it and keeps the dialog stack shallow.
        previous_close = getattr(self, "_lighting_color_picker_close", None)
        if callable(previous_close):
            previous_close(False)
        self._lighting_color_picker_close = close_picker
        self.page.show_dialog(dialog)

    def _save_lighting_lab_settings(self, settings):
        """Persist a verified lighting reply without disturbing live input.

        Lighting reads and writes complete on a short HID worker.  The old
        implementation assigned into ``entry`` just before calling
        ``save_config()``; that small unprotected gap was enough for a config
        export or a magnetic timer to serialise the same nested device mapping
        while it was being resized.  Keep the mutation and its detached save
        in the common configuration transaction instead.

        This is device-only state, so reloading the foreground profile runtime
        here is both unnecessary and harmful: it can call
        ``keyboard.unhook_all()`` after a colour change.  Magnetic/background
        saves already use the same no-reload path.
        """
        if not isinstance(settings, LightingSettings):
            raise LightingProtocolError("некорректные параметры подсветки")
        with _CONFIG_WRITE_LOCK:
            entry = self._active_device()
            if entry is None:
                raise LightingProtocolError("сначала выберите клавиатуру")
            entry["lighting_lab"] = settings.to_config()
            # ``_CONFIG_WRITE_LOCK`` is re-entrant, so this retains one
            # atomic snapshot/write boundary without serialising any HID I/O.
            self.save_config(reload_runtime=False)

    def _set_lighting_status(self, text, color=None):
        def update():
            try:
                self.lighting_status.value = text
                self.lighting_status.color = color or ft.Colors.ON_SURFACE_VARIANT
                # The lighting worker can finish while Magnetic Lab is
                # changing RT visibility.  A fixed status leaf never needs a
                # full page patch, which avoids an unrelated background read
                # joining that mutable selected-key reconciliation.
                self.lighting_status.update()
            except Exception:
                pass
        self._ui_call(update)

    def _send_lighting_packets(self, packets, label, inter_packet_delay=0.0):
        """Write one or more lighting HID packets atomically."""
        packets = [list(packet) for packet in packets]
        if not packets:
            return True
        if any(len(packet) != RongyuanLightingProtocol.REPORT_SIZE for packet in packets):
            raise LightingProtocolError("HID lighting packet must be 64 bytes")

        with self.usb_lock:
            return self._send_lighting_packets_locked(packets, label, inter_packet_delay)

    def _send_lighting_packets_locked(self, packets, label, inter_packet_delay=0.0):
        """Send packets through one HID handle. Caller holds ``usb_lock``."""
        paths = self.get_keyboard_paths()
        if not paths:
            raise LightingProtocolError("клавиатура не найдена среди HID-интерфейсов")

        last_error = None
        for path in paths:
            device = None
            try:
                device = hid.device()
                device.open_path(path)
                device.set_nonblocking(1)
                for index, packet in enumerate(packets):
                    sent = device.send_feature_report([0] + packet)
                    if sent is None or sent <= 0:
                        raise OSError(f"send_feature_report returned {sent} at packet {index + 1}")
                    if inter_packet_delay and index + 1 < len(packets):
                        time.sleep(inter_packet_delay)
                entry = self._active_device() or {}
                cache_key = self._device_key(
                    entry.get("vid", 0), entry.get("pid", 0), entry.get("usage_page", 0)
                )
                self._working_hid_path[cache_key] = path
                logger.debug("lighting [%s] sent %d packet(s) on %s", label, len(packets), path)
                return True
            except Exception as exc:
                last_error = exc
                logger.debug("lighting [%s] failed on %s: %s", label, path, exc)
            finally:
                try:
                    if device is not None:
                        device.close()
                except Exception:
                    pass
        raise LightingProtocolError(f"не удалось отправить HID-команду: {last_error}")

    def _query_lighting_settings(self):
        """Read the effective Womier lighting state without changing it."""
        with self.usb_lock:
            return self._query_lighting_settings_locked()

    def _query_lighting_settings_locked(self):
        """Read ``GET_LEDPARAM`` while the caller owns ``usb_lock``.

        A successful feature-report write only says that Windows accepted the
        packet.  The readback is what lets Lighting Lab distinguish a selected
        HEX colour from the firmware's actual multi-colour/Dazzle mode.
        """
        packet = RongyuanLightingProtocol.get_settings_packet()
        paths = self.get_keyboard_paths()
        if not paths:
            raise LightingProtocolError("клавиатура не найдена среди HID-интерфейсов")

        last_error = None
        for path in paths:
            device = None
            try:
                device = hid.device()
                device.open_path(path)
                device.set_nonblocking(0)
                sent = device.send_feature_report([0] + packet)
                if sent is None or sent <= 0:
                    raise OSError(f"send_feature_report returned {sent}")
                # The official Womier app yields briefly after a feature
                # request too; without it some firmware revisions return the
                # previous packet.
                time.sleep(0.04)
                response = list(device.get_feature_report(0, 65))
                settings = RongyuanLightingProtocol.decode_settings(response)
                entry = self._active_device() or {}
                cache_key = self._device_key(
                    entry.get("vid", 0), entry.get("pid", 0), entry.get("usage_page", 0)
                )
                self._working_hid_path[cache_key] = path
                logger.debug("lighting readback on %s: %s", path, settings)
                return settings
            except Exception as exc:
                last_error = exc
                logger.debug("lighting readback failed on %s: %s", path, exc)
            finally:
                try:
                    if device is not None:
                        device.close()
                except Exception:
                    pass
        raise LightingProtocolError(f"не удалось прочитать подсветку клавиатуры: {last_error}")

    def _sync_lighting_controls_from_settings(self, settings):
        """Reflect a verified firmware answer in the mounted Lighting Lab."""
        if not isinstance(settings, LightingSettings):
            return

        def update():
            effect_dropdown = getattr(self, "lighting_effect_dropdown", None)
            direction_dropdown = getattr(self, "lighting_direction_dropdown", None)
            if effect_dropdown is None or direction_dropdown is None:
                return
            effect_dropdown.value = str(settings.effect)
            labels = EFFECT_OPTIONS.get(settings.effect, ("По умолчанию",))
            direction_dropdown.options = [
                ft.dropdown.Option(key=str(index), text=label)
                for index, label in enumerate(labels)
            ]
            direction_dropdown.value = str(min(max(0, settings.option), len(labels) - 1))
            self._set_lighting_primary_color(settings.color, update_controls=False)
            brightness_slider = getattr(self, "lighting_brightness_slider", None)
            speed_slider = getattr(self, "lighting_speed_slider", None)
            if brightness_slider is not None:
                brightness_slider.value = settings.brightness
            if speed_slider is not None:
                speed_slider.value = settings.speed
            custom_switch = getattr(self, "lighting_custom_color_switch", None)
            if custom_switch is not None:
                custom_switch.value = not settings.rainbow
            mode_text = getattr(self, "lighting_custom_color_mode_text", None)
            if mode_text is not None:
                mode_text.value = (
                    "Кастомные цвета: вкл" if not settings.rainbow else "Кастомные цвета: выкл"
                )
            brightness_label = getattr(self, "lighting_brightness_label", None)
            if brightness_label is not None:
                brightness_label.value = f"Яркость: {settings.brightness + 1}/5"
            speed_label = getattr(self, "lighting_speed_label", None)
            if speed_label is not None:
                speed_label.value = f"Скорость: {settings.speed + 1}/5"
            self._refresh_lighting_color_capability(settings.effect, update_controls=False)
            self._refresh_lighting_effect_preview(update_controls=False)
            for control in (
                effect_dropdown,
                direction_dropdown,
                brightness_slider,
                speed_slider,
                custom_switch,
                mode_text,
                brightness_label,
                speed_label,
                getattr(self, "lighting_color_summary", None),
                getattr(self, "lighting_color_header_row", None),
                getattr(self, "lighting_custom_color_mode_row", None),
                getattr(self, "lighting_hex_colors_row", None),
                getattr(self, "lighting_rgb_colors_row", None),
                getattr(self, "lighting_preview_surface", None),
            ):
                try:
                    if control is not None:
                        control.update()
                except Exception:
                    pass

        self._ui_call(update)

    def _read_lighting_settings_from_keyboard(self, *, silent=True):
        """Synchronize local preview/config from a read-only firmware query."""
        def worker():
            try:
                settings = self._query_lighting_settings()
                self._save_lighting_lab_settings(settings)
                self._sync_lighting_controls_from_settings(settings)
                if not silent:
                    self._set_lighting_status("Подсветка считана с клавиатуры.", ft.Colors.GREEN_300)
            except LightingProtocolError as exc:
                if not silent:
                    self._set_lighting_status(str(exc), ft.Colors.ERROR)

        threading.Thread(target=worker, daemon=True, name="lighting-readback").start()

    def _lighting_apply_effect(self):
        try:
            settings = self._lighting_settings_from_controls()
        except LightingProtocolError as exc:
            self._set_lighting_status(str(exc), ft.Colors.ERROR)
            return
        def worker():
            try:
                self._send_lighting_packets(
                    [RongyuanLightingProtocol.settings_packet(settings)], "lighting_effect"
                )
                # A write accepted by HID is not proof that the firmware has
                # switched modes.  Query it back so the right preview always
                # reflects the keyboard (especially Neon/Dazzle palette).
                try:
                    time.sleep(0.06)
                    actual = self._query_lighting_settings()
                except LightingProtocolError as read_error:
                    self._save_lighting_lab_settings(settings)
                    self._set_lighting_status(
                        f"Команда отправлена, но подтверждение не прочитано: {read_error}",
                        ft.Colors.AMBER_300,
                    )
                    return

                self._save_lighting_lab_settings(actual)
                self._sync_lighting_controls_from_settings(actual)
                if actual == settings:
                    self._set_lighting_status("Эффект применён и подтверждён клавиатурой.", ft.Colors.GREEN_300)
                else:
                    self._set_lighting_status(
                        "Клавиатура вернула другие параметры — в предпросмотре показано её фактическое состояние.",
                        ft.Colors.AMBER_300,
                    )
            except LightingProtocolError as exc:
                self._set_lighting_status(str(exc), ft.Colors.ERROR)

        threading.Thread(target=worker, daemon=True, name="lighting-effect").start()

    # ---------- Magnetic Lab · SK75 TMR ----------
    def _build_magnetic_lab_card(self):
        """Build targeted magnetic-switch controls for the connected SK75 TMR."""
        # There is deliberately no implicit target.  Seeding the controls from
        # Q made the page look editable before the user had selected a key and
        # could accidentally write that key.  Keep both logical and visual
        # selection empty until the first explicit click on the layout.
        self.magnetic_selected_slot = None
        self.magnetic_visual_selected_slot = None
        # A Snap pair is intentionally blank until the user picks both sides
        # in its visual selector.  Pre-filling arbitrary A/D keys made a
        # right-click capable of changing the keyboard before a deliberate
        # first-key choice.
        self.snap_first_slot = None
        self.snap_second_slot = None
        self._magnetic_write_lock = threading.Lock()
        self._magnetic_write_timers = {}
        self._magnetic_write_revisions = {}
        # The timer is only a debounce mechanism; retain the latest intent
        # separately so closing to the tray cannot silently throw it away.
        # An entry is claimed either by its timer or by the small final-drain
        # worker.  That ownership marker prevents a race from writing one key
        # twice when the user hides the window exactly as a timer fires.
        self._magnetic_pending_key_writes = {}
        self._magnetic_inflight_key_writes = {}
        self._magnetic_options_timer = None
        self._magnetic_options_revision = 0
        self._magnetic_pending_options_write = None
        self._magnetic_options_inflight = None
        # Magnetic presets live in the app because the SK75 settings protocol
        # has no profile byte.  A selection may therefore require a batch of
        # normal per-key writes.  Keep one cancellable request so a quick
        # 1 → 2 → 3 selection can never apply an already abandoned preset.
        self._magnetic_profile_switch_lock = threading.Lock()
        self._magnetic_profile_switch_timer = None
        self._magnetic_profile_switch_revision = 0
        # The official Womier application keeps a separate Chromium cache.
        # Coalesce successful HID writes before mirroring them there: one
        # slider gesture must not create one 230 KB Local Storage WAL update
        # per intermediate Flet event.  The helper itself refuses to write
        # while Womier is open and retries only that safe deferred case.
        self._womier_cache_sync_lock = threading.Lock()
        self._womier_cache_sync_pending = {}
        self._womier_cache_sync_timer = None
        self._womier_cache_sync_revision = 0
        # HID writes update the live magnetic cache immediately, while the
        # larger JSON/LevelDB persistence work is coalesced after a brief
        # quiet period.  This state is kept separate from the HID debounce:
        # a successful write is never delayed, only redundant disk work is.
        self._magnetic_persistence_lock = threading.RLock()
        self._magnetic_persistence_timer = None
        self._magnetic_persistence_revision = 0
        self._magnetic_persistence_pending = False
        self._restore_persisted_womier_cache_sync()
        self.keyboard_picker_hint = ft.Text(
            "Выберите клавишу на раскладке — её выделение покажет текущую цель настройки.",
            size=12,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        self.keyboard_picker_root = ft.Container(
            alignment=ft.Alignment.CENTER,
            padding=8,
        )
        self.magnetic_key_metrics_explanation = ft.Text(
            MAGNETIC_KEY_METRICS_EXPLANATION,
            size=10,
            color=ft.Colors.ON_SURFACE_VARIANT,
            selectable=False,
        )
        # ``page.width`` can be zero during the first native frame.  Preserve
        # it when available and use the conservative 1360 px fallback until
        # the first resize notification provides the true viewport width.
        self._sk75_viewport_width = getattr(self.page, "width", None)
        self._sk75_rendered_deck_width = None
        active_entry = self._active_device() or {}
        try:
            self.magnetic_profile_index = int(active_entry.get("magnetic_selected_profile", 0))
        except (TypeError, ValueError):
            self.magnetic_profile_index = 0
        self.magnetic_profile_index = max(
            0, min(MAGNETIC_PROFILE_COUNT - 1, self.magnetic_profile_index)
        )
        self.magnetic_profile_dropdown = self._app_dropdown(
            label="Набор магнитных настроек",
            value=str(self.magnetic_profile_index),
            options=[
                ft.dropdown.Option(key=str(index), text=self._magnetic_profile_label(index))
                for index in range(MAGNETIC_PROFILE_COUNT)
            ],
            width=250,
            border_radius=12,
            filled=True,
            on_select=self._on_magnetic_profile_changed,
        )
        self.magnetic_profile_note = ft.Text(
            "При выборе набора параметры с текущего экрана сохраняются, а выбранные применяются к клавиатуре.",
            size=11,
            color=ft.Colors.ON_SURFACE_VARIANT,
            max_lines=2,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        # The switch is deliberately label-less.  Its M3 surface below owns
        # the typography, which keeps the interaction target compact and
        # gives the ON/OFF states a stable layout instead of a loose switch
        # floating above the parameter controls.
        self.magnetic_rt_switch = ft.Switch(
            value=True,
            active_color=ft.Colors.ON_PRIMARY,
            active_track_color=ft.Colors.PRIMARY,
            inactive_thumb_color=ft.Colors.OUTLINE,
            inactive_track_color=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            track_outline_color=ft.Colors.OUTLINE,
            overlay_color=ft.Colors.with_opacity(0.12, ft.Colors.PRIMARY),
            on_change=lambda e: self._on_magnetic_rt_changed(),
        )
        # The release threshold is the ordinary Rapid Trigger setting.  A
        # separate downstroke threshold is an optional per-key refinement,
        # just like the extra setting in Womier's driver.  Existing asymmetric
        # keyboard values are preserved and reveal that extra control.
        self.magnetic_rt_separate_switch = ft.Switch(
            value=False,
            active_color=ft.Colors.ON_SECONDARY,
            active_track_color=ft.Colors.SECONDARY,
            inactive_thumb_color=ft.Colors.OUTLINE,
            inactive_track_color=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            track_outline_color=ft.Colors.OUTLINE,
            overlay_color=ft.Colors.with_opacity(0.12, ft.Colors.SECONDARY),
            on_change=lambda e: self._on_magnetic_rt_separation_changed(),
        )
        # ``liftTravel`` (operation 1) is the firmware's ordinary
        # deactivation threshold.  It is independent only when this switch is
        # enabled; with it off we intentionally keep it equal to actuation.
        # This mirrors the existing separate-RT behaviour without inventing a
        # second firmware setting.
        self.magnetic_deactivation_separate_switch = ft.Switch(
            value=False,
            active_color=ft.Colors.ON_TERTIARY,
            active_track_color=ft.Colors.TERTIARY,
            inactive_thumb_color=ft.Colors.OUTLINE,
            inactive_track_color=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            track_outline_color=ft.Colors.OUTLINE,
            overlay_color=ft.Colors.with_opacity(0.12, ft.Colors.TERTIARY),
            on_change=lambda e: self._on_magnetic_deactivation_separation_changed(),
        )

        # Keep the proven compact travel-test rulers.  The device state and
        # callbacks beneath them are unchanged; this restores the less busy
        # visual layout from before the experimental M3-card / combined-meter
        # redesign.  Each heading still has a small coloured role icon so the
        # three values match the corners of the keyboard preview.
        self.magnetic_actuation_label, self.magnetic_actuation_slider, self.magnetic_actuation_control = self._make_vertical_magnetic_control(
            MAGNETIC_SCALE_ROLE_LABELS["actuation"], 150, 10, 330, 320,
            MAGNETIC_METRIC_COLORS["actuation"],
            fills_from_top=True,
            icon=ft.Icons.ADS_CLICK_ROUNDED,
        )
        self.magnetic_rt_release_label, self.magnetic_rt_release_slider, self.magnetic_rt_release_control = self._make_vertical_magnetic_control(
            MAGNETIC_SCALE_ROLE_LABELS["rapid_release"], 20, 1, 200, 199,
            MAGNETIC_METRIC_COLORS["rapid_release"],
            fills_from_top=True,
            icon=ft.Icons.KEYBOARD_RETURN_ROUNDED,
        )
        self.magnetic_rt_press_label, self.magnetic_rt_press_slider, self.magnetic_rt_press_control = self._make_vertical_magnetic_control(
            MAGNETIC_SCALE_ROLE_LABELS["rapid_press"], 15, 1, 200, 199,
            MAGNETIC_METRIC_COLORS["rapid_press"],
            fills_from_top=True,
            icon=ft.Icons.KEYBOARD_DOUBLE_ARROW_DOWN_ROUNDED,
        )
        self.magnetic_rt_press_control.visible = False
        self.magnetic_lower_dead_zone_label, self.magnetic_lower_dead_zone_slider, self.magnetic_lower_dead_zone_control = self._make_vertical_magnetic_control(
            MAGNETIC_SCALE_ROLE_LABELS["lower_dead_zone"], 5, 0, 100, 100,
            "#FFA629",
            fills_from_top=False,
            icon=ft.Icons.VERTICAL_ALIGN_BOTTOM_ROUNDED,
        )
        self.magnetic_upper_dead_zone_label, self.magnetic_upper_dead_zone_slider, self.magnetic_upper_dead_zone_control = self._make_vertical_magnetic_control(
            MAGNETIC_SCALE_ROLE_LABELS["upper_dead_zone"], 10, 0, 100, 100,
            "#FF62B0",
            fills_from_top=True,
            icon=ft.Icons.VERTICAL_ALIGN_TOP_ROUNDED,
        )
        # Normal mode has its own firmware operation for deactivation.  Its
        # write/read/cache handling lives beside the protocol code; the UI
        # owns a distinct ruler instead of relabelling an RT threshold so the
        # user never edits the wrong setting when Rapid Trigger is off.
        self.magnetic_deactivation_label, self.magnetic_deactivation_slider, self.magnetic_deactivation_control = self._make_vertical_magnetic_control(
            "Точка\nдеактивации", 30, 10, 330, 320,
            MAGNETIC_METRIC_COLORS["rapid_release"],
            fills_from_top=True,
            icon=ft.Icons.KEYBOARD_RETURN_ROUNDED,
        )
        self.magnetic_deactivation_control.visible = False

        cached_options = self._cached_magnetic_keyboard_options()
        self._magnetic_keyboard_options_cache = cached_options
        self.magnetic_rt_stab_dropdown = self._app_dropdown(
            label="RTStab",
            value=str(cached_options.rt_stab),
            options=[
                ft.dropdown.Option(key=str(value), text=f"{value}%")
                for value in (0, 25, 50, 75, 100, 125)
            ],
            width=150,
            border_radius=12,
            filled=True,
            on_select=lambda e: self._schedule_magnetic_options_write(),
        )
        self.magnetic_anti_accidental_switch = ft.Switch(
            value=cached_options.anti_accidental,
            tooltip="Включить или выключить защиту от случайных нажатий",
            on_change=lambda e: self._schedule_magnetic_options_write(),
        )

        self.magnetic_status = ft.Text(size=11, color=ft.Colors.ON_SURFACE_VARIANT)
        # Snap Key selection is intentionally a dialog-only draft.  Keeping a
        # tiny summary beside the per-key scales makes the action discoverable
        # without leaving yellow "pending" markers on the main keyboard.
        self.magnetic_snap_summary = ft.Text(
            size=10,
            color=ft.Colors.ON_SURFACE_VARIANT,
            max_lines=2,
            overflow=ft.TextOverflow.ELLIPSIS,
        )

        # These four controls are one global-settings band.  Sharing a fixed
        # height keeps the profile, RTStab, protection and Snap Key cards
        # perfectly level despite their different amount of copy.
        magnetic_header_card_height = 154
        magnetic_header_copy_height = 34
        magnetic_header_control_height = 52

        def magnetic_header_title(icon, title, color):
            return ft.Row(
                [
                    ft.Icon(icon, size=16, color=color),
                    ft.Text(
                        title,
                        size=12,
                        weight=ft.FontWeight.W_600,
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                ],
                spacing=6,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )

        def magnetic_header_body(icon, title, color, copy, control):
            """Pin every global magnetic setting to the same three baselines.

            The profile field used to appear above its note while the other
            cards put their control below the note.  It made the row feel
            crooked even though all four outer cards shared a height.  Fixed
            copy/control slots leave the titles, explanation text and actual
            controls on the same horizontal lines.
            """
            return ft.Column(
                [
                    ft.Container(
                        content=magnetic_header_title(icon, title, color),
                        height=20,
                        alignment=ft.Alignment.CENTER_LEFT,
                    ),
                    ft.Container(
                        content=copy,
                        height=magnetic_header_copy_height,
                        alignment=ft.Alignment.TOP_LEFT,
                    ),
                    ft.Container(
                        content=control,
                        height=magnetic_header_control_height,
                        alignment=ft.Alignment.CENTER_LEFT,
                    ),
                ],
                spacing=0,
                expand=True,
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            )

        snap_key_button = ft.FilledTonalButton(
            "Настроить пару",
            icon=ft.Icons.TUNE_ROUNDED,
            height=40,
            on_click=lambda e: self._open_snap_key_dialog(),
        )

        profile_card = ft.Container(
            content=magnetic_header_body(
                ft.Icons.LAYERS_ROUNDED,
                "Профиль",
                ft.Colors.PRIMARY,
                self.magnetic_profile_note,
                self.magnetic_profile_dropdown,
            ),
            # Together with the three neighbouring cards this exactly fills
            # the 1128 px keyboard deck (including the three 12 px gaps).
            # It keeps global controls in one compact, easy-to-scan band.
            width=336,
            height=magnetic_header_card_height,
            padding=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=14,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        )
        # These are keyboard-wide options, so placing them beside the preset
        # selector makes their scope clear and avoids a second, oversized
        # "general settings" block below the key controls.
        rt_stab_card = ft.Container(
            content=magnetic_header_body(
                ft.Icons.GRAPHIC_EQ_ROUNDED,
                "RTStab",
                ft.Colors.TERTIARY,
                ft.Text(
                    "Стабилизация деактивации Rapid Trigger.",
                    size=10,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                    max_lines=2,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                self.magnetic_rt_stab_dropdown,
            ),
            width=210,
            height=magnetic_header_card_height,
            padding=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=14,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        )
        anti_accidental_card = ft.Container(
            content=magnetic_header_body(
                ft.Icons.SHIELD_OUTLINED,
                "Защита от случайных нажатий",
                ft.Colors.SECONDARY,
                ft.Text(
                    "Фильтр активации клавиши, не RTStab.",
                    size=10,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                    max_lines=2,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                self.magnetic_anti_accidental_switch,
            ),
            width=270,
            height=magnetic_header_card_height,
            padding=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=14,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        )
        snap_key_card = ft.Container(
            content=magnetic_header_body(
                ft.Icons.LINK_ROUNDED,
                "Snap Key",
                ft.Colors.TERTIARY,
                self.magnetic_snap_summary,
                snap_key_button,
            ),
            width=276,
            height=magnetic_header_card_height,
            padding=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=14,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        )

        # Persistent M3 mode header.  The controls below are never recreated
        # while switching RT: their labels and visibility change in place,
        # preserving selected-key values and avoiding a complete page redraw.
        self.magnetic_parameter_mode_title = ft.Text(
            "Rapid Trigger",
            size=16,
            weight=ft.FontWeight.W_600,
        )
        self.magnetic_parameter_mode_description = ft.Text(
            size=11,
            color=ft.Colors.ON_SURFACE_VARIANT,
            max_lines=2,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        self.magnetic_parameter_mode_badge_text = ft.Text(
            size=10,
            weight=ft.FontWeight.W_700,
        )
        self.magnetic_parameter_mode_badge = ft.Container(
            content=self.magnetic_parameter_mode_badge_text,
            padding=ft.Padding.symmetric(horizontal=10, vertical=5),
            border_radius=99,
        )
        self.magnetic_parameter_mode_surface = ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        content=ft.Icon(ft.Icons.BOLT_ROUNDED, size=21, color=ft.Colors.PRIMARY),
                        width=42,
                        height=42,
                        alignment=ft.Alignment.CENTER,
                        bgcolor=ft.Colors.PRIMARY_CONTAINER,
                        border_radius=14,
                    ),
                    ft.Column(
                        [
                            ft.Row(
                                [self.magnetic_parameter_mode_title, self.magnetic_parameter_mode_badge],
                                spacing=8,
                                tight=True,
                            ),
                            self.magnetic_parameter_mode_description,
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    self.magnetic_rt_switch,
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=548,
            height=76,
            padding=16,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=20,
            animate=ft.Animation(160, ft.AnimationCurve.EASE_OUT),
        )
        self.magnetic_rt_separate_surface = ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        content=ft.Icon(ft.Icons.KEYBOARD_DOUBLE_ARROW_DOWN_ROUNDED, size=18, color=ft.Colors.SECONDARY),
                        width=34,
                        height=34,
                        alignment=ft.Alignment.CENTER,
                        bgcolor=ft.Colors.SECONDARY_CONTAINER,
                        border_radius=11,
                    ),
                    ft.Column(
                        [
                            ft.Text("Отдельный RT при повторном нажатии вниз", size=12, weight=ft.FontWeight.W_600),
                            ft.Text(
                                "Показывает дополнительный независимый порог нажатия.",
                                size=10,
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                        ],
                        spacing=1,
                        expand=True,
                    ),
                    self.magnetic_rt_separate_switch,
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=548,
            height=76,
            padding=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=18,
            animate=ft.Animation(160, ft.AnimationCurve.EASE_OUT),
            animate_opacity=ft.Animation(160, ft.AnimationCurve.EASE_OUT),
        )
        self.magnetic_deactivation_separate_surface = ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        content=ft.Icon(
                            ft.Icons.KEYBOARD_RETURN_ROUNDED,
                            size=18,
                            color=ft.Colors.TERTIARY,
                        ),
                        width=34,
                        height=34,
                        alignment=ft.Alignment.CENTER,
                        bgcolor=ft.Colors.TERTIARY_CONTAINER,
                        border_radius=11,
                    ),
                    ft.Column(
                        [
                            ft.Text(
                                "Отдельная точка деактивации",
                                size=12,
                                weight=ft.FontWeight.W_600,
                            ),
                            ft.Text(
                                "Позволяет задать независимый порог отпускания.",
                                size=10,
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                        ],
                        spacing=1,
                        expand=True,
                    ),
                    self.magnetic_deactivation_separate_switch,
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=548,
            height=76,
            padding=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=18,
            visible=False,
            animate=ft.Animation(160, ft.AnimationCurve.EASE_OUT),
            animate_opacity=ft.Animation(160, ft.AnimationCurve.EASE_OUT),
        )
        # The parameter rail is deliberately split into two permanent groups.
        # The left group contains thresholds whose visibility is mode-specific;
        # the right group contains the two physical dead zones.  Keeping a
        # flexible, always-present anchor between them means that enabling an
        # extra RT/deactivation threshold can never make the dead zones walk
        # left or right.  It also keeps the actual Flet control tree stable:
        # rapid switches only toggle ``visible`` on existing children.
        self.magnetic_dead_zone_spacer = ft.Container(
            expand=True,
            height=1,
            opacity=0.0,
            ignore_interactions=True,
        )
        self.magnetic_primary_parameter_cards = ft.Row(
            [
                self.magnetic_actuation_control,
                self.magnetic_rt_release_control,
                self.magnetic_rt_press_control,
                # In ordinary mode this is immediately after activation;
                # it is never appended after the dead-zone group.
                self.magnetic_deactivation_control,
            ],
            spacing=12,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
        self.magnetic_dead_zone_cards = ft.Row(
            [
                self.magnetic_lower_dead_zone_control,
                self.magnetic_upper_dead_zone_control,
            ],
            spacing=12,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
        self.magnetic_parameter_cards = ft.Row(
            [
                self.magnetic_primary_parameter_cards,
                self.magnetic_dead_zone_spacer,
                self.magnetic_dead_zone_cards,
            ],
            spacing=0,
            wrap=False,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
        # Keep the optional independent threshold in the otherwise-empty
        # right-hand side of the selected-key panel.  RT and normal mode reuse
        # that exact slot instead of stacking two long cards on the left.
        magnetic_parameter_header = ft.Row(
            [
                self.magnetic_parameter_mode_surface,
                self.magnetic_rt_separate_surface,
                self.magnetic_deactivation_separate_surface,
            ],
            spacing=10,
            tight=False,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        # This is the one update boundary for every dynamic part of the
        # selected-key panel.  In particular, changing Rapid Trigger changes
        # several child visibility flags at once; updating this parent once is
        # safe while Flet is reconciling a scrolling page.
        self.magnetic_parameter_header = magnetic_parameter_header
        self.magnetic_parameter_panel = ft.Column(
            [magnetic_parameter_header, self.magnetic_parameter_cards],
            spacing=10,
        )

        content = ft.Column(
            [
                ft.Row(
                    [profile_card, rt_stab_card, anti_accidental_card, snap_key_card],
                    spacing=12,
                    wrap=True,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                self.keyboard_picker_hint,
                self.keyboard_picker_root,
                self.magnetic_key_metrics_explanation,
                ft.Text("Параметры выбранной клавиши", size=16, weight=ft.FontWeight.W_600),
                self.magnetic_parameter_panel,
                self.magnetic_status,
            ],
            spacing=10,
        )
        self._update_magnetic_parameter_mode_ui(update=False)
        self._load_magnetic_controls(self.magnetic_selected_slot)
        self._refresh_snap_key_summary()
        self._refresh_sk75_keyboard_picker()
        self._install_sk75_keyboard_resize_handler()
        return self._card(
            icon=ft.Icons.TOLL_ROUNDED,
            title="Magnetic Lab · SK75 TMR",
            subtitle="Точные настройки магнитных клавиш, Rapid Trigger и Snap Key.",
            content=ft.Container(content, margin=ft.Margin.only(top=12)),
        )

    def _sk75_deck_width_for_current_viewport(self):
        """Measure the full visual board against the page's usable width."""
        viewport_width = getattr(self, "_sk75_viewport_width", None)
        if not viewport_width:
            viewport_width = getattr(getattr(self, "page", None), "width", None)
        return _sk75_visual_deck_width_for_viewport(viewport_width)

    def _on_sk75_keyboard_page_resize(self, event):
        """Reflow the absolute 17u deck after a native-window resize.

        Selection still patches two individual caps; a complete board build is
        performed only when its pixel width actually changes.
        """
        if not hasattr(self, "keyboard_picker_root"):
            return
        viewport_width = getattr(event, "width", None)
        if not viewport_width:
            viewport_width = getattr(getattr(self, "page", None), "width", None)
        self._sk75_viewport_width = viewport_width
        target_width = self._sk75_deck_width_for_current_viewport()
        if target_width == getattr(self, "_sk75_rendered_deck_width", None):
            return
        self._refresh_sk75_keyboard_picker()

    def _install_sk75_keyboard_resize_handler(self):
        """Attach one composable page-resize listener for the Magnetic Lab."""
        if getattr(self, "_sk75_resize_handler_installed", False):
            return
        page = getattr(self, "page", None)
        if page is None:
            return
        previous_handler = getattr(page, "on_resize", None)

        def handle_resize(event):
            if callable(previous_handler):
                previous_handler(event)
            self._on_sk75_keyboard_page_resize(event)

        try:
            page.on_resize = handle_resize
        except (AttributeError, TypeError):
            # Older portable Flet clients simply keep the conservative first
            # frame width.  It already fits the app's enforced minimum width.
            return
        self._sk75_resize_handler_installed = True

    def _sk75_key_name(self, slot):
        key = SK75_KEY_BY_SLOT.get(slot)
        return key.label if key is not None else "неизвестная клавиша"

    @staticmethod
    def _magnetic_travel_is_pressed(travel_mm):
        """Compatibility helper for callers that only need a travel threshold.

        The visual test itself intentionally does *not* use this state: a
        magnetic key can be held at any depth, while the UI needs to show the
        current physical direction (downstroke or upstroke).
        """
        try:
            return float(travel_mm) > 0.015
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _travel_tester_key_name(slot):
        """Return a legacy Windows key name without affecting live testing.

        Kept for integrations that used the old helper.  The tester no longer
        calls it: the SK75 live-travel stream represents movement from any
        magnetic key, not only the currently selected key.
        """
        key = SK75_KEY_BY_SLOT.get(slot)
        hid = getattr(key, "hid", None)
        if hid is None:
            return None
        if 4 <= hid <= 29:
            return chr(ord("a") + hid - 4)
        if 30 <= hid <= 38:
            return str(hid - 29)
        if hid == 39:
            return "0"
        if 58 <= hid <= 69:
            return f"f{hid - 57}"
        return {
            40: "enter", 41: "esc", 42: "backspace", 43: "tab",
            44: "space", 45: "-", 46: "=", 47: "[", 48: "]",
            49: "\\", 51: ";", 52: "'", 53: "`", 54: ",", 55: ".",
            56: "/", 57: "caps lock", 74: "home", 75: "page up",
            76: "delete", 78: "page down", 79: "right", 80: "left",
            81: "down", 82: "up", 224: "left ctrl", 225: "left shift",
            226: "left alt", 227: "left windows", 228: "right ctrl",
            229: "right shift",
        }.get(hid)

    @staticmethod
    def _travel_tester_virtual_key(slot):
        """Map a key to a Win32 virtual key for backwards-compatible callers.

        This mapping deliberately has no live listener and is not used by the
        current tester, whose data comes solely from Womier's reversible HID
        travel-report stream.
        """
        key = SK75_KEY_BY_SLOT.get(slot)
        hid = getattr(key, "hid", None)
        if hid is None:
            return None
        if 4 <= hid <= 29:
            return ord("A") + hid - 4
        if 30 <= hid <= 38:
            return ord("1") + hid - 30
        if hid == 39:
            return ord("0")
        if 58 <= hid <= 69:
            return 0x70 + hid - 58
        return {
            40: 0x0D, 41: 0x1B, 42: 0x08, 43: 0x09, 44: 0x20,
            45: 0xBD, 46: 0xBB, 47: 0xDB, 48: 0xDD, 49: 0xDC,
            51: 0xBA, 52: 0xDE, 53: 0xC0, 54: 0xBC, 55: 0xBE,
            56: 0xBF, 57: 0x14, 74: 0x24, 75: 0x21, 76: 0x2E,
            78: 0x22, 79: 0x27, 80: 0x25, 81: 0x28, 82: 0x26,
            224: 0xA2, 225: 0xA0, 226: 0xA4, 227: 0x5B,
            228: 0xA3, 229: 0xA1, 230: 0xA5, 231: 0x5C,
        }.get(hid)

    @staticmethod
    def _is_windows_virtual_key_pressed(virtual_key):
        """Read one Win32 key state without installing a global hook."""
        if virtual_key is None:
            return False
        try:
            return bool(ctypes.windll.user32.GetAsyncKeyState(int(virtual_key)) & 0x8000)
        except Exception:
            return False

    def _travel_tester_pressed_slot(self, preferred_slot=None):
        """Return the physically held SK75 key using read-only Win32 state.

        The firmware's live-travel report contains a depth but not a matrix
        slot.  Polling ``GetAsyncKeyState`` lets the tester associate that
        depth with the actual key the user is holding without a keyboard hook
        or a listener that could outlive the dialog.  Prefer the previously
        detected key while it remains held so two briefly overlapping presses
        do not make the active-key label jump between keys.
        """
        preferred_key = SK75_KEY_BY_SLOT.get(preferred_slot)
        if preferred_key is not None:
            virtual_key = self._travel_tester_virtual_key(preferred_slot)
            if self._is_windows_virtual_key_pressed(virtual_key):
                return preferred_slot

        for key in SK75_KEYS:
            virtual_key = self._travel_tester_virtual_key(key.slot)
            if virtual_key is not None and self._is_windows_virtual_key_pressed(virtual_key):
                return key.slot
        return None

    @staticmethod
    def _magnetic_travel_direction(previous_mm, current_mm, *, epsilon=0.0005):
        """Return the instant direction of a live magnetic-travel sample.

        ``None`` means no measurable movement, ``"down"`` means the key is
        moving deeper, and ``"up"`` means it is returning.  This deliberately
        compares consecutive samples instead of treating every non-zero value
        as a press, so the colour changes as soon as the key reverses even
        when it has not returned to 0 mm yet.
        """
        try:
            previous = float(previous_mm)
            current = float(current_mm)
        except (TypeError, ValueError):
            return None
        if current > previous + epsilon:
            return "down"
        if current < previous - epsilon:
            return "up"
        return None

    @staticmethod
    def _magnetic_travel_stable_direction(
        anchor_mm,
        current_mm,
        *,
        hysteresis_mm=TRAVEL_TESTER_DIRECTION_HYSTERESIS_MM,
    ):
        """Return a debounced direction and its next comparison anchor.

        The SK75 reports movement in hundredths (or half-hundredths) of a
        millimetre.  Comparing neighbouring raw packets made harmless sensor
        jitter alternate the green/blue direction every display frame.  That
        in turn rebuilt several coloured Flet effects unnecessarily.  Keeping
        a tiny physical hysteresis still reacts within 0.02 mm, but only after
        there has been real travel in either direction.
        """
        try:
            anchor = float(anchor_mm)
            current = float(current_mm)
            hysteresis = max(0.0, float(hysteresis_mm))
        except (TypeError, ValueError):
            return None, anchor_mm
        if current >= anchor + hysteresis:
            return "down", current
        if current <= anchor - hysteresis:
            return "up", current
        return None, anchor

    @staticmethod
    def _drain_magnetic_travel_samples(read_report, *, step, max_reports=64):
        """Discard stale HID input reports and return the newest valid depth.

        A non-blocking HID input endpoint has its own native queue.  The UI is
        intentionally rate-limited, so consuming only one packet per loop
        makes the display replay old movement instead of showing the key's
        current position.  This helper drains a bounded batch and retains the
        most recent recognised travel report.  The bound keeps a noisy device
        from monopolising the reader thread while the next pass catches up.
        """
        try:
            limit = max(1, min(int(max_reports), 512))
        except (TypeError, ValueError):
            limit = 1

        newest_mm = None
        reports_read = 0
        for _index in range(limit):
            report = read_report()
            if not report:
                break
            reports_read += 1
            travel_mm = MagneticProtocol.decode_magnetic_travel_report(
                report, step=step
            )
            if travel_mm is not None:
                newest_mm = travel_mm
        return newest_mm, reports_read

    @staticmethod
    def _magnetic_travel_tester_scale():
        """Return the current SK75's official live-travel ruler.

        Calibration progress is not a per-key travel limit.  The official
        driver instead exposes one current-SK75 travel cap through
        :class:`MagneticProtocol`; keep the tester's ruler, sample clamp and
        animation tied to that source instead of advertising the obsolete
        3.50 mm range.
        """
        full_travel_mm = float(MagneticProtocol.OFFICIAL_SK75_ACTUATION_MAX_MM)
        tick_step_mm = 0.50
        ticks = [
            round(index * tick_step_mm, 2)
            for index in range(int(full_travel_mm // tick_step_mm) + 1)
        ]
        if not ticks or ticks[-1] < full_travel_mm:
            ticks.append(full_travel_mm)
        return full_travel_mm, tuple(ticks)

    @staticmethod
    def _vertical_magnetic_pointer_fraction(pointer_y, track_height, *, fills_from_top=False):
        """Translate a pointer position into a semantic scale fraction.

        Actuation, both Rapid Trigger thresholds and the upper dead zone grow
        *down* from the top: the smallest value is at the top of the ruler,
        matching the direction used by the main switch-travel controls.  Only
        the lower dead zone grows from the physical bottom.  Keeping that
        distinction here makes interaction match the rendered fill instead
        of merely reversing its paint direction.
        """
        try:
            height = max(1.0, float(track_height))
            y = max(0.0, min(float(pointer_y), height))
        except (TypeError, ValueError):
            return 0.0
        fraction = y / height
        return fraction if fills_from_top else 1.0 - fraction

    @staticmethod
    def _vertical_magnetic_scale_fraction(value, minimum, maximum):
        """Clamp a raw hundredths-of-a-millimetre value to a 0…1 scale."""
        try:
            minimum = float(minimum)
            maximum = float(maximum)
            if maximum <= minimum:
                return 0.0
            return max(0.0, min(1.0, (float(value) - minimum) / (maximum - minimum)))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _consume_magnetic_pointer_event():
        """Prevent Flet's implicit full-page patch for a ruler input event.

        Flet 0.85 automatically updates the nearest isolated ancestor after
        an event handler which did not explicitly update a control.  A
        throttled ruler sample deliberately has *no* visual patch, so without
        this acknowledgement Flet walks up to the page and redraws the whole
        Magnetic Lab (including the 75% key deck) for every skipped pointer
        sample.  That is the main source of the apparent slider/GPU lag.

        The normal ruler painter publishes its small dynamic leaf itself at a
        bounded cadence.  Marking the event handled keeps the event loop free
        until that leaf patch is actually due.  It is safe for detached unit
        tests too: Flet's context owns a default update state outside a live
        page.
        """
        try:
            ft.context.mark_update_called()
        except Exception:
            # A page may be closing while a final native drag event arrives.
            # The model is still valid, and no implicit page repaint should be
            # attempted from that stale callback.
            pass

    def _paint_vertical_magnetic_control(self, state, *, update_controls=True):
        """Paint only the mutable leaves of one magnetic scale.

        Flet 0.85 builds a patch by walking the subtree passed to
        ``Control.update()``.  Updating the old outer ruler ``Container`` on
        every click/drag therefore walked the static rail, 17 tick marks and
        endpoint captions even though only the fill, thumb and readout had
        changed.  Keep the ruler's static drawing outside the update boundary
        and record dirtiness for its three truly mutable leaves instead.
        """
        # The device-facing state is shared by the legacy ruler and the new
        # Material 3 card.  Keep this public helper as the common paint entry
        # point so loading/startup-lock code does not need to know which view
        # currently represents the value.
        if getattr(state, "presentation", None) == "m3_parameter":
            self._paint_m3_magnetic_parameter_control(
                state, update_controls=update_controls
            )
            return
        if getattr(state, "presentation", None) == "m3_vertical_parameter":
            self._paint_m3_vertical_magnetic_parameter_control(
                state, update_controls=update_controls
            )
            return
        fraction = self._vertical_magnetic_scale_fraction(
            state.value, state.minimum, state.maximum
        )
        # Treat the threshold as a real position rather than reserving a
        # decorative minimum fill.  At the actual minimum the coloured part
        # can disappear completely, leaving the divider at the endpoint just
        # like a native Material slider at zero.
        fill_height = max(0, round(state.track_height * fraction))
        state.fill.height = fill_height
        # The tonal overlay is clipped to the same inner slot as the fill;
        # it gives the active piece depth without an external glow or a
        # shadow that can bleed out of the dark pill-shaped track.
        state.fill_glow.height = fill_height
        # The slim divider deliberately straddles the active/inactive edge.
        # This is the vertical equivalent of the subtle light separator in
        # Android's media volume controls, while the interaction remains the
        # same exact 0.01 mm ruler.
        thumb_position = max(
            0,
            min(
                state.track_height - state.thumb_height,
                round(fill_height - state.thumb_height / 2),
            ),
        )
        if state.fills_from_top:
            # The upper dead zone is a block at the top of switch travel.
            # Its boundary deliberately moves down as its value increases.
            state.thumb.top = thumb_position
            state.thumb_glow.top = max(
                0,
                min(
                    state.track_height - state.thumb_glow_height,
                    round(fill_height - state.thumb_glow_height / 2),
                ),
            )
        else:
            state.thumb.bottom = thumb_position
            state.thumb_glow.bottom = max(
                0,
                min(
                    state.track_height - state.thumb_glow_height,
                    round(fill_height - state.thumb_glow_height / 2),
                ),
            )
        display_value = f"{state.value / 100:.2f} мм"
        state.value_text.value = display_value
        # Keep the precise +/- affordances honest at either hardware limit.
        # They are optional state members during the first construction paint,
        # before the buttons themselves have been attached to the ruler.
        decrease_button = getattr(state, "decrease_button", None)
        increase_button = getattr(state, "increase_button", None)
        interactive = bool(getattr(state, "interaction_enabled", True))
        decrease_disabled = (not interactive) or state.value <= state.minimum
        increase_disabled = (not interactive) or state.value >= state.maximum
        if decrease_button is not None:
            decrease_button.disabled = decrease_disabled
        if increase_button is not None:
            increase_button.disabled = increase_disabled

        # Do not treat a local model change as painted until the corresponding
        # Flet leaf has actually been patched.  ``update_controls=False`` is
        # used while a hidden linked threshold follows another one; preserving
        # the dirty state lets the next visible paint publish the correct
        # value instead of waiting for an unrelated RT toggle.
        dynamic_signature = (
            fill_height,
            thumb_position,
            state.fills_from_top,
        )
        readout_signature = display_value
        button_signature = (decrease_disabled, increase_disabled)
        state._vertical_pending_dynamic_signature = dynamic_signature
        state._vertical_pending_readout_signature = readout_signature
        state._vertical_pending_button_signature = button_signature
        state._vertical_dynamic_dirty = (
            dynamic_signature
            != getattr(state, "_vertical_painted_dynamic_signature", None)
        )
        state._vertical_readout_dirty = (
            readout_signature
            != getattr(state, "_vertical_painted_readout_signature", None)
        )
        painted_button_signature = getattr(
            state, "_vertical_painted_button_signature", (None, None)
        )
        if not isinstance(painted_button_signature, tuple) or len(painted_button_signature) != 2:
            painted_button_signature = (None, None)
        state._vertical_decrease_button_dirty = (
            button_signature[0] != painted_button_signature[0]
        )
        state._vertical_increase_button_dirty = (
            button_signature[1] != painted_button_signature[1]
        )
        if not update_controls or not getattr(state, "mounted", False):
            return
        # A parameter-mode transition changes several siblings' visibility.
        # Do not issue a child patch while its stable parent is about to be
        # reconciled: Flet 0.85 may otherwise walk a mutable internal control
        # map and raise ``dictionary changed size during iteration``.  The
        # transition sends one parent patch after all values are settled.
        if bool(getattr(self, "_magnetic_parameter_mode_transition", False)):
            return

        # Patch only the mutable paint/readout leaves.  The static tick/ruler
        # tree is intentionally never a patch root during normal interaction.
        self._patch_vertical_magnetic_control(state)

    def _flush_magnetic_vertical_drag_paint(self, state, *, force=False):
        """Paint a dragged ruler at a bounded rate on the Flet event thread.

        A vertical ruler contains a full travel rail, 17 tick marks, a value
        label and two state-layer buttons.  Patching that whole small subtree
        *and* the selected keyboard key on every raw pointer sample can flood
        the Flet transport faster than the browser consumes it.  The result
        looks like a stuck ruler even though its Python value has already
        changed.

        Keep the authoritative value hot on every pointer event, but publish
        it at a modest frame rate.  This is deliberately synchronous and is
        only called from the Flet gesture callback: no timer is allowed to
        call ``Control.update()`` from a background thread.  The first and
        final samples are forced, so a short click and the final 0.01 mm value
        are never dropped.
        """
        if bool(getattr(self, "_magnetic_parameter_mode_transition", False)):
            return False
        now = time.monotonic()
        interval = float(getattr(state, "visual_update_interval", 1 / 24))
        last_update = float(getattr(state, "last_visual_update_at", 0.0))
        if not force and now - last_update < interval:
            return False
        state.last_visual_update_at = now
        self._paint_vertical_magnetic_control(state, update_controls=True)
        return True

    def _patch_vertical_magnetic_control(self, state):
        """Commit just the ruler leaves that changed, with a safe fallback.

        A normal 0.01-mm click now sends two tiny patches at most: the dynamic
        fill/thumb stack and the value text.  A +/- button is additionally
        patched only on a hardware endpoint where its disabled state changes.
        In particular, this never uses the outer 292-px container as the
        update root during normal interaction, so Flet does not repeatedly
        traverse the static tick/gradient/ruler subtree.
        """
        dynamic_layer = getattr(state, "paint_layer", None)
        value_text = getattr(state, "value_text", None)
        decrease_button = getattr(state, "decrease_button", None)
        increase_button = getattr(state, "increase_button", None)
        targets = []
        if bool(getattr(state, "_vertical_dynamic_dirty", False)) and dynamic_layer is not None:
            targets.append(dynamic_layer)
        if bool(getattr(state, "_vertical_readout_dirty", False)) and value_text is not None:
            targets.append(value_text)
        if bool(getattr(state, "_vertical_decrease_button_dirty", False)) and decrease_button is not None:
            targets.append(decrease_button)
        if bool(getattr(state, "_vertical_increase_button_dirty", False)) and increase_button is not None:
            targets.append(increase_button)

        if targets:
            try:
                for target in targets:
                    target.update()
                state._vertical_painted_dynamic_signature = getattr(
                    state, "_vertical_pending_dynamic_signature", None
                )
                state._vertical_painted_readout_signature = getattr(
                    state, "_vertical_pending_readout_signature", None
                )
                state._vertical_painted_button_signature = getattr(
                    state, "_vertical_pending_button_signature", None
                )
                return True
            except Exception:
                logger.debug(
                    "could not patch magnetic ruler leaves; using parent",
                    exc_info=True,
                )
        elif dynamic_layer is not None:
            # Nothing is dirty: the model has already reached the client, so
            # avoid emitting an empty Flet patch for a duplicate pointer event.
            return True

        # A detached/settling leaf is unusual.  The existing persistent parent
        # fallback remains important for that one frame: it preserves the
        # user's latest value instead of making it appear only after an RT
        # switch.  This branch is never used on the normal ruler hot path.
        control = getattr(state, "control", None)
        if control is not None:
            try:
                control.update()
                state._vertical_painted_dynamic_signature = getattr(
                    state, "_vertical_pending_dynamic_signature", None
                )
                state._vertical_painted_readout_signature = getattr(
                    state, "_vertical_pending_readout_signature", None
                )
                state._vertical_painted_button_signature = getattr(
                    state, "_vertical_pending_button_signature", None
                )
                return True
            except Exception:
                logger.debug(
                    "could not patch magnetic ruler fallback control; using parent",
                    exc_info=True,
                )
        try:
            patched = bool(self._patch_magnetic_parameter_panel())
            if patched:
                state._vertical_painted_dynamic_signature = getattr(
                    state, "_vertical_pending_dynamic_signature", None
                )
                state._vertical_painted_readout_signature = getattr(
                    state, "_vertical_pending_readout_signature", None
                )
                state._vertical_painted_button_signature = getattr(
                    state, "_vertical_pending_button_signature", None
                )
            return patched
        except (AttributeError, TypeError):
            # Lightweight test managers and first construction do not own a
            # mounted parent yet.  Their in-memory ruler value remains the
            # correct first paint.
            return False

    def _make_vertical_magnetic_control(
        self,
        label,
        value,
        minimum,
        maximum,
        divisions,
        color,
        *,
        fills_from_top=False,
        icon=None,
    ):
        """Create a full-height ruler matching the travel-test meter.

        The magnetic controls remain vertical.  They use the same compact
        dark rail, readable side scale and clipped inner glow as the live
        travel test, rather than a broad phone-style fill.
        """
        # Fixed dimensions keep all five controls aligned in the desktop
        # layout.  Only the physical rail is rounded; the outer control stays
        # unframed, so there is no card around every magnetic setting.
        track_height = 224
        track_width = 158
        rail_width = 28
        rail_left = 12
        fill_width = 20
        fill_left = rail_left + 4
        thumb_width = rail_width
        thumb_height = 4
        thumb_left = rail_left
        # Glow is deliberately contained in the rail area: it makes the
        # threshold legible like the tester without spilling over the marks.
        thumb_glow_width = rail_width + 4
        thumb_glow_height = 10
        thumb_glow_left = rail_left - 2
        thumb_glow_offset = (thumb_glow_height - thumb_height) // 2
        ruler_left = 54
        endpoint_left = 96
        fills_from_top = bool(fills_from_top)
        state = SimpleNamespace(
            value=float(value),
            minimum=float(minimum),
            maximum=float(maximum),
            divisions=int(divisions),
            # The protocol stores every visible magnetic value in hundredths
            # of a millimetre.  Keep the actual scale increment on the state
            # so the +/- affordances below always move exactly one rendered
            # division instead of relying on drag rounding.
            step=(float(maximum) - float(minimum)) / max(1, int(divisions)),
            label=label,
            icon=icon,
            track_height=track_height,
            track_width=track_width,
            fills_from_top=fills_from_top,
            interaction_enabled=True,
            last_pointer_y=track_height / 2,
            # The tester-like right-hand marks make the vertical travel
            # readable at a glance without becoming a dense visual ladder.
            tick_count=17,
            rail_left=rail_left,
            ruler_left=ruler_left,
            thumb_height=thumb_height,
            thumb_glow_height=thumb_glow_height,
            thumb_glow_offset=thumb_glow_offset,
            # Raw drag samples arrive much faster than a native Flet control
            # tree can be diffed.  Keep the input model immediate, but cap
            # actual Flet/GPU commits to 24 fps.  A static ruler consumes no
            # frames; the cap applies only while a person is dragging it.
            visual_update_interval=1 / 24,
            last_visual_update_at=0.0,
            interaction_active=False,
        )
        value_text = ft.Text(
            size=12,
            weight=ft.FontWeight.W_700,
            text_align=ft.TextAlign.CENTER,
            color=color,
            no_wrap=True,
        )
        rail = ft.Container(
            # Same quiet rail as the travel tester: dark, compact and with a
            # gentle rounded edge only at its physical outside.
            width=rail_width,
            height=track_height,
            left=rail_left,
            bgcolor=ft.Colors.with_opacity(0.78, ft.Colors.SURFACE_CONTAINER_HIGHEST),
            border=None,
            border_radius=8,
        )
        fill_glow = ft.Container(
            # Soft inner aura, matching the live travel meter.  It remains
            # clipped to the rail stack and cannot leak into the side ruler.
            width=rail_width,
            left=rail_left,
            top=0 if fills_from_top else None,
            bottom=None if fills_from_top else 0,
            gradient=ft.LinearGradient(
                begin=ft.Alignment.CENTER_LEFT,
                end=ft.Alignment.CENTER_RIGHT,
                colors=[
                    ft.Colors.TRANSPARENT,
                    ft.Colors.with_opacity(0.46, color),
                    ft.Colors.TRANSPARENT,
                ],
            ),
            border_radius=0,
        )
        fill = ft.Container(
            width=fill_width,
            left=fill_left,
            top=0 if fills_from_top else None,
            bottom=None if fills_from_top else 0,
            bgcolor=ft.Colors.with_opacity(0.82, color),
            border_radius=3,
        )
        thumb_glow = ft.Container(
            # A contained gradient instead of an external drop shadow keeps
            # the glow even at MIN and MAX.
            width=thumb_glow_width,
            height=thumb_glow_height,
            left=thumb_glow_left,
            top=0 if fills_from_top else None,
            bottom=None if fills_from_top else 0,
            gradient=ft.LinearGradient(
                begin=ft.Alignment.CENTER_LEFT,
                end=ft.Alignment.CENTER_RIGHT,
                colors=[
                    ft.Colors.TRANSPARENT,
                    ft.Colors.with_opacity(0.70, color),
                    ft.Colors.TRANSPARENT,
                ],
            ),
            border_radius=0,
        )
        thumb = ft.Container(
            # Thin bright threshold line, exactly as on the travel tester.
            width=thumb_width,
            height=thumb_height,
            left=thumb_left,
            top=0 if fills_from_top else None,
            bottom=None if fills_from_top else 0,
            bgcolor=color,
            border=ft.Border.all(
                1, ft.Colors.with_opacity(0.58, ft.Colors.ON_SURFACE)
            ),
            border_radius=1,
        )
        # The first value is painted before the Stack itself exists, so the
        # primitive parts must be available on state already.
        state.fill = fill
        state.fill_glow = fill_glow
        state.thumb_glow = thumb_glow
        state.thumb = thumb
        state.value_text = value_text

        def set_value(raw_value, update_controls=True):
            fraction = self._vertical_magnetic_scale_fraction(
                raw_value, state.minimum, state.maximum
            )
            state.value = round(
                state.minimum
                + round(fraction * state.divisions)
                * (state.maximum - state.minimum)
                / state.divisions,
                3,
            )
            self._paint_vertical_magnetic_control(
                state, update_controls=update_controls
            )

        # Expose the quantising setter to the compact +/- controls and to
        # tests.  It deliberately stays local to this ruler: the common
        # magnetic-change callback below is what follows the normal live-save
        # path for both drag and button adjustments.
        state.set_value = set_value

        def set_from_pointer(event, *, force_visual=False, mirror_keycap=True):
            # Even when this raw sample is inside the visual throttle window,
            # it is an intentionally handled event.  Otherwise Flet's
            # automatic after-event fallback patches the complete Page,
            # defeating the bounded local-ruler paint below.
            self._consume_magnetic_pointer_event()
            # Pointer events can already be queued when RT hides this optional
            # ruler.  Ignore that stale gesture instead of mutating a hidden
            # threshold after the mode transaction has completed.
            if (
                not state.interaction_enabled
                or bool(getattr(self, "_magnetic_parameter_mode_transition", False))
                or getattr(getattr(state, "control", None), "visible", True) is False
            ):
                return False
            position = getattr(event, "local_position", None)
            if position is not None and getattr(position, "y", None) is not None:
                y = float(position.y)
            else:
                delta = getattr(event, "primary_delta", None)
                if delta is None:
                    return False
                y = state.last_pointer_y + float(delta)
            state.last_pointer_y = max(0.0, min(y, track_height))
            fraction = self._vertical_magnetic_pointer_fraction(
                state.last_pointer_y,
                track_height,
                fills_from_top=state.fills_from_top,
            )
            # Mutate the local model first.  Avoid a full nested Flet patch
            # for every raw drag update; ``_flush_magnetic_vertical_drag_paint``
            # publishes the current label/rail at a bounded rate below.
            set_value(
                state.minimum + fraction * (state.maximum - state.minimum),
                update_controls=False,
            )
            painted = self._flush_magnetic_vertical_drag_paint(
                state, force=force_visual
            )
            if painted and mirror_keycap:
                # The selected cap gets just its changed corner Text on the
                # same bounded visual cadence as the ruler.  This remains a
                # tiny leaf patch, not a keyboard-deck refresh.
                self._patch_selected_magnetic_keycap_metrics_now()
            # Do not touch the keycap shell, config cache, or HID debounce
            # while the pointer is moving.  Only the changed corner Text above
            # may be patched at the bounded ruler cadence.  The final
            # interaction synchronizes linked hidden thresholds and queues
            # exactly one latest-wins HID write with the last sampled value.
            return painted

        def start_interaction(event):
            already_active = bool(getattr(state, "interaction_active", False))
            state.interaction_active = True
            # Flet emits both TapDown and VerticalDragStart for one gesture.
            # The first one publishes immediately; the second only refreshes
            # the local value and lets the regular cadence decide whether a
            # new visual patch is necessary.
            return set_from_pointer(event, force_visual=not already_active)

        def finish_interaction(event=None):
            self._consume_magnetic_pointer_event()
            if not bool(getattr(state, "interaction_active", False)):
                return
            # DragEnd/TapUp carries a final local position in Flet 0.85.  It
            # is useful when the final movement did not produce a separate
            # DragUpdate event.  Events without a position are harmless and
            # leave the last sampled value intact.
            if event is not None:
                # The outer finish path does the one final text mirror below;
                # avoid sending the same metric leaf twice for TapUp/DragEnd.
                set_from_pointer(event, force_visual=True, mirror_keycap=False)
            state.interaction_active = False
            self._flush_magnetic_vertical_drag_paint(state, force=True)
            self._patch_selected_magnetic_keycap_metrics_now()
            # Queue one latest-wins HID/keycap commit after the input handler
            # yields.  Intermediate drag samples avoid config/HID work;
            # corner texts above are already mirrored locally.
            self._commit_magnetic_input_after_frame()

        set_value(value, update_controls=False)

        def step_button(icon, tooltip, direction):
            # M3-style state layers make a small precise button discoverable
            # without introducing another card or changing its click target.
            return ft.IconButton(
                icon=icon,
                icon_size=15,
                width=28,
                height=26,
                tooltip=tooltip,
                on_click=lambda _event, scale_state=state, delta=direction: self._adjust_vertical_magnetic_value(
                    scale_state, delta
                ),
                style=ft.ButtonStyle(
                    shape=ft.RoundedRectangleBorder(radius=9),
                    padding=0,
                    color={
                        ft.ControlState.DEFAULT: color,
                        ft.ControlState.HOVERED: color,
                        ft.ControlState.DISABLED: ft.Colors.with_opacity(
                            0.34, ft.Colors.ON_SURFACE_VARIANT
                        ),
                    },
                    bgcolor={
                        ft.ControlState.DEFAULT: ft.Colors.with_opacity(0.12, color),
                        ft.ControlState.HOVERED: ft.Colors.with_opacity(0.28, color),
                        ft.ControlState.DISABLED: ft.Colors.with_opacity(
                            0.06, ft.Colors.ON_SURFACE_VARIANT
                        ),
                    },
                    overlay_color={ft.ControlState.HOVERED: ft.Colors.TRANSPARENT},
                    elevation={
                        ft.ControlState.DEFAULT: 0,
                        ft.ControlState.HOVERED: 1,
                        ft.ControlState.PRESSED: 0,
                    },
                    animation_duration=120,
                ),
            )

        decrease_button = step_button(
            ft.Icons.REMOVE_ROUNDED,
            "Уменьшить на 0,01 мм",
            -1,
        )
        increase_button = step_button(
            ft.Icons.ADD_ROUNDED,
            "Увеличить на 0,01 мм",
            1,
        )
        state.decrease_button = decrease_button
        state.increase_button = increase_button
        decrease_button.disabled = state.value <= state.minimum
        increase_button.disabled = state.value >= state.maximum
        # A fixed three-column band keeps both exact-step buttons on the
        # same baseline as the numeric value.  The old tightly-sized Row let
        # the +/- pair drift horizontally depending on the localized label.
        value_row = ft.Row(
            [
                ft.Container(
                    content=decrease_button,
                    width=28,
                    alignment=ft.Alignment.CENTER,
                ),
                ft.Container(
                    content=value_text,
                    expand=True,
                    alignment=ft.Alignment.CENTER,
                ),
                ft.Container(
                    content=increase_button,
                    width=28,
                    alignment=ft.Alignment.CENTER,
                ),
            ],
            width=track_width,
            height=28,
            spacing=4,
            tight=False,
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        # Reuse the travel-test ruler language: long major marks, medium
        # midpoints and light minor marks sit beside the rail, never across
        # the coloured fill.
        tick_controls = []
        for index in range(state.tick_count):
            y = round(index * (track_height - 1) / (state.tick_count - 1))
            major = index % 8 == 0
            medium = index % 4 == 0
            tick_controls.append(
                ft.Container(
                    width=34 if major else 22 if medium else 12,
                    height=2 if major else 1,
                    left=ruler_left,
                    top=y,
                    bgcolor=ft.Colors.with_opacity(
                        0.80 if major else 0.54 if medium else 0.30,
                        ft.Colors.ON_SURFACE,
                    ),
                )
            )
        if fills_from_top:
            top_caption = f"MIN {minimum / 100:.2f} мм"
            bottom_caption = f"MAX {maximum / 100:.2f} мм"
        else:
            top_caption = f"MAX {maximum / 100:.2f} мм"
            bottom_caption = f"MIN {minimum / 100:.2f} мм"
        top_endpoint = ft.Text(
            top_caption,
            size=9,
            weight=ft.FontWeight.W_600,
            color=ft.Colors.ON_SURFACE_VARIANT,
            no_wrap=True,
        )
        bottom_endpoint = ft.Text(
            bottom_caption,
            size=9,
            weight=ft.FontWeight.W_600,
            color=ft.Colors.ON_SURFACE_VARIANT,
            no_wrap=True,
        )
        # Only this small overlay changes when a user moves a ruler.  Keeping
        # it as its own Flet control means a value update does not make Flet
        # recursively diff the static rail, 17 tick marks and captions.
        # Those elements stay in the outer track below, so the visual result
        # is identical to the former single Stack.
        paint_layer = ft.Stack(
            [fill_glow, fill, thumb_glow, thumb],
            width=track_width,
            height=track_height,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        track = ft.Stack(
            [
                rail,
                *tick_controls,
                paint_layer,
                ft.Container(content=top_endpoint, left=endpoint_left, top=4),
                ft.Container(
                    content=bottom_endpoint,
                    left=endpoint_left,
                    top=track_height - 16,
                ),
            ],
            width=track_width,
            height=track_height,
            # Keep the visual rail, fill and endpoint strictly inside this
            # small rendering surface.  The captions/ruler already live
            # within these bounds, so no useful content is clipped.
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        drag_surface = ft.GestureDetector(
            content=ft.Container(
                content=track,
                width=track_width,
                height=track_height,
            ),
            on_tap_down=start_interaction,
            on_tap_up=finish_interaction,
            on_tap_cancel=finish_interaction,
            on_vertical_drag_start=start_interaction,
            on_vertical_drag_update=set_from_pointer,
            on_vertical_drag_end=finish_interaction,
            on_vertical_drag_cancel=finish_interaction,
            # There is no benefit in waking Python at a 60+ Hz pointer rate
            # when the committed visual cadence is 24 fps.  This retains a
            # smooth ruler while avoiding a backlog of stale Flet events.
            drag_interval=24,
            mouse_cursor=ft.MouseCursor.CLICK,
        )
        label_text = ft.Text(
            label,
            size=11,
            weight=ft.FontWeight.W_700,
            text_align=ft.TextAlign.CENTER,
            max_lines=2,
            no_wrap=False,
            width=track_width - 22 if icon is not None else track_width,
        )
        # The role icons are compact but deliberately outlined in the exact
        # metric colour.  This keeps the visual key-corner mapping discoverable
        # without reintroducing a large card around each ruler.
        heading_icon = (
            ft.Container(
                content=ft.Icon(icon, size=14, color=color),
                width=24,
                height=24,
                alignment=ft.Alignment.CENTER,
                bgcolor=ft.Colors.with_opacity(0.10, color),
                border=ft.Border.all(1, ft.Colors.with_opacity(0.75, color)),
                border_radius=7,
            )
            if icon is not None
            else None
        )
        heading = ft.Row(
            [
                *([heading_icon] if heading_icon is not None else []),
                label_text,
            ],
            spacing=4 if icon is not None else 0,
            tight=True,
            alignment=ft.MainAxisAlignment.CENTER,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        control = ft.Container(
            content=ft.Column(
                [
                    # Keep every ruler aligned even when a clear Russian
                    # role needs two short lines.  Without this fixed slot,
                    # the travel rail under a longer RT heading starts lower
                    # than the other four controls.
                    ft.Container(
                        content=heading,
                        width=track_width,
                        height=28,
                        alignment=ft.Alignment.CENTER,
                    ),
                    value_row,
                    drag_surface,
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=4,
                tight=True,
            ),
            width=track_width,
            height=292,
            # This is only a layout wrapper.  Do not reintroduce a rounded
            # container behind activation, RT, or dead-zone scales.
            bgcolor=None,
            border=None,
            border_radius=None,
        )
        state.track = track
        state.paint_layer = paint_layer
        state.rail = rail
        state.tick_controls = tick_controls
        state.top_endpoint = top_endpoint
        state.bottom_endpoint = bottom_endpoint
        state.label_text = label_text
        state.heading = heading
        state.heading_icon = heading_icon
        state.value_row = value_row
        # ``paint_layer`` and ``value_text`` above are the normal update
        # boundaries.  Retain the full wrapper only as a first-mount fallback
        # when Flet rejects a leaf patch while the scroll view is settling.
        state.control = control
        state.mounted = True
        # The initial construction is included in the first page render, so
        # it is already visually committed without a child ``update()``.
        state._vertical_painted_dynamic_signature = getattr(
            state, "_vertical_pending_dynamic_signature", None
        )
        state._vertical_painted_readout_signature = getattr(
            state, "_vertical_pending_readout_signature", None
        )
        state._vertical_painted_button_signature = getattr(
            state, "_vertical_pending_button_signature", None
        )
        return value_text, state, control

    # ---------- Material 3 selected-key parameter surfaces ----------
    def _make_m3_magnetic_parameter_control(
        self,
        title,
        supporting_text,
        value,
        minimum,
        maximum,
        divisions,
        color,
        icon,
    ):
        """Create one compact Material 3 magnetic-parameter surface.

        The device model continues to use integer hundredths of a millimetre
        exactly as the former vertical ruler did.  This factory deliberately
        exposes that same state shape (``value``, ``minimum``, ``maximum``,
        ``step`` and ``set_value``), so all existing debounce/HID code keeps
        working while the visual control becomes a conventional M3 slider.
        """
        state = SimpleNamespace(
            presentation="m3_parameter",
            value=float(value),
            minimum=float(minimum),
            maximum=float(maximum),
            divisions=int(divisions),
            step=(float(maximum) - float(minimum)) / max(1, int(divisions)),
            interaction_enabled=True,
            title=str(title),
            supporting_text=str(supporting_text),
            color=color,
            icon=icon,
            mounted=True,
            interaction_active=False,
            visual_update_interval=1 / 24,
            last_visual_update_at=0.0,
        )

        title_text = ft.Text(
            state.title,
            size=13,
            weight=ft.FontWeight.W_600,
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        supporting = ft.Text(
            state.supporting_text,
            size=10,
            color=ft.Colors.ON_SURFACE_VARIANT,
            max_lines=2,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        value_text = ft.Text(
            size=24,
            weight=ft.FontWeight.W_700,
            color=color,
            no_wrap=True,
        )
        unit_text = ft.Text(
            "мм",
            size=12,
            weight=ft.FontWeight.W_600,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )

        def state_button(icon_data, tooltip, direction):
            return ft.IconButton(
                icon=icon_data,
                icon_size=18,
                width=36,
                height=36,
                tooltip=tooltip,
                on_click=lambda _event, scale_state=state, delta=direction: self._adjust_vertical_magnetic_value(
                    scale_state, delta
                ),
                style=ft.ButtonStyle(
                    shape=ft.RoundedRectangleBorder(radius=12),
                    padding=0,
                    color={
                        ft.ControlState.DEFAULT: color,
                        ft.ControlState.HOVERED: color,
                        ft.ControlState.DISABLED: ft.Colors.with_opacity(
                            0.38, ft.Colors.ON_SURFACE_VARIANT
                        ),
                    },
                    bgcolor={
                        ft.ControlState.DEFAULT: ft.Colors.with_opacity(0.10, color),
                        ft.ControlState.HOVERED: ft.Colors.with_opacity(0.20, color),
                        ft.ControlState.PRESSED: ft.Colors.with_opacity(0.30, color),
                        ft.ControlState.DISABLED: ft.Colors.with_opacity(
                            0.05, ft.Colors.ON_SURFACE_VARIANT
                        ),
                    },
                    overlay_color={
                        ft.ControlState.HOVERED: ft.Colors.with_opacity(0.08, color),
                        ft.ControlState.PRESSED: ft.Colors.with_opacity(0.16, color),
                    },
                    animation_duration=140,
                ),
            )

        decrease_button = state_button(
            ft.Icons.REMOVE_ROUNDED, "Уменьшить на 0,01 мм", -1
        )
        increase_button = state_button(
            ft.Icons.ADD_ROUNDED, "Увеличить на 0,01 мм", 1
        )

        def on_slider_change(event):
            self._consume_magnetic_pointer_event()
            if not bool(getattr(state, "interaction_enabled", True)):
                return
            # Let Flutter keep moving the native thumb.  The Python side only
            # changes its local model here and emits a bounded card paint;
            # persistence/keycap work happens once at drag end.
            state.set_value(
                getattr(event.control, "value", state.value),
                update_controls=False,
            )
            if self._flush_magnetic_vertical_drag_paint(state):
                self._patch_selected_magnetic_keycap_metrics_now()

        def on_slider_start(_event):
            self._consume_magnetic_pointer_event()
            state.interaction_active = True
            self._flush_magnetic_vertical_drag_paint(state, force=True)

        def on_slider_end(_event):
            self._consume_magnetic_pointer_event()
            state.interaction_active = False
            self._flush_magnetic_vertical_drag_paint(state, force=True)
            self._patch_selected_magnetic_keycap_metrics_now()
            self._commit_magnetic_input_after_frame()

        slider = ft.Slider(
            min=state.minimum,
            max=state.maximum,
            divisions=state.divisions,
            value=state.value,
            round=0,
            active_color=color,
            inactive_color=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            secondary_active_color=ft.Colors.with_opacity(0.28, color),
            thumb_color=color,
            overlay_color={
                ft.ControlState.HOVERED: ft.Colors.with_opacity(0.12, color),
                ft.ControlState.PRESSED: ft.Colors.with_opacity(0.20, color),
            },
            interaction=ft.SliderInteraction.TAP_AND_SLIDE,
            year_2023=False,
            height=32,
            padding=ft.Padding.symmetric(horizontal=2, vertical=0),
            on_change=on_slider_change,
            on_change_start=on_slider_start,
            on_change_end=on_slider_end,
        )
        min_text = ft.Text(
            f"Мин. {state.minimum / 100:.2f}",
            size=10,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        max_text = ft.Text(
            f"Макс. {state.maximum / 100:.2f}",
            size=10,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )

        card = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Container(
                                content=ft.Icon(icon, size=18, color=color),
                                width=32,
                                height=32,
                                alignment=ft.Alignment.CENTER,
                                bgcolor=ft.Colors.with_opacity(0.13, color),
                                border_radius=10,
                            ),
                            ft.Column([title_text, supporting], spacing=1, expand=True),
                        ],
                        spacing=9,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            ft.Row([value_text, unit_text], spacing=4, tight=True),
                            ft.Row([decrease_button, increase_button], spacing=2, tight=True),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    slider,
                    ft.Row(
                        [min_text, max_text],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                ],
                spacing=4,
                tight=True,
            ),
            width=258,
            height=166,
            padding=14,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=20,
            animate=ft.Animation(160, ft.AnimationCurve.EASE_OUT),
            animate_opacity=ft.Animation(160, ft.AnimationCurve.EASE_OUT),
        )

        state.title_text = title_text
        state.supporting = supporting
        state.value_text = value_text
        state.unit_text = unit_text
        state.slider = slider
        state.min_text = min_text
        state.max_text = max_text
        state.decrease_button = decrease_button
        state.increase_button = increase_button
        state.card = card
        state.control = card

        def set_value(raw_value, update_controls=True):
            try:
                fraction = self._vertical_magnetic_scale_fraction(
                    raw_value, state.minimum, state.maximum
                )
                state.value = round(
                    state.minimum
                    + round(fraction * state.divisions)
                    * (state.maximum - state.minimum)
                    / state.divisions,
                    3,
                )
            except (TypeError, ValueError):
                return
            self._paint_m3_magnetic_parameter_control(
                state, update_controls=update_controls
            )

        state.set_value = set_value
        set_value(value, update_controls=False)
        return value_text, state, card

    def _make_m3_vertical_magnetic_parameter_control(
        self,
        title,
        supporting_text,
        value,
        minimum,
        maximum,
        divisions,
        color,
        icon,
        *,
        fills_from_top=True,
    ):
        """Create a compact vertical M3 parameter card.

        Flet's native ``Slider`` is horizontal, so this uses the same precise
        touch/drag math as the former ruler while rendering it as a Material
        3 surface: tonal card, state-layer +/- buttons, contained glow and a
        bright threshold handle.  The state contract is identical to the
        horizontal card and therefore preserves all device/HID behaviour.
        """
        track_height = 164
        track_width = 174
        rail_width = 32
        rail_left = 12
        fill_width = 24
        fill_left = rail_left + 4
        ruler_left = 58
        endpoint_left = 92
        thumb_height = 5
        state = SimpleNamespace(
            presentation="m3_vertical_parameter",
            value=float(value),
            minimum=float(minimum),
            maximum=float(maximum),
            divisions=int(divisions),
            step=(float(maximum) - float(minimum)) / max(1, int(divisions)),
            interaction_enabled=True,
            title=str(title),
            supporting_text=str(supporting_text),
            color=color,
            icon=icon,
            fills_from_top=bool(fills_from_top),
            track_height=track_height,
            track_width=track_width,
            thumb_height=thumb_height,
            interaction_active=False,
            mounted=True,
            last_pointer_y=track_height / 2,
            # Keep the legacy and M3 vertical variants on the same bounded
            # visual cadence.  The M3 factory is retained for compatibility
            # with existing layouts/tests, so it must not reintroduce a
            # per-event keycap/HID cascade when it is mounted.
            visual_update_interval=1 / 24,
            last_visual_update_at=0.0,
        )
        title_text = ft.Text(
            state.title,
            size=11,
            weight=ft.FontWeight.W_700,
            max_lines=2,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        supporting = ft.Text(
            state.supporting_text,
            size=9,
            color=ft.Colors.ON_SURFACE_VARIANT,
            max_lines=2,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        value_text = ft.Text(
            size=20,
            weight=ft.FontWeight.W_700,
            color=color,
            no_wrap=True,
        )
        unit_text = ft.Text(
            "мм",
            size=10,
            weight=ft.FontWeight.W_600,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )

        def state_button(icon_data, tooltip, direction):
            return ft.IconButton(
                icon=icon_data,
                icon_size=16,
                width=30,
                height=30,
                tooltip=tooltip,
                on_click=lambda _event, scale_state=state, delta=direction: self._adjust_vertical_magnetic_value(
                    scale_state, delta
                ),
                style=ft.ButtonStyle(
                    shape=ft.RoundedRectangleBorder(radius=10),
                    padding=0,
                    color={
                        ft.ControlState.DEFAULT: color,
                        ft.ControlState.HOVERED: color,
                        ft.ControlState.DISABLED: ft.Colors.with_opacity(0.38, ft.Colors.ON_SURFACE_VARIANT),
                    },
                    bgcolor={
                        ft.ControlState.DEFAULT: ft.Colors.with_opacity(0.10, color),
                        ft.ControlState.HOVERED: ft.Colors.with_opacity(0.22, color),
                        ft.ControlState.PRESSED: ft.Colors.with_opacity(0.32, color),
                        ft.ControlState.DISABLED: ft.Colors.with_opacity(0.05, ft.Colors.ON_SURFACE_VARIANT),
                    },
                    animation_duration=140,
                ),
            )

        decrease_button = state_button(ft.Icons.REMOVE_ROUNDED, "Уменьшить на 0,01 мм", -1)
        increase_button = state_button(ft.Icons.ADD_ROUNDED, "Увеличить на 0,01 мм", 1)
        rail = ft.Container(
            width=rail_width,
            height=track_height,
            left=rail_left,
            bgcolor=ft.Colors.with_opacity(0.84, ft.Colors.SURFACE_CONTAINER_HIGHEST),
            border_radius=8,
        )
        fill_glow = ft.Container(
            width=rail_width,
            left=rail_left,
            top=0 if state.fills_from_top else None,
            bottom=None if state.fills_from_top else 0,
            gradient=ft.LinearGradient(
                begin=ft.Alignment.CENTER_LEFT,
                end=ft.Alignment.CENTER_RIGHT,
                colors=[
                    ft.Colors.TRANSPARENT,
                    ft.Colors.with_opacity(0.42, color),
                    ft.Colors.TRANSPARENT,
                ],
            ),
            border_radius=7,
        )
        fill = ft.Container(
            width=fill_width,
            left=fill_left,
            top=0 if state.fills_from_top else None,
            bottom=None if state.fills_from_top else 0,
            bgcolor=ft.Colors.with_opacity(0.82, color),
            border_radius=5,
        )
        thumb_glow = ft.Container(
            width=rail_width + 6,
            height=12,
            left=rail_left - 3,
            top=0 if state.fills_from_top else None,
            bottom=None if state.fills_from_top else 0,
            gradient=ft.LinearGradient(
                begin=ft.Alignment.CENTER_LEFT,
                end=ft.Alignment.CENTER_RIGHT,
                colors=[
                    ft.Colors.TRANSPARENT,
                    ft.Colors.with_opacity(0.68, color),
                    ft.Colors.TRANSPARENT,
                ],
            ),
            border_radius=8,
        )
        thumb = ft.Container(
            width=rail_width + 2,
            height=thumb_height,
            left=rail_left - 1,
            top=0 if state.fills_from_top else None,
            bottom=None if state.fills_from_top else 0,
            bgcolor=color,
            border=ft.Border.all(1, ft.Colors.with_opacity(0.58, ft.Colors.ON_SURFACE)),
            border_radius=1,
        )
        tick_controls = []
        tick_count = 9
        for index in range(tick_count):
            y = round(index * (track_height - 1) / (tick_count - 1))
            major = index % 4 == 0
            tick_controls.append(
                ft.Container(
                    width=28 if major else 14,
                    height=2 if major else 1,
                    left=ruler_left,
                    top=y,
                    bgcolor=ft.Colors.with_opacity(
                        0.72 if major else 0.38, ft.Colors.ON_SURFACE
                    ),
                )
            )
        if state.fills_from_top:
            top_caption = f"MIN {state.minimum / 100:.2f} мм"
            bottom_caption = f"MAX {state.maximum / 100:.2f} мм"
        else:
            top_caption = f"MAX {state.maximum / 100:.2f} мм"
            bottom_caption = f"MIN {state.minimum / 100:.2f} мм"
        top_endpoint = ft.Text(top_caption, size=8, weight=ft.FontWeight.W_600, color=ft.Colors.ON_SURFACE_VARIANT, no_wrap=True)
        bottom_endpoint = ft.Text(bottom_caption, size=8, weight=ft.FontWeight.W_600, color=ft.Colors.ON_SURFACE_VARIANT, no_wrap=True)
        track = ft.Stack(
            [
                rail,
                fill_glow,
                fill,
                *tick_controls,
                thumb_glow,
                thumb,
                ft.Container(content=top_endpoint, left=endpoint_left, top=2),
                ft.Container(content=bottom_endpoint, left=endpoint_left, top=track_height - 13),
            ],
            width=track_width,
            height=track_height,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

        def set_value(raw_value, update_controls=True):
            try:
                fraction = self._vertical_magnetic_scale_fraction(raw_value, state.minimum, state.maximum)
                state.value = round(
                    state.minimum
                    + round(fraction * state.divisions)
                    * (state.maximum - state.minimum)
                    / state.divisions,
                    3,
                )
            except (TypeError, ValueError):
                return
            self._paint_m3_vertical_magnetic_parameter_control(state, update_controls=update_controls)

        def set_from_pointer(event, *, force_visual=False, mirror_keycap=True):
            # See the active legacy ruler: a skipped raw sample must not make
            # Flet auto-update the page root after this callback returns.
            self._consume_magnetic_pointer_event()
            if (
                not bool(getattr(state, "interaction_enabled", True))
                or bool(getattr(self, "_magnetic_parameter_mode_transition", False))
                or getattr(getattr(state, "control", None), "visible", True) is False
            ):
                return False
            position = getattr(event, "local_position", None)
            if position is not None and getattr(position, "y", None) is not None:
                y = float(position.y)
            else:
                delta = getattr(event, "primary_delta", None)
                if delta is None:
                    return False
                y = state.last_pointer_y + float(delta)
            state.last_pointer_y = max(0.0, min(y, track_height))
            fraction = self._vertical_magnetic_pointer_fraction(
                state.last_pointer_y, track_height, fills_from_top=state.fills_from_top
            )
            # Update the authoritative local value on every sample, but only
            # publish the small vertical card at the shared 24-fps cadence.
            # In particular, do not construct packets or reset debounce
            # timers here.  When a visual paint is due, mirror only the
            # changed corner Text on the selected keycap as well.
            set_value(
                state.minimum + fraction * (state.maximum - state.minimum),
                update_controls=False,
            )
            painted = self._flush_magnetic_vertical_drag_paint(
                state, force=force_visual
            )
            if painted and mirror_keycap:
                self._patch_selected_magnetic_keycap_metrics_now()
            return painted

        def start_interaction(event):
            already_active = bool(getattr(state, "interaction_active", False))
            state.interaction_active = True
            return set_from_pointer(event, force_visual=not already_active)

        def end_interaction(event=None):
            self._consume_magnetic_pointer_event()
            if not bool(getattr(state, "interaction_active", False)):
                return
            if event is not None:
                # ``end_interaction`` owns the final text-leaf mirror.
                set_from_pointer(event, force_visual=True, mirror_keycap=False)
            state.interaction_active = False
            self._flush_magnetic_vertical_drag_paint(state, force=True)
            self._patch_selected_magnetic_keycap_metrics_now()
            self._commit_magnetic_input_after_frame()

        drag_surface = ft.GestureDetector(
            content=ft.Container(content=track, width=track_width, height=track_height),
            mouse_cursor=ft.MouseCursor.CLICK,
            drag_interval=36,
            on_tap_down=start_interaction,
            on_tap_up=end_interaction,
            on_tap_cancel=end_interaction,
            on_vertical_drag_start=start_interaction,
            on_vertical_drag_update=set_from_pointer,
            on_vertical_drag_end=end_interaction,
            on_vertical_drag_cancel=end_interaction,
        )
        card = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Container(
                                content=ft.Icon(icon, size=16, color=color),
                                width=28,
                                height=28,
                                alignment=ft.Alignment.CENTER,
                                bgcolor=ft.Colors.with_opacity(0.13, color),
                                border_radius=9,
                            ),
                            ft.Column([title_text, supporting], spacing=0, expand=True),
                        ],
                        spacing=7,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            ft.Row([value_text, unit_text], spacing=3, tight=True),
                            ft.Row([decrease_button, increase_button], spacing=2, tight=True),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Container(content=drag_surface, alignment=ft.Alignment.CENTER, height=track_height),
                ],
                spacing=5,
                tight=True,
            ),
            width=208,
            height=278,
            padding=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=20,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            # Do not animate this outer ruler surface.  A drag can publish
            # several local frames per second; a 160-ms implicit container
            # animation for every frame keeps overlapping Flutter compositor
            # work alive and turns an otherwise local rail patch into visible
            # GPU churn.  The +/- IconButtons retain their Material state
            # feedback and the contained fill/glow remain static between
            # actual value changes.
        )
        state.title_text = title_text
        state.supporting = supporting
        state.value_text = value_text
        state.unit_text = unit_text
        state.decrease_button = decrease_button
        state.increase_button = increase_button
        state.card = card
        state.control = card
        state.track = track
        state.rail = rail
        state.fill = fill
        state.fill_glow = fill_glow
        state.thumb = thumb
        state.thumb_glow = thumb_glow
        state.tick_controls = tick_controls
        state.top_endpoint = top_endpoint
        state.bottom_endpoint = bottom_endpoint
        state.set_value = set_value
        set_value(value, update_controls=False)
        return value_text, state, card

    def _paint_m3_vertical_magnetic_parameter_control(self, state, *, update_controls=True):
        """Apply one vertical M3 slider update without touching the key deck."""
        interactive = bool(getattr(state, "interaction_enabled", True))
        raw_value = float(state.value)
        neutral_startup = (not interactive) and raw_value <= 0.0
        value = 0.0 if neutral_startup else max(
            float(state.minimum), min(float(state.maximum), raw_value)
        )
        if not neutral_startup:
            state.value = value
        fraction = self._vertical_magnetic_scale_fraction(value, state.minimum, state.maximum)
        fill_height = 0 if neutral_startup else max(0, round(state.track_height * fraction))
        state.fill.height = fill_height
        state.fill_glow.height = fill_height
        thumb_position = max(
            0,
            min(
                state.track_height - state.thumb_height,
                round(fill_height - state.thumb_height / 2),
            ),
        )
        glow_position = max(
            0,
            min(state.track_height - state.thumb_glow.height, round(fill_height - state.thumb_glow.height / 2)),
        )
        if state.fills_from_top:
            state.thumb.top = thumb_position
            state.thumb.bottom = None
            state.thumb_glow.top = glow_position
            state.thumb_glow.bottom = None
        else:
            state.thumb.bottom = thumb_position
            state.thumb.top = None
            state.thumb_glow.bottom = glow_position
            state.thumb_glow.top = None
        state.value_text.value = f"{value / 100:.2f}"
        state.decrease_button.disabled = (not interactive) or value <= state.minimum
        state.increase_button.disabled = (not interactive) or value >= state.maximum
        if not interactive:
            state.card.opacity = 0.58
            state.card.bgcolor = ft.Colors.SURFACE_CONTAINER_LOW
            state.card.border = ft.Border.all(1, ft.Colors.OUTLINE_VARIANT)
        elif bool(getattr(state, "interaction_active", False)):
            state.card.opacity = 1.0
            state.card.bgcolor = ft.Colors.with_opacity(0.54, state.color)
            state.card.border = ft.Border.all(1, ft.Colors.with_opacity(0.68, state.color))
        else:
            state.card.opacity = 1.0
            state.card.bgcolor = ft.Colors.SURFACE_CONTAINER_LOW
            state.card.border = ft.Border.all(1, ft.Colors.OUTLINE_VARIANT)
        self._refresh_magnetic_travel_visualization(update=False)
        if not update_controls or not getattr(state, "mounted", False):
            return
        try:
            state.card.update()
        except Exception:
            pass
        # The linked travel illustration is useful at rest, but it is a
        # second rich subtree.  Hold its Flet patch until a drag finishes so
        # the active ruler remains the only GPU work at pointer cadence.
        if not bool(getattr(state, "interaction_active", False)):
            self._refresh_magnetic_travel_visualization(update=True)

    def _paint_m3_magnetic_parameter_control(self, state, *, update_controls=True):
        """Patch one Material 3 parameter surface without redrawing the deck."""
        interactive = bool(getattr(state, "interaction_enabled", True))
        raw_value = float(state.value)
        # The neutral startup state intentionally displays 0.00 mm even for
        # controls whose firmware minimum is 0.01/0.10.  The native Slider
        # still receives its legal minimum under the thumb, but it is disabled
        # until the physical keyboard read replaces this visual placeholder.
        neutral_startup = (not interactive) and raw_value <= 0.0
        value = (
            0.0
            if neutral_startup
            else max(float(state.minimum), min(float(state.maximum), raw_value))
        )
        if not neutral_startup:
            state.value = value
        state.value_text.value = f"{value / 100:.2f}"
        # While the native M3 Slider is under a pointer, Flutter already owns
        # the thumb position.  Echoing the Python value back on every
        # ``on_change`` creates a feedback patch and makes the thumb catch.
        # Programmatic loads/final paints still set it normally.
        if not bool(getattr(state, "interaction_active", False)):
            state.slider.value = state.minimum if neutral_startup else value
        state.slider.disabled = not interactive
        state.decrease_button.disabled = (not interactive) or value <= state.minimum
        state.increase_button.disabled = (not interactive) or value >= state.maximum
        # M3 state feedback: focus/drag subtly lifts the current surface and
        # tints it with the parameter colour.  It is local to this card and
        # never calls page.update(), so a live slider gesture stays fluid.
        if not interactive:
            state.card.opacity = 0.58
            state.card.bgcolor = ft.Colors.SURFACE_CONTAINER_LOW
            state.card.border = ft.Border.all(1, ft.Colors.OUTLINE_VARIANT)
        elif bool(getattr(state, "interaction_active", False)):
            state.card.opacity = 1.0
            state.card.bgcolor = ft.Colors.with_opacity(0.56, state.color)
            state.card.border = ft.Border.all(1, ft.Colors.with_opacity(0.68, state.color))
        else:
            state.card.opacity = 1.0
            state.card.bgcolor = ft.Colors.SURFACE_CONTAINER_LOW
            state.card.border = ft.Border.all(1, ft.Colors.OUTLINE_VARIANT)

        self._refresh_magnetic_travel_visualization(update=False)
        if not update_controls or not getattr(state, "mounted", False):
            return
        try:
            # A single card update is intentionally cheaper than updating the
            # page or the 81-key visual layout on each slider frame.
            state.card.update()
        except Exception:
            pass
        if not bool(getattr(state, "interaction_active", False)):
            self._refresh_magnetic_travel_visualization(update=True)

    def _set_m3_magnetic_parameter_copy(self, state, title, supporting_text, *, update=False):
        """Rename a persistent M3 card or compact legacy ruler in place.

        Keeping this narrow compatibility helper lets the RT mode switch
        retain its safe, one-parent update boundary while the visible scales
        use the lighter legacy ruler presentation again.
        """
        if getattr(state, "presentation", None) not in {
            "m3_parameter", "m3_vertical_parameter"
        }:
            state.label = str(title)
            label_text = getattr(state, "label_text", None)
            if label_text is not None:
                label_text.value = state.label
                if update:
                    try:
                        heading = getattr(state, "heading", None)
                        (heading or label_text).update()
                    except Exception:
                        pass
            return
        state.title = str(title)
        state.supporting_text = str(supporting_text)
        state.title_text.value = state.title
        state.supporting.value = state.supporting_text
        if update:
            try:
                state.card.update()
            except Exception:
                pass

    def _build_magnetic_travel_visualization(self):
        """Build the key-travel view as a vertical tester-style meter.

        The green fill is the ordinary Rapid Trigger threshold.  The cyan and
        yellow threshold bars stay linked to the activation/deactivation cards,
        so the relation between all values is visible without turning the
        settings back into five technical rulers.
        """
        full_travel = float(MagneticProtocol.OFFICIAL_SK75_ACTUATION_MAX_MM)
        track_height = 224
        track_width = 202
        rail_width = 48
        rail_left = 16
        ruler_left = 82
        marker_height = 5
        self.magnetic_travel_full_mm = full_travel
        self.magnetic_travel_track_height = track_height
        self.magnetic_travel_marker_height = marker_height
        self.magnetic_travel_rail_left = rail_left

        rail = ft.Container(
            width=rail_width,
            height=track_height,
            left=rail_left,
            top=0,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            border_radius=8,
        )
        rapid_fill_glow = ft.Container(
            width=rail_width,
            left=rail_left,
            top=0,
            gradient=ft.LinearGradient(
                begin=ft.Alignment.CENTER_LEFT,
                end=ft.Alignment.CENTER_RIGHT,
                colors=[
                    ft.Colors.TRANSPARENT,
                    ft.Colors.with_opacity(0.36, ft.Colors.GREEN_300),
                    ft.Colors.TRANSPARENT,
                ],
            ),
            border_radius=7,
        )
        rapid_fill = ft.Container(
            width=rail_width - 8,
            left=rail_left + 4,
            top=0,
            gradient=ft.LinearGradient(
                begin=ft.Alignment.TOP_CENTER,
                end=ft.Alignment.BOTTOM_CENTER,
                colors=[ft.Colors.GREEN_300, ft.Colors.GREEN_800],
            ),
            border_radius=5,
        )

        def marker(color, tooltip):
            return ft.Container(
                width=rail_width + 6,
                height=marker_height,
                left=rail_left - 3,
                top=0,
                bgcolor=color,
                border=ft.Border.all(1, ft.Colors.with_opacity(0.68, ft.Colors.ON_SURFACE)),
                border_radius=1,
                tooltip=tooltip,
                animate_position=ft.Animation(130, ft.AnimationCurve.EASE_OUT),
            )

        activation_marker = marker(
            MAGNETIC_METRIC_COLORS["actuation"], "Точка активации"
        )
        deactivation_marker = marker(
            MAGNETIC_METRIC_COLORS["rapid_release"], "RT при отпускании / точка деактивации"
        )
        ticks = []
        # Match the readable cadence of the travel tester: 0.5-mm majors and
        # the exact official 3.30-mm endpoint.
        tick_values = [round(index * 0.5, 2) for index in range(7)] + [full_travel]
        for tick_value in tick_values:
            fraction = max(0.0, min(1.0, tick_value / full_travel))
            y = round(fraction * (track_height - 1))
            ticks.extend(
                [
                    ft.Container(
                        width=16 if tick_value not in {0.0, full_travel} else 26,
                        height=1,
                        left=ruler_left,
                        top=y,
                        bgcolor=ft.Colors.with_opacity(0.58, ft.Colors.ON_SURFACE_VARIANT),
                    ),
                    ft.Container(
                        content=ft.Text(
                            f"{tick_value:.2f} мм",
                            size=8,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                            no_wrap=True,
                        ),
                        left=ruler_left + 24,
                        top=max(0, min(track_height - 12, y - 5)),
                    ),
                ]
            )
        self.magnetic_travel_activation_marker = activation_marker
        self.magnetic_travel_deactivation_marker = deactivation_marker
        # Keep the historical name as an alias for integrations/tests, but the
        # green layer is deliberately the Rapid Trigger feedback now.
        self.magnetic_travel_fill = rapid_fill
        self.magnetic_travel_rapid_fill = rapid_fill
        self.magnetic_travel_rapid_fill_glow = rapid_fill_glow
        self.magnetic_travel_activation_caption = ft.Text(
            size=10, weight=ft.FontWeight.W_600, color=MAGNETIC_METRIC_COLORS["actuation"]
        )
        self.magnetic_travel_deactivation_caption = ft.Text(
            size=10, weight=ft.FontWeight.W_600, color=MAGNETIC_METRIC_COLORS["rapid_release"]
        )
        self.magnetic_travel_mode_caption = ft.Text(
            size=9,
            color=ft.Colors.ON_SURFACE_VARIANT,
            max_lines=2,
        )
        self.magnetic_travel_visual_stack = ft.Stack(
            [rail, rapid_fill_glow, rapid_fill, *ticks, activation_marker, deactivation_marker],
            width=track_width,
            height=track_height,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        self.magnetic_travel_visualization = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.SHOW_CHART_ROUNDED, size=18, color=ft.Colors.GREEN_300),
                            ft.Text("Ход клавиши", size=14, weight=ft.FontWeight.W_600),
                        ],
                        spacing=8,
                    ),
                    ft.Text(
                        "Зелёная шкала — Rapid Trigger",
                        size=10,
                        color=ft.Colors.GREEN_300,
                    ),
                    self.magnetic_travel_visual_stack,
                    self.magnetic_travel_activation_caption,
                    self.magnetic_travel_deactivation_caption,
                    self.magnetic_travel_mode_caption,
                ],
                spacing=5,
                tight=True,
            ),
            width=258,
            padding=14,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=20,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        self._refresh_magnetic_travel_visualization(update=False)
        return self.magnetic_travel_visualization

    def _refresh_magnetic_travel_visualization(self, *, update=True):
        """Patch the vertical tester-style travel meter from current controls."""
        marker = getattr(self, "magnetic_travel_activation_marker", None)
        if marker is None:
            return
        try:
            full_travel = float(self.magnetic_travel_full_mm)
            track_height = float(self.magnetic_travel_track_height)
            marker_height = float(self.magnetic_travel_marker_height)
            activation = float(self.magnetic_actuation_slider.value) / 100.0
            deactivation = float(self.magnetic_rt_release_slider.value) / 100.0
            rt_enabled = bool(self.magnetic_rt_switch.value)
        except (AttributeError, TypeError, ValueError):
            return

        def position(value):
            fraction = max(0.0, min(1.0, value / max(0.01, full_travel)))
            return round(fraction * (track_height - marker_height))

        self.magnetic_travel_activation_marker.top = position(activation)
        self.magnetic_travel_deactivation_marker.top = position(deactivation)
        rapid_height = max(0, round((deactivation / max(0.01, full_travel)) * track_height))
        self.magnetic_travel_rapid_fill.height = rapid_height if rt_enabled else 0
        self.magnetic_travel_rapid_fill_glow.height = rapid_height if rt_enabled else 0
        self.magnetic_travel_rapid_fill.opacity = 1.0 if rt_enabled else 0.18
        self.magnetic_travel_rapid_fill_glow.opacity = 1.0 if rt_enabled else 0.0
        self.magnetic_travel_activation_caption.value = f"Активация {activation:.2f} мм"
        if rt_enabled:
            self.magnetic_travel_deactivation_caption.value = f"RT при отпускании {deactivation:.2f} мм"
            self.magnetic_travel_mode_caption.value = (
                "Зелёная заливка показывает RT; цветные линии — пороги карточек."
            )
        else:
            self.magnetic_travel_deactivation_caption.value = f"Деактивация {deactivation:.2f} мм"
            self.magnetic_travel_mode_caption.value = (
                "Деактивация сохранена для следующего включения Rapid Trigger."
            )
        if not update:
            return
        try:
            self.magnetic_travel_visualization.update()
        except Exception:
            pass

    def _patch_magnetic_parameter_panel(self):
        """Patch the selected-key editor through one stable parent.

        Flet's deferred ``Session.schedule_update`` is useful for declarative
        components, but this editor intentionally mutates a persistent set of
        controls in a synchronous native-switch callback.  Queuing the parent
        while another callback changes a child's visibility leaves the Flet
        diff worker holding a live dictionary.  That was the source of the
        intermittent ``dictionary changed size during iteration`` crash after
        repeated RT / separate-threshold clicks.

        Keep the mutation and its patch in the same event turn instead.  This
        helper is deliberately the *only* broad selected-key-panel patch; live
        slider drags still repaint only their small fixed ruler wrapper.
        """
        panel = getattr(self, "magnetic_parameter_panel", None)
        if panel is not None:
            try:
                panel.update()
                return True
            except Exception:
                logger.debug("could not patch magnetic parameter panel", exc_info=True)
                return False

        # Detached construction/test managers do not own the parent panel yet.
        # Keep the narrow compatibility paint for their persistent mode card.
        surface = getattr(self, "magnetic_parameter_mode_surface", None)
        if surface is not None:
            try:
                surface.update()
                return True
            except Exception:
                pass
        return False

    def _update_magnetic_parameter_mode_ui(self, *, update=False):
        """Switch persistent compact rulers between RT and ordinary modes.

        The controls are never recreated: only their labels and visibility
        change.  This preserves all selected-key values and avoids a page or
        keyboard-deck rebuild when the user flips Rapid Trigger.
        """
        if not hasattr(self, "magnetic_actuation_slider"):
            return
        rt_enabled = bool(self.magnetic_rt_switch.value)
        if rt_enabled:
            mode_title = "Rapid Trigger"
            mode_description = "Динамический сброс и повторное срабатывание по ходу клавиши."
            mode_badge = "ВКЛ"
            mode_badge_color = ft.Colors.ON_PRIMARY_CONTAINER
            mode_badge_background = ft.Colors.PRIMARY_CONTAINER
            release_title = "RT при отпускании"
            if getattr(self.magnetic_rt_release_slider, "presentation", None) not in {
                "m3_parameter", "m3_vertical_parameter"
            }:
                release_title = MAGNETIC_SCALE_ROLE_LABELS["rapid_release"]
            self._set_m3_magnetic_parameter_copy(
                self.magnetic_actuation_slider,
                MAGNETIC_SCALE_ROLE_LABELS["actuation"],
                "Точка срабатывания клавиши",
            )
            self._set_m3_magnetic_parameter_copy(
                self.magnetic_rt_release_slider,
                release_title,
                "Сброс при движении клавиши вверх",
            )
        else:
            # Keep the mode name stable.  The badge is the state, so changing
            # the title to “ordinary thresholds” made it look as though this
            # switch controlled a different feature instead of Rapid Trigger.
            mode_title = "Rapid Trigger"
            mode_description = "Выключен: используйте обычные точки активации и деактивации."
            mode_badge = "ВЫКЛ"
            mode_badge_color = ft.Colors.ON_SURFACE_VARIANT
            mode_badge_background = ft.Colors.SURFACE_CONTAINER_HIGHEST
            activation_title = "Точка активации"
            if getattr(self.magnetic_actuation_slider, "presentation", None) not in {
                "m3_parameter", "m3_vertical_parameter"
            }:
                activation_title = "Точка\nактивации"
            self._set_m3_magnetic_parameter_copy(
                self.magnetic_actuation_slider,
                activation_title,
                "Срабатывание при нажатии клавиши",
            )
            deactivation_slider = getattr(self, "magnetic_deactivation_slider", None)
            if deactivation_slider is not None:
                self._set_m3_magnetic_parameter_copy(
                    deactivation_slider,
                    "Точка\nдеактивации",
                    "Отпускание клавиши без Rapid Trigger",
                )
            # Production always owns a separate normal-mode ruler.  Retain a
            # small fallback for detached/test managers created before that
            # control exists: their single release ruler can still be read as
            # the ordinary deactivation threshold without changing its value.
            if getattr(self, "magnetic_deactivation_control", None) is None:
                self._set_m3_magnetic_parameter_copy(
                    self.magnetic_rt_release_slider,
                    "Точка деактивации",
                    "Порог отпускания без Rapid Trigger",
                )
        mode_title_control = getattr(self, "magnetic_parameter_mode_title", None)
        if mode_title_control is not None:
            self.magnetic_parameter_mode_title.value = mode_title
            self.magnetic_parameter_mode_description.value = mode_description
            self.magnetic_parameter_mode_badge_text.value = mode_badge
            self.magnetic_parameter_mode_badge_text.color = mode_badge_color
            self.magnetic_parameter_mode_badge.bgcolor = mode_badge_background
            self.magnetic_parameter_mode_surface.bgcolor = (
                ft.Colors.SURFACE_CONTAINER_LOW
                if rt_enabled
                else ft.Colors.SURFACE_CONTAINER
            )
        separate_rt = rt_enabled and bool(self.magnetic_rt_separate_switch.value)
        deactivation_switch = getattr(
            self, "magnetic_deactivation_separate_switch", None
        )
        # Detached compatibility/test instances created before the toggle was
        # added retain the old behaviour: their available deactivation ruler
        # stays visible in normal mode.
        separate_deactivation = (
            bool(getattr(deactivation_switch, "value", True))
            if not rt_enabled
            else False
        )
        # This is a flex anchor between two permanent Rows, rather than a
        # conditional placeholder inside the ruler sequence.  It must remain
        # laid out in every mode so the right-side dead-zone group keeps its
        # physical x-position.  The explicit assignment also keeps detached
        # test/mount instances consistent with the live construction state.
        dead_zone_anchor = getattr(self, "magnetic_dead_zone_spacer", None)
        if dead_zone_anchor is not None:
            dead_zone_anchor.visible = True
            dead_zone_anchor.opacity = 0.0
        for control, visible in (
            (getattr(self, "magnetic_rt_separate_surface", None), rt_enabled),
            (getattr(self, "magnetic_deactivation_separate_surface", None), not rt_enabled),
            (getattr(self, "magnetic_rt_release_control", None), rt_enabled),
            (getattr(self, "magnetic_rt_press_control", None), separate_rt),
            # Dead zones are physical travel limits, not an RT-only setting.
            # Keep them available in ordinary mode too, next to activation
            # and the optional independent deactivation point.
            (getattr(self, "magnetic_lower_dead_zone_control", None), True),
            (getattr(self, "magnetic_upper_dead_zone_control", None), True),
            (
                getattr(self, "magnetic_deactivation_control", None),
                (not rt_enabled) and separate_deactivation,
            ),
        ):
            if control is None:
                continue
            control.visible = visible
            control.opacity = 1.0 if visible else 0.0
        # Do not update each child while Flet is reconciling a switch event.
        # A rapid RT toggle during scroll used to make the framework iterate a
        # mutable control map and fail with "dictionary changed size during
        # iteration".  Mutate the whole view first, then send exactly one
        # immediate patch for its stable parent surface.  In particular, do
        # *not* use Session.schedule_update here: the deferred callback can
        # race the next native Switch event with a partially changed child map.
        self._refresh_magnetic_travel_visualization(update=False)
        if update:
            self._patch_magnetic_parameter_panel()

    def _set_vertical_magnetic_value(self, state, value, *, update_controls=True):
        # Loading a stale local preset must not leave a thumb beyond the
        # official min/max rail.  This is local UI clamping only; it does not
        # schedule or send a HID write.
        if getattr(state, "presentation", None) in {
            "m3_parameter", "m3_vertical_parameter"
        }:
            setter = getattr(state, "set_value", None)
            if callable(setter):
                setter(value, update_controls=update_controls)
                return
        state.value = max(
            state.minimum,
            min(state.maximum, round(float(value), 3)),
        )
        self._paint_vertical_magnetic_control(state, update_controls=update_controls)

    def _adjust_vertical_magnetic_value(self, state, direction):
        """Move one ruler division and use the normal live-save pipeline.

        Mouse dragging is intentionally continuous, so it is useful for
        broad adjustment but awkward for exact values such as ``1.20 мм``.
        The physical protocol represents these controls in 0.01-mm steps;
        asking the state for its division keeps the +/- buttons exact even if
        a future firmware range uses a different number of divisions.
        """
        # A disabled/endpoint click must not fall through to Flet's implicit
        # whole-page after-event update either.  The one small ruler paint
        # below is the only UI work this callback is allowed to do directly.
        self._consume_magnetic_pointer_event()
        if (
            not bool(getattr(state, "interaction_enabled", True))
            or bool(getattr(self, "_magnetic_parameter_mode_transition", False))
            or getattr(getattr(state, "control", None), "visible", True) is False
        ):
            return
        try:
            direction = 1 if int(direction) > 0 else -1
            step = float(state.step)
            target = max(
                float(state.minimum),
                min(
                    float(state.maximum),
                    round(float(state.value) + direction * step, 6),
                ),
            )
        except (AttributeError, TypeError, ValueError):
            return

        # Flet can deliver one already-queued click after a button becomes
        # disabled at MIN/MAX.  Do not start a debounce/keycap timer when the
        # quantised ruler value cannot actually change.
        if abs(float(target) - float(state.value)) < 1e-9:
            return

        setter = getattr(state, "set_value", None)
        if callable(setter):
            setter(target)
        else:
            # Detached test managers can construct a plain ruler state.  The
            # same bounded paint still makes the fallback safe and visible.
            self._set_vertical_magnetic_value(state, target)
        # Mirror the new number onto the selected keyboard key right now.
        # This is deliberately a *text-leaf* patch: it never redraws the key
        # shell or the whole 75% deck, so the layout no longer appears to
        # lag behind a precise +/- adjustment.  Packet construction, config
        # locking and HID debounce remain after-frame below.
        self._patch_selected_magnetic_keycap_metrics_now()
        self._commit_magnetic_input_after_frame()

    def _synchronize_magnetic_preview_thresholds(self):
        """Keep hidden linked thresholds truthful before a keycap text paint.

        The visible three corner values must agree with the active editor on
        the same frame.  In particular, an ordinary release threshold mirrors
        to repeat-down RT while its separate switch is off, and ordinary
        deactivation mirrors activation while its own separate switch is off.
        This performs only in-memory ruler updates with ``update_controls``
        disabled; it cannot send HID packets or trigger another Flet patch.
        """
        try:
            separate_rt = bool(
                getattr(getattr(self, "magnetic_rt_separate_switch", None), "value", False)
            )
            if not separate_rt:
                release = getattr(self, "magnetic_rt_release_slider", None)
                press = getattr(self, "magnetic_rt_press_slider", None)
                if release is not None and press is not None:
                    self._set_vertical_magnetic_value(
                        press, release.value, update_controls=False
                    )

            rt_enabled = bool(
                getattr(getattr(self, "magnetic_rt_switch", None), "value", True)
            )
            separate_deactivation = bool(
                getattr(
                    getattr(self, "magnetic_deactivation_separate_switch", None),
                    "value",
                    True,
                )
            )
            if not rt_enabled and not separate_deactivation:
                activation = getattr(self, "magnetic_actuation_slider", None)
                deactivation = getattr(self, "magnetic_deactivation_slider", None)
                if activation is not None and deactivation is not None:
                    self._set_vertical_magnetic_value(
                        deactivation, activation.value, update_controls=False
                    )
        except (AttributeError, TypeError, ValueError):
            # Detached/lightweight managers can omit the linked controls.  A
            # subsequent normal settings read still supplies the right value.
            pass

    def _patch_selected_magnetic_keycap_metrics_now(self):
        """Immediately copy live thresholds into only the cap metric texts.

        The keycap renderer keeps references to its three corner ``Text``
        controls.  ``_patch_magnetic_picker_keycap`` detects the changed
        corner and updates that leaf rather than the keycap container, making
        this safe inside a slider event and visually immediate.
        """
        slot = getattr(self, "magnetic_selected_slot", None)
        if (
            slot not in SK75_KEY_BY_SLOT
            or bool(getattr(self, "_magnetic_parameter_mode_transition", False))
        ):
            return False
        try:
            self._synchronize_magnetic_preview_thresholds()
            settings = self._magnetic_settings_from_controls()
            return bool(
                self._patch_magnetic_picker_keycap(
                    slot,
                    selected=(
                        getattr(self, "magnetic_visual_selected_slot", None) == slot
                    ),
                    settings=settings,
                )
            )
        except (
            AttributeError,
            TypeError,
            ValueError,
            MagneticProtocolError,
        ):
            # The keyboard deck can be detached during its first mount.  The
            # next normal mount sees the same in-memory thresholds.
            return False

    def _commit_magnetic_input_after_frame(self):
        """Move non-visual ruler work to the next Flet event-loop turn.

        The button/drag callback has already painted its tiny ruler leaf.  Do
        not synchronously calculate the key packet, acquire configuration
        locks or patch the keycap before returning from that input event: a
        save/cache worker may briefly hold one of those locks and make exact
        +/- clicks feel delayed.  A latest-wins UI-loop task performs that
        ordinary work after the input handler yields.
        """
        slot = getattr(self, "magnetic_selected_slot", None)
        # Lightweight test managers do not own a Flet Page.  Keep their
        # callback contract synchronous while production always takes the
        # after-frame branch below.
        if getattr(self, "page", None) is None:
            self._run_magnetic_input_commit_after_frame(slot, None)
            return

        lock = getattr(self, "_magnetic_input_commit_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._magnetic_input_commit_lock = lock
            self._magnetic_input_commit_revision = 0
        with lock:
            revision = int(getattr(self, "_magnetic_input_commit_revision", 0)) + 1
            self._magnetic_input_commit_revision = revision

        ui_call = getattr(self, "_ui_call", None)
        if callable(ui_call):
            ui_call(lambda: self._run_magnetic_input_commit_after_frame(slot, revision))

    def _run_magnetic_input_commit_after_frame(self, slot, revision):
        """Run the latest non-visual edit work on Flet's page event loop."""
        if revision is not None:
            lock = getattr(self, "_magnetic_input_commit_lock", None)
            if lock is None:
                return
            with lock:
                if int(getattr(self, "_magnetic_input_commit_revision", 0)) != revision:
                    return
        # A key/profile/mode operation can win the race while this callback
        # waits its turn.  Never send the former key's value after that.
        if slot in SK75_KEY_BY_SLOT and getattr(self, "magnetic_selected_slot", None) != slot:
            return
        changed = getattr(self, "_on_magnetic_control_changed", None)
        if callable(changed):
            try:
                parameters = inspect.signature(changed).parameters.values()
                accepts_keyword = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    or parameter.name == "refresh_keycap"
                    for parameter in parameters
                )
            except (TypeError, ValueError):
                accepts_keyword = True
            if accepts_keyword:
                changed(refresh_keycap=False)
            else:
                changed()
        # The slider event already copied the changed corner text directly.
        # Do not enqueue a second, delayed keycap pass: it would make the
        # visible number arrive one frame late after a rapid adjustment.

    def _schedule_magnetic_keycap_refresh(self, slot):
        """Mirror a completed ruler edit on its keycap after the input turn.

        This method never mutates Flet controls from a worker thread.  It
        queues one short coroutine onto Flet's page loop, which necessarily
        runs after the native button/drag handler has yielded.  The revision
        guard lets a burst of input discard stale queued cap paints before
        they reach the keyboard deck.  HID scheduling is intentionally
        separate and remains immediate/latest-wins.
        """
        if slot not in SK75_KEY_BY_SLOT:
            return
        lock = getattr(self, "_magnetic_keycap_refresh_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._magnetic_keycap_refresh_lock = lock
            self._magnetic_keycap_refresh_revision = 0
        with lock:
            revision = int(getattr(self, "_magnetic_keycap_refresh_revision", 0)) + 1
            self._magnetic_keycap_refresh_revision = revision
        ui_call = getattr(self, "_ui_call", None)
        if callable(ui_call):
            ui_call(lambda: self._run_magnetic_keycap_refresh(slot, revision))

    def _run_magnetic_keycap_refresh(self, slot, revision):
        """Paint one latest cap mirror on Flet's own event loop."""
        lock = getattr(self, "_magnetic_keycap_refresh_lock", None)
        if lock is None:
            return
        with lock:
            if int(getattr(self, "_magnetic_keycap_refresh_revision", 0)) != revision:
                return
        if bool(getattr(self, "_magnetic_parameter_mode_transition", False)):
            return
        if getattr(self, "magnetic_selected_slot", None) != slot:
            return
        try:
            settings = self._magnetic_settings_from_controls()
            self._patch_magnetic_picker_keycap(
                slot,
                selected=(
                    getattr(self, "magnetic_visual_selected_slot", None)
                    == slot
                ),
                settings=settings,
            )
        except (
            AttributeError,
            TypeError,
            ValueError,
            MagneticProtocolError,
        ):
            # The deck can detach during a profile/key change.  Its next
            # normal paint reads the authoritative control values.
            pass

    # ---------- Magnetic presets (application-side, not firmware slots) ----------
    def _selected_magnetic_profile_index(self):
        """Return the selected local magnetic-preset slot for this device."""
        # ``magnetic_profile_index`` is the source of truth after a Flet select
        # event.  Dropdown.value can remain one frame behind while controls are
        # being rebuilt, which was enough to make profile 2 appear identical to
        # profile 1 in the old implementation.
        raw_value = getattr(self, "magnetic_profile_index", None)
        if raw_value is None:
            raw_value = getattr(getattr(self, "magnetic_profile_dropdown", None), "value", None)
        if raw_value is None:
            raw_value = (self._active_device() or {}).get("magnetic_selected_profile", 0)
        try:
            index = int(raw_value)
        except (TypeError, ValueError):
            index = 0
        return max(0, min(MAGNETIC_PROFILE_COUNT - 1, index))

    def _magnetic_profile_slot(self, index=None):
        """Return a normalized local magnetic preset for the active device."""
        # This helper can run from a Flet event while a debounced HID writer
        # updates the same local preset.  Normalization may create nested maps,
        # so it belongs to the config transaction too rather than being a
        # seemingly read-only accessor.
        with _CONFIG_WRITE_LOCK:
            entry = self._active_device()
            if entry is None:
                return None
            if not isinstance(entry.get("magnetic_profiles"), dict):
                _normalize_magnetic_profile_slots(entry)
            if index is None:
                index = self._selected_magnetic_profile_index()
            try:
                index = int(index)
            except (TypeError, ValueError):
                index = 0
            index = max(0, min(MAGNETIC_PROFILE_COUNT - 1, index))
            slot = entry["magnetic_profiles"].get(str(index))
            if not isinstance(slot, dict):
                _normalize_magnetic_profile_slots(entry)
                slot = entry["magnetic_profiles"][str(index)]
            return slot

    def _ensure_womier_cache_sync_state(self):
        """Return lazy state used to mirror successful HID writes to Womier.

        The normal UI creates this state with Magnetic Lab.  Keeping this
        small fallback makes isolated diagnostic/test manager instances safe
        too, without coupling cache mirroring to Flet controls.
        """
        lock = getattr(self, "_womier_cache_sync_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._womier_cache_sync_lock = lock
            self._womier_cache_sync_pending = {}
            self._womier_cache_sync_timer = None
            self._womier_cache_sync_revision = 0
        return lock

    def _ensure_magnetic_persistence_state(self):
        """Return the lazy quiet-period gate for magnetic disk writes.

        Small isolated test managers do not build Magnetic Lab, whereas the
        real app creates this state together with the sliders.  Keep the
        fallback here so a successful background HID write can still persist
        safely without reaching into the Flet control tree.
        """
        lock = getattr(self, "_magnetic_persistence_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._magnetic_persistence_lock = lock
            self._magnetic_persistence_timer = None
            self._magnetic_persistence_revision = 0
            self._magnetic_persistence_pending = False
        return lock

    def _schedule_magnetic_persistence(
        self, delay=MAGNETIC_BACKGROUND_PERSIST_DEBOUNCE_SEC
    ):
        """Persist accepted magnetic edits once after the user pauses.

        This does *not* defer HID.  The keyboard and in-memory cache have
        already been updated by the time this is called.  It only prevents a
        rapid sequence of +/- clicks from issuing repeated JSON serialise /
        fsync operations and Womier-cache bookkeeping while the next Flet
        frame is trying to paint the value.
        """
        lock = self._ensure_magnetic_persistence_state()
        try:
            quiet_delay = max(0.0, float(delay))
        except (TypeError, ValueError):
            quiet_delay = MAGNETIC_BACKGROUND_PERSIST_DEBOUNCE_SEC
        with lock:
            self._magnetic_persistence_revision = (
                int(getattr(self, "_magnetic_persistence_revision", 0)) + 1
            )
            revision = self._magnetic_persistence_revision
            self._magnetic_persistence_pending = True
            previous = getattr(self, "_magnetic_persistence_timer", None)
            if previous is not None:
                try:
                    previous.cancel()
                except Exception:
                    pass

            # Use a closure rather than Timer args so lightweight tests which
            # replace ``threading.Timer`` with a two-argument fake retain the
            # exact same call shape.
            def flush_current():
                self._flush_magnetic_persistence(expected_revision=revision)

            timer = threading.Timer(quiet_delay, flush_current)
            timer.daemon = True
            self._magnetic_persistence_timer = timer
            timer.start()
        return revision

    def _flush_magnetic_persistence(self, *, expected_revision=None):
        """Write the latest magnetic/cache state exactly once.

        A stale cancelled Timer is harmless: it carries an older revision and
        returns before touching disk.  Holding this narrow gate throughout
        ``save_config`` also lets an explicit quit wait for an in-flight save
        instead of exiting halfway through its atomic file replacement.
        """
        lock = self._ensure_magnetic_persistence_state()
        with lock:
            revision = int(getattr(self, "_magnetic_persistence_revision", 0))
            if expected_revision is not None and expected_revision != revision:
                return False
            if not bool(getattr(self, "_magnetic_persistence_pending", False)):
                return False
            timer = getattr(self, "_magnetic_persistence_timer", None)
            self._magnetic_persistence_timer = None
            self._magnetic_persistence_pending = False
            if timer is not None and expected_revision is None:
                try:
                    timer.cancel()
                except Exception:
                    pass
            try:
                # Magnetic values do not alter the foreground hook/runtime.
                # In particular this must never call keyboard.unhook_all().
                self.save_config(reload_runtime=False)
            except Exception:
                logger.exception("could not persist queued magnetic changes")
                # The in-memory cache remains the latest state.  Mark it
                # dirty again so a later edit, hide or quit retries the write.
                self._magnetic_persistence_pending = True
                return False
        return True

    def _womier_cache_sync_device_key(self):
        config = getattr(self, "config", None)
        if not isinstance(config, dict):
            return ""
        key = config.get("active_device")
        return key if isinstance(key, str) else ""

    def _womier_cache_sync_device_entry(self, device_key):
        config = getattr(self, "config", None)
        devices = config.get("devices") if isinstance(config, dict) else None
        entry = devices.get(device_key) if isinstance(devices, dict) else None
        return entry if isinstance(entry, dict) else None

    @staticmethod
    def _mapping_items_snapshot(mapping, *, attempts=3):
        """Return a short-lived item snapshot without exposing a live view.

        A few callers accept a mapping produced by another worker (for example
        a readback/cache hand-off).  Iterating its live ``dict_items`` view can
        raise ``RuntimeError: dictionary changed size during iteration`` if a
        producer adds the next key between the call and the copy.  The normal
        paths already own their corresponding lock; this defensive boundary
        keeps an unexpected integration/race from taking down the UI or the
        cache worker.  A later HID-success delta will retry any item that is
        still being assembled, so dropping an unstable transient snapshot is
        safer than serialising a partial live view.
        """
        items = getattr(mapping, "items", None)
        if not callable(items):
            return []
        try:
            attempts = max(1, int(attempts))
        except (TypeError, ValueError):
            attempts = 1
        for attempt in range(attempts):
            try:
                return list(items())
            except RuntimeError:
                # Yield once so the producer can complete its atomic dict
                # update before the next detached copy attempt.
                if attempt + 1 < attempts:
                    time.sleep(0)
        logger.debug("mapping changed while taking background cache snapshot")
        return []

    @staticmethod
    def _womier_cache_sync_setting_payload(value):
        """Return a portable, validated key delta, or ``None`` if invalid."""
        if isinstance(value, KeyMagneticSettings):
            return QMKManager._magnetic_settings_to_config(value)
        if isinstance(value, dict):
            normalized = QMKManager._magnetic_settings_from_config(value)
            if normalized is not None:
                return QMKManager._magnetic_settings_to_config(normalized)
        return None

    def _persist_womier_cache_sync_delta(self, device_key, profile_index, job):
        """Store an already-HID-written narrow delta for later safe replay."""
        with _CONFIG_WRITE_LOCK:
            entry = self._womier_cache_sync_device_entry(device_key)
            if entry is None:
                return False
            pending = entry.setdefault("womier_cache_sync_pending", {})
            if not isinstance(pending, dict):
                pending = {}
                entry["womier_cache_sync_pending"] = pending
            stored = pending.setdefault(
                str(profile_index),
                {"key_settings": {}, "key_modes": {}, "rt_stab": None},
            )
            if not isinstance(stored, dict):
                stored = {"key_settings": {}, "key_modes": {}, "rt_stab": None}
                pending[str(profile_index)] = stored
            for name in ("key_settings", "key_modes"):
                if not isinstance(stored.get(name), dict):
                    stored[name] = {}
            changed = False
            for slot, value in (job.get("key_settings") or {}).items():
                if stored["key_settings"].get(slot) != value:
                    stored["key_settings"][slot] = value
                    changed = True
            for slot, value in (job.get("key_modes") or {}).items():
                if stored["key_modes"].get(slot) != value:
                    stored["key_modes"][slot] = value
                    changed = True
            if job.get("rt_stab") is not None and stored.get("rt_stab") != job["rt_stab"]:
                stored["rt_stab"] = job["rt_stab"]
                changed = True
            return changed

    def _clear_persisted_womier_cache_sync_delta(self, device_key, profile_index, job):
        """Drop only values that have not been superseded by a newer HID write."""
        with _CONFIG_WRITE_LOCK:
            entry = self._womier_cache_sync_device_entry(device_key)
            pending = entry.get("womier_cache_sync_pending") if entry else None
            stored = pending.get(str(profile_index)) if isinstance(pending, dict) else None
            if not isinstance(stored, dict):
                return False
            changed = False
            for name in ("key_settings", "key_modes"):
                values = stored.get(name)
                for slot, value in (job.get(name) or {}).items():
                    if isinstance(values, dict) and values.get(slot) == value:
                        values.pop(slot, None)
                        changed = True
            if job.get("rt_stab") is not None and stored.get("rt_stab") == job["rt_stab"]:
                stored["rt_stab"] = None
                changed = True
            if not (stored.get("key_settings") or stored.get("key_modes") or stored.get("rt_stab") is not None):
                pending.pop(str(profile_index), None)
            if not pending:
                entry.pop("womier_cache_sync_pending", None)
            return changed

    def _restore_persisted_womier_cache_sync(self):
        """Resume pre-existing HID-success deltas after a program restart."""
        # A cache-sync timer can remove a persisted delta while startup is
        # restoring it.  Read the small persisted structure under the same
        # configuration lock used by save_config(), then iterate a detached
        # snapshot below.  Without this a worker could resize ``persisted``
        # exactly while this method is walking ``.items()``.
        with _CONFIG_WRITE_LOCK:
            device_key = self._womier_cache_sync_device_key()
            entry = self._womier_cache_sync_device_entry(device_key)
            persisted = entry.get("womier_cache_sync_pending") if entry else None
            if not isinstance(persisted, dict):
                return
            persisted_jobs = [
                (raw_profile, _json_copy(raw_job))
                for raw_profile, raw_job in list(persisted.items())
                if isinstance(raw_job, dict)
            ]
        restored = {}
        for raw_profile, raw_job in persisted_jobs:
            try:
                profile_index = int(raw_profile)
            except (TypeError, ValueError):
                continue
            if not 0 <= profile_index < MAGNETIC_PROFILE_COUNT or not isinstance(raw_job, dict):
                continue
            settings = {}
            for raw_slot, value in list((raw_job.get("key_settings") or {}).items()):
                try:
                    slot = str(int(raw_slot))
                except (TypeError, ValueError):
                    continue
                normalized = self._womier_cache_sync_setting_payload(value)
                if normalized is not None:
                    settings[slot] = normalized
            modes = {}
            for raw_slot, value in list((raw_job.get("key_modes") or {}).items()):
                try:
                    modes[str(int(raw_slot))] = int(value)
                except (TypeError, ValueError):
                    continue
            try:
                rt_stab = (
                    int(raw_job["rt_stab"])
                    if raw_job.get("rt_stab") is not None
                    else None
                )
            except (TypeError, ValueError):
                rt_stab = None
            if settings or rt_stab is not None:
                restored[(device_key, profile_index)] = {
                    "device_key": device_key,
                    "profile_index": profile_index,
                    "key_settings": settings,
                    "key_modes": modes,
                    "rt_stab": rt_stab,
                }
        if not restored:
            return
        lock = self._ensure_womier_cache_sync_state()
        with lock:
            self._womier_cache_sync_pending.update(restored)
            self._start_womier_cache_sync_timer_locked(
                MAGNETIC_BACKGROUND_PERSIST_DEBOUNCE_SEC, replace=True
            )

    def _start_womier_cache_sync_timer_locked(self, delay, *, replace=False):
        """Schedule one daemon cache drain; caller owns the sync lock."""
        previous = getattr(self, "_womier_cache_sync_timer", None)
        if previous is not None and previous.is_alive():
            if not replace:
                return
            try:
                previous.cancel()
            except Exception:
                pass
        timer = threading.Timer(delay, self._drain_womier_cache_sync)
        timer.daemon = True
        self._womier_cache_sync_timer = timer
        timer.start()

    def _queue_womier_cache_sync(
        self, profile_index, *, key_settings=None, key_modes=None, rt_stab=None
    ):
        """Queue values already accepted by HID for safe official-cache sync.

        This must only be called after a magnetic HID transaction succeeds.
        A pending job is intentionally a narrow delta rather than a complete
        preset, so an old local profile cannot overwrite a Womier-only
        advanced setting that this application did not write.
        """
        try:
            profile_index = int(profile_index)
        except (TypeError, ValueError):
            return
        if not 0 <= profile_index < MAGNETIC_PROFILE_COUNT:
            return
        device_key = self._womier_cache_sync_device_key()
        # Callers normally pass fresh deltas, but a cache/readback worker may
        # hand us a mapping that is still being populated.  Iterate snapshots,
        # not a live ``dict_items`` view, so the queue cannot surface Python's
        # "dictionary changed size during iteration" error.
        setting_items = self._mapping_items_snapshot(key_settings)
        mode_items = self._mapping_items_snapshot(key_modes)
        settings = {}
        modes = {}
        for raw_slot, value in setting_items:
            try:
                slot = str(int(raw_slot))
            except (TypeError, ValueError):
                continue
            normalized = self._womier_cache_sync_setting_payload(value)
            # Test/diagnostic objects without a persisted device can still
            # exercise the queue; real app deltas must be JSON-safe.
            if normalized is None and device_key:
                continue
            settings[slot] = normalized if normalized is not None else value
        for raw_slot, value in mode_items:
            try:
                modes[str(int(raw_slot))] = int(value)
            except (TypeError, ValueError):
                continue
        if device_key and rt_stab is not None:
            try:
                rt_stab = int(rt_stab)
            except (TypeError, ValueError):
                return
        if not settings and rt_stab is None:
            return
        job_key = (device_key, profile_index)
        lock = self._ensure_womier_cache_sync_state()
        needs_save = False
        with lock:
            pending = self._womier_cache_sync_pending.setdefault(
                job_key,
                {
                    "device_key": device_key,
                    "profile_index": profile_index,
                    "key_settings": {},
                    "key_modes": {},
                    "rt_stab": None,
                },
            )
            pending["key_settings"].update(settings)
            pending["key_modes"].update(modes)
            if rt_stab is not None:
                pending["rt_stab"] = rt_stab
            if device_key:
                needs_save = self._persist_womier_cache_sync_delta(
                    device_key, profile_index, pending
                )
            # Align the mirror with the configuration quiet period.  The old
            # 350-ms timer often fired between consecutive +/- clicks, then
            # parsed, backed up and fsync'ed Chromium's LevelDB while the next
            # Flet slider frame was in flight.  HID has already succeeded;
            # waiting for a short pause only coalesces duplicate cache work.
            self._start_womier_cache_sync_timer_locked(
                MAGNETIC_BACKGROUND_PERSIST_DEBOUNCE_SEC, replace=True
            )
        if needs_save:
            schedule_persistence = getattr(self, "_schedule_magnetic_persistence", None)
            if callable(schedule_persistence) and isinstance(getattr(self, "config", None), dict):
                schedule_persistence()
            else:
                # Minimal external integrations historically supplied only a
                # ``save_config`` callback.  Retain their durable fallback;
                # the production manager always takes the quiet-period path.
                self.save_config(reload_runtime=False)

    def _drain_womier_cache_sync(self):
        """Mirror queued HID-success deltas, retrying only closed-driver work."""
        lock = self._ensure_womier_cache_sync_state()
        with lock:
            pending = getattr(self, "_womier_cache_sync_pending", {})
            jobs = dict(pending)
            self._womier_cache_sync_pending = {}
            self._womier_cache_sync_timer = None
            revision = int(getattr(self, "_womier_cache_sync_revision", 0))
        if not jobs:
            return

        deferred = {}
        retryable = {}
        config_changed = False
        for job_key, job in jobs.items():
            # An explicit config import may replace all profile maps while a
            # cache drain is between jobs.  Do not mirror an old document into
            # the official Womier cache after that boundary.
            with lock:
                if int(getattr(self, "_womier_cache_sync_revision", 0)) != revision:
                    return
            profile_index = job.get("profile_index")
            try:
                result = sync_womier_magnetic_cache(
                    profile_index,
                    job.get("key_settings") or {},
                    key_modes=job.get("key_modes") or None,
                    rt_stab=job.get("rt_stab"),
                )
            except WomierCacheSyncError as exc:
                # HID has already succeeded.  A missing/invalid Womier cache
                # must never be reported as a failed keyboard setting write.
                # More importantly, do not drop its durable delta: a transient
                # file lock or antivirus scan used to make the official driver
                # permanently stale until the next full app restart.
                logger.warning("Womier cache sync skipped: %s", exc)
                retryable[job_key] = job
                continue
            except Exception:
                logger.exception("unexpected Womier cache sync failure")
                retryable[job_key] = job
                continue
            if result.deferred:
                deferred[job_key] = job
            elif result.synced:
                with lock:
                    if int(getattr(self, "_womier_cache_sync_revision", 0)) != revision:
                        return
                    # Keep the generation check and journal cleanup in one
                    # cache-sync critical section.  Otherwise an import could
                    # replace the config in the tiny gap between them and an
                    # old successful drain could remove a new document's
                    # matching pending delta.
                    config_changed = self._clear_persisted_womier_cache_sync_delta(
                        job.get("device_key", ""), profile_index, job
                    ) or config_changed
            else:
                logger.warning("Womier cache sync skipped: %s", result.detail)
                retryable[job_key] = job

        if config_changed:
            # Clearing a successful mirror delta is bookkeeping only.  Keep
            # it behind the same quiet-period gate as the incoming key write
            # rather than taking another full JSON/fsync turn immediately.
            schedule_persistence = getattr(self, "_schedule_magnetic_persistence", None)
            if callable(schedule_persistence) and isinstance(getattr(self, "config", None), dict):
                schedule_persistence()
            else:
                self.save_config(reload_runtime=False)

        retry_jobs = dict(deferred)
        retry_jobs.update(retryable)
        if not retry_jobs or not getattr(self, "app_alive", True):
            return
        # The official driver owns the cache while it is open.  Requeue only
        # those deltas; a newer slider write retains priority for the same
        # key, so a delayed retry can never restore an older value.
        with lock:
            if int(getattr(self, "_womier_cache_sync_revision", 0)) != revision:
                return
            for job_key, job in retry_jobs.items():
                pending = self._womier_cache_sync_pending.setdefault(
                    job_key,
                    {
                        "device_key": job.get("device_key", ""),
                        "profile_index": job.get("profile_index"),
                        "key_settings": {},
                        "key_modes": {},
                        "rt_stab": None,
                    },
                )
                for slot, value in (job.get("key_settings") or {}).items():
                    pending["key_settings"].setdefault(slot, value)
                for slot, value in (job.get("key_modes") or {}).items():
                    pending["key_modes"].setdefault(slot, value)
                if pending.get("rt_stab") is None and job.get("rt_stab") is not None:
                    pending["rt_stab"] = job["rt_stab"]
            # Keep checking only while this program lives in the tray; the
            # official process may be closed a few seconds after a change.
            # An actual error gets a calmer retry cadence than the normal
            # "driver currently open" result, so a damaged/missing cache never
            # turns into a 2.5-second disk/log busy loop.
            self._start_womier_cache_sync_timer_locked(
                2.5 if deferred else WOMIER_CACHE_SYNC_ERROR_RETRY_SEC,
                replace=False,
            )

    def _flush_womier_cache_sync(self):
        """Perform queued cache mirroring synchronously during application exit."""
        # Make any in-memory HID-success journal durable before trying the
        # external LevelDB write.  A successful mirror may then remove that
        # journal, in which case the second flush below records the cleanup.
        self._flush_magnetic_persistence()
        lock = self._ensure_womier_cache_sync_state()
        with lock:
            timer = getattr(self, "_womier_cache_sync_timer", None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass
            self._womier_cache_sync_timer = None
        self._drain_womier_cache_sync()
        self._flush_magnetic_persistence()

    def _magnetic_profile_label(self, index):
        name = self._profile_name_at(index)
        return f"{index + 1}. {name}" if name else f"Профиль {index + 1}"

    def _refresh_magnetic_profile_dropdown(self, update=False):
        dropdown = getattr(self, "magnetic_profile_dropdown", None)
        if dropdown is None:
            return
        selected = self._selected_magnetic_profile_index()
        dropdown.options = [
            ft.dropdown.Option(key=str(index), text=self._magnetic_profile_label(index))
            for index in range(MAGNETIC_PROFILE_COUNT)
        ]
        dropdown.value = str(selected)
        if update:
            try:
                dropdown.update()
            except Exception:
                pass

    def _copy_live_magnetic_to_profile(self, entry, index, *, only_uninitialized=False):
        """Save the current keyboard cache into one local preset.

        This is called after a read-only HID read.  With ``only_uninitialized``
        it seeds old configurations without overwriting a user's independent
        presets on subsequent starts.
        """
        if not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("magnetic_profiles"), dict):
            _normalize_magnetic_profile_slots(entry)
        try:
            index = int(index)
        except (TypeError, ValueError):
            index = 0
        index = max(0, min(MAGNETIC_PROFILE_COUNT - 1, index))
        target = entry["magnetic_profiles"][str(index)]
        if only_uninitialized and target.get("initialized"):
            return False
        snapshot = _magnetic_profile_snapshot_from_live(entry)
        target.update(snapshot)
        target["initialized"] = True
        return True

    def _seed_uninitialized_magnetic_profiles(self, entry):
        """Give empty preset slots their first real keyboard values once."""
        changed = False
        for index in range(MAGNETIC_PROFILE_COUNT):
            changed = self._copy_live_magnetic_to_profile(
                entry, index, only_uninitialized=True
            ) or changed
        return changed

    @staticmethod
    def _magnetic_settings_from_config(values):
        if not isinstance(values, dict):
            return None
        try:
            raw_settings = KeyMagneticSettings(
                actuation=values["actuation"],
                rapid_trigger=values["rapid_trigger"],
                rapid_press=values["rapid_press"],
                rapid_release=values["rapid_release"],
                lower_dead_zone=values["lower_dead_zone"],
                upper_dead_zone=values["upper_dead_zone"],
                deactivation=values.get(
                    "deactivation", values.get("lift_travel", values["actuation"])
                ),
            )
            # Local presets imported from an older build can contain a wider
            # protocol value (for example 3.50 mm).  The official SK75 UI is
            # narrower.  Canonicalising on read keeps controls/profile applies
            # honest without triggering a startup HID write.
            return MagneticProtocol.clamp_key_settings_to_official_bounds(
                raw_settings
            )
        except (KeyError, TypeError, ValueError, MagneticProtocolError):
            return None

    @staticmethod
    def _keyboard_options_from_config(values, fallback=None):
        """Decode stored options, retaining non-visible firmware fields safely."""
        if fallback is None:
            fallback = KeyboardOptions()
        if not isinstance(values, dict) or not values:
            return None
        try:
            return KeyboardOptions(
                fn_index=int(values.get("fn_index", fallback.fn_index)),
                anti_accidental=bool(values.get("anti_accidental", fallback.anti_accidental)),
                rt_stab=int(values.get("rt_stab", fallback.rt_stab)),
                wasd_swap=bool(values.get("wasd_swap", fallback.wasd_swap)),
                system=str(values.get("system", fallback.system)),
            )
        except (TypeError, ValueError, MagneticProtocolError):
            return None

    @staticmethod
    def _magnetic_options_to_config(options):
        return {
            "fn_index": options.fn_index,
            "anti_accidental": options.anti_accidental,
            "rt_stab": options.rt_stab,
            "wasd_swap": options.wasd_swap,
            "system": options.system,
        }

    def _live_magnetic_keyboard_options(self):
        """Return options last read/written on the physical keyboard."""
        entry = self._active_device() or {}
        options = self._keyboard_options_from_config(entry.get("magnetic_keyboard_options"))
        return options or KeyboardOptions()

    def _cancel_pending_magnetic_writes(self):
        """Discard unsent slider writes before changing an app-side preset.

        The controls are first copied into the outgoing preset.  Cancelling the
        short debounce here is intentional: the incoming preset is about to
        replace the physical values, so a late packet from the old selector
        must not arrive after the new one.
        """
        lock = getattr(self, "_magnetic_write_lock", None)
        if lock is None:
            return
        with lock:
            timers = getattr(self, "_magnetic_write_timers", {})
            pending = getattr(self, "_magnetic_pending_key_writes", {})
            inflight = getattr(self, "_magnetic_inflight_key_writes", {})
            revisions = getattr(self, "_magnetic_write_revisions", {})
            # A timer normally has a matching pending intent.  Use the union
            # because a callback can have claimed one a fraction of a second
            # before a profile or device switch invalidates it.
            for slot in set(timers) | set(pending) | set(inflight):
                timer = timers.get(slot)
                try:
                    if timer is not None:
                        timer.cancel()
                except Exception:
                    pass
                revisions[slot] = revisions.get(slot, 0) + 1
                pending.pop(slot, None)
                # A final-drain worker has not necessarily opened HID yet.
                # Removing its claim makes it observe the new revision and
                # stop, preserving cancellation for profile/device switches.
                claim = inflight.get(slot)
                if isinstance(claim, tuple) and len(claim) > 1 and claim[1] == "flush":
                    inflight.pop(slot, None)
            timers.clear()
            self._magnetic_options_revision = getattr(self, "_magnetic_options_revision", 0) + 1
            timer = getattr(self, "_magnetic_options_timer", None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass
            self._magnetic_options_timer = None
            self._magnetic_pending_options_write = None
            option_claim = getattr(self, "_magnetic_options_inflight", None)
            if (
                isinstance(option_claim, tuple)
                and len(option_claim) > 1
                and option_claim[1] == "flush"
            ):
                self._magnetic_options_inflight = None

    def _claim_magnetic_key_write_locked(self, slot, revision, owner):
        """Claim the current key-write intent while ``_magnetic_write_lock`` is held.

        A debounce timer and the final drain at hide/quit can race.  The
        marker makes exactly one of them responsible for a revision.  A newer
        revision can safely replace a stale in-flight marker because callers
        hold ``usb_lock`` before invoking this helper.
        """
        revisions = getattr(self, "_magnetic_write_revisions", {})
        if revisions.get(slot) != revision:
            return False
        inflight = getattr(self, "_magnetic_inflight_key_writes", None)
        if not isinstance(inflight, dict):
            inflight = {}
            self._magnetic_inflight_key_writes = inflight
        existing = inflight.get(slot)
        if isinstance(existing, tuple) and existing and existing[0] == revision:
            return False
        inflight[slot] = (revision, owner)
        pending = getattr(self, "_magnetic_pending_key_writes", None)
        if isinstance(pending, dict):
            intent = pending.get(slot)
            if isinstance(intent, tuple) and len(intent) >= 3 and intent[2] == revision:
                pending.pop(slot, None)
        timers = getattr(self, "_magnetic_write_timers", None)
        if isinstance(timers, dict):
            timers.pop(slot, None)
        return True

    def _finish_magnetic_key_write(self, slot, revision, owner):
        lock = getattr(self, "_magnetic_write_lock", None)
        if lock is None:
            return
        with lock:
            inflight = getattr(self, "_magnetic_inflight_key_writes", {})
            if inflight.get(slot) == (revision, owner):
                inflight.pop(slot, None)

    def _claim_magnetic_options_write_locked(self, revision, owner):
        if getattr(self, "_magnetic_options_revision", None) != revision:
            return False
        existing = getattr(self, "_magnetic_options_inflight", None)
        if isinstance(existing, tuple) and existing and existing[0] == revision:
            return False
        self._magnetic_options_inflight = (revision, owner)
        intent = getattr(self, "_magnetic_pending_options_write", None)
        if isinstance(intent, tuple) and len(intent) >= 3 and intent[2] == revision:
            self._magnetic_pending_options_write = None
        self._magnetic_options_timer = None
        return True

    def _finish_magnetic_options_write(self, revision, owner):
        lock = getattr(self, "_magnetic_write_lock", None)
        if lock is None:
            return
        with lock:
            if getattr(self, "_magnetic_options_inflight", None) == (revision, owner):
                self._magnetic_options_inflight = None

    def _pending_magnetic_writes_are_idle(self):
        """Return whether no queued or claimed slider setting is left to send."""
        lock = getattr(self, "_magnetic_write_lock", None)
        if lock is None:
            return True
        with lock:
            return not (
                getattr(self, "_magnetic_pending_key_writes", {})
                or getattr(self, "_magnetic_inflight_key_writes", {})
                or getattr(self, "_magnetic_pending_options_write", None)
                or getattr(self, "_magnetic_options_inflight", None)
            )

    def _wait_for_pending_magnetic_writes(self, timeout=1.25):
        """Bounded wait used only while the application is actually quitting."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if self._pending_magnetic_writes_are_idle():
                return True
            time.sleep(0.02)
        return self._pending_magnetic_writes_are_idle()

    def _flush_pending_magnetic_writes(self):
        """Send final debounced slider values after hiding the window.

        This deliberately does *not* replace ``_cancel_pending_magnetic_writes``:
        profile/device selection still invalidates stale values.  It is only
        used for hide/quit, where the visible value is still the intended one.
        The actual HID work happens on a short daemon worker, keeping the
        Flet event loop responsive.
        """
        lock = getattr(self, "_magnetic_write_lock", None)
        if lock is None:
            return None
        key_jobs = []
        option_job = None
        with lock:
            pending = getattr(self, "_magnetic_pending_key_writes", {})
            timers = getattr(self, "_magnetic_write_timers", {})
            revisions = getattr(self, "_magnetic_write_revisions", {})
            for slot, intent in list(pending.items()):
                if not isinstance(intent, tuple) or len(intent) != 4:
                    pending.pop(slot, None)
                    continue
                settings, packets, revision, profile_index = intent
                if revisions.get(slot) != revision:
                    pending.pop(slot, None)
                    continue
                timer = timers.get(slot)
                if timer is not None:
                    try:
                        timer.cancel()
                    except Exception:
                        pass
                if self._claim_magnetic_key_write_locked(slot, revision, "flush"):
                    key_jobs.append((slot, settings, packets, revision, profile_index))

            option_intent = getattr(self, "_magnetic_pending_options_write", None)
            if isinstance(option_intent, tuple) and len(option_intent) == 4:
                rt_stab, anti_accidental, revision, profile_index = option_intent
                if getattr(self, "_magnetic_options_revision", None) == revision:
                    timer = getattr(self, "_magnetic_options_timer", None)
                    if timer is not None:
                        try:
                            timer.cancel()
                        except Exception:
                            pass
                    if self._claim_magnetic_options_write_locked(revision, "flush"):
                        option_job = (rt_stab, anti_accidental, revision, profile_index)
                else:
                    self._magnetic_pending_options_write = None

        if not key_jobs and option_job is None:
            return None

        def worker():
            for slot, settings, packets, revision, profile_index in key_jobs:
                self._write_flushed_magnetic_key(
                    slot, settings, packets, revision, profile_index
                )
            if option_job is not None:
                self._write_flushed_magnetic_options(*option_job)

        worker_thread = threading.Thread(
            target=worker, daemon=True, name="magnetic-final-drain"
        )
        worker_thread.start()
        return worker_thread

    def _write_flushed_magnetic_key(self, slot, settings, packets, revision, profile_index):
        try:
            with self.usb_lock:
                with self._magnetic_write_lock:
                    if (
                        self._magnetic_write_revisions.get(slot) != revision
                        or self._magnetic_inflight_key_writes.get(slot) != (revision, "flush")
                    ):
                        return
                self._send_lighting_packets_locked(
                    packets, f"magnetic_key_{slot}", inter_packet_delay=0.01
                )
                with self._magnetic_write_lock:
                    if (
                        self._magnetic_write_revisions.get(slot) != revision
                        or self._magnetic_inflight_key_writes.get(slot) != (revision, "flush")
                    ):
                        return
                self._cache_magnetic_settings(
                    slot, settings, profile_index=profile_index
                )
            if getattr(self, "_womier_cache_sync_lock", None) is not None:
                self._queue_womier_cache_sync(
                    profile_index,
                    key_settings={slot: settings},
                    key_modes={
                        slot: MagneticProtocol.MODE_NORMAL
                        | (
                            MagneticProtocol.MODE_RAPID_TRIGGER_BIT
                            if settings.rapid_trigger
                            else 0
                        )
                    },
                )
            schedule_persistence = getattr(self, "_schedule_magnetic_persistence", None)
            # Bare test/dry-run managers intentionally omit ``config``.  Keep
            # their old synchronous callback semantics instead of arming a
            # background timer that cannot persist anything useful.
            if callable(schedule_persistence) and isinstance(getattr(self, "config", None), dict):
                schedule_persistence()
            else:
                self.save_config(reload_runtime=False)
        except (MagneticProtocolError, LightingProtocolError) as exc:
            self._set_magnetic_status(str(exc), ft.Colors.ERROR)
        finally:
            self._finish_magnetic_key_write(slot, revision, "flush")

    def _write_flushed_magnetic_options(
        self, rt_stab, anti_accidental, revision, profile_index
    ):
        try:
            with self.usb_lock:
                with self._magnetic_write_lock:
                    if (
                        self._magnetic_options_revision != revision
                        or self._magnetic_options_inflight != (revision, "flush")
                    ):
                        return
                current = self._live_magnetic_keyboard_options()
                options = KeyboardOptions(
                    fn_index=current.fn_index,
                    anti_accidental=anti_accidental,
                    rt_stab=rt_stab,
                    wasd_swap=current.wasd_swap,
                    system=current.system,
                )
                self._send_lighting_packets_locked(
                    [MagneticProtocol.keyboard_options_packet(options)],
                    "magnetic_kboption",
                    inter_packet_delay=0.0,
                )
                with self._magnetic_write_lock:
                    if (
                        self._magnetic_options_revision != revision
                        or self._magnetic_options_inflight != (revision, "flush")
                    ):
                        return
                self._cache_magnetic_keyboard_options(
                    options, profile_index=profile_index
                )
            if getattr(self, "_womier_cache_sync_lock", None) is not None:
                self._queue_womier_cache_sync(
                    profile_index, rt_stab=options.rt_stab
                )
            schedule_persistence = getattr(self, "_schedule_magnetic_persistence", None)
            # See the equivalent key-write path above: the real manager has
            # a config dict; minimal integrations retain the legacy callback.
            if callable(schedule_persistence) and isinstance(getattr(self, "config", None), dict):
                schedule_persistence()
            else:
                self.save_config(reload_runtime=False)
        except (MagneticProtocolError, LightingProtocolError) as exc:
            self._set_magnetic_status(str(exc), ft.Colors.ERROR)
        finally:
            self._finish_magnetic_options_write(revision, "flush")

    def _store_magnetic_controls_in_profile(self, index):
        """Save the visible controls into a specific local preset only.

        This is the automatic counterpart of the removed "read into preset"
        button.  It deliberately never performs a new HID read: selecting a
        preset must be quick and must not overwrite that preset with whatever
        happened to be on the keyboard just before the selection.
        """
        # A delayed per-key HID write can finish while a profile select stores
        # the outgoing editor values.  Use the same re-entrant config lock as
        # save/export/cache writes so JSON/Flet never observes a nested profile
        # dictionary while it is growing.
        with _CONFIG_WRITE_LOCK:
            entry = self._active_device()
            if entry is None or not hasattr(self, "magnetic_actuation_slider"):
                return False
            profile = self._magnetic_profile_slot(index)
            if profile is None:
                return False
            try:
                settings = self._magnetic_settings_from_controls()
                slot = int(self.magnetic_selected_slot)
            except (TypeError, ValueError, MagneticProtocolError):
                return False
            profile.setdefault("key_settings", {})[str(slot)] = self._magnetic_settings_to_config(settings)
            profile.setdefault("rt_separate", {})[str(slot)] = bool(
                self.magnetic_rt_separate_switch.value
            )
            if hasattr(self, "magnetic_rt_stab_dropdown"):
                try:
                    fallback = self._keyboard_options_from_config(
                        profile.get("keyboard_options"), self._live_magnetic_keyboard_options()
                    ) or self._live_magnetic_keyboard_options()
                    options = KeyboardOptions(
                        fn_index=fallback.fn_index,
                        anti_accidental=bool(self.magnetic_anti_accidental_switch.value),
                        rt_stab=int(self.magnetic_rt_stab_dropdown.value),
                        wasd_swap=fallback.wasd_swap,
                        system=fallback.system,
                    )
                    profile["keyboard_options"] = self._magnetic_options_to_config(options)
                except (TypeError, ValueError, MagneticProtocolError):
                    pass
            profile["initialized"] = True
        return True

    def _magnetic_profile_switch_is_current(self, index, revision, device_key=None):
        lock = getattr(self, "_magnetic_profile_switch_lock", None)
        if lock is None:
            return False
        with lock:
            return (
                bool(getattr(self, "app_alive", True))
                and
                getattr(self, "_magnetic_profile_switch_revision", None) == revision
                and self._selected_magnetic_profile_index() == index
                and (device_key is None or self.config.get("active_device") == device_key)
            )

    def _schedule_magnetic_profile_apply(self, index, delay=0.12):
        """Coalesce selector changes, then apply the final local preset.

        The SK75 protocol has no magnetic profile field, so this is necessarily
        an app-side operation.  The revision makes a fast selector change safe:
        only the last requested preset reaches the HID write stage.
        """
        if not getattr(self, "app_alive", True):
            return
        lock = getattr(self, "_magnetic_profile_switch_lock", None)
        if lock is None:
            return
        device_key = self.config.get("active_device")
        with lock:
            self._magnetic_profile_switch_revision += 1
            revision = self._magnetic_profile_switch_revision
            previous = self._magnetic_profile_switch_timer
            if previous is not None:
                try:
                    previous.cancel()
                except Exception:
                    pass
            timer = threading.Timer(
                delay,
                self._apply_selected_magnetic_profile_automatically,
                args=(index, revision, device_key),
            )
            timer.daemon = True
            self._magnetic_profile_switch_timer = timer
            timer.start()

    def _stop_magnetic_profile_switching(self):
        """Cancel a queued profile write when the app is hidden or exits."""
        lock = getattr(self, "_magnetic_profile_switch_lock", None)
        if lock is None:
            return
        with lock:
            self._magnetic_profile_switch_revision += 1
            timer = self._magnetic_profile_switch_timer
            self._magnetic_profile_switch_timer = None
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass

    def _on_magnetic_profile_changed(
        self,
        event=None,
        *,
        apply_delay=0.12,
        apply_to_keyboard=True,
    ):
        """Select a local magnetic preset and optionally apply it to hardware.

        A direct Magnetic Lab selection is an explicit request to write and
        verify the preset, so it retains the debounced profile worker.  A
        foreground/process-rule selection is deliberately UI/cache-only.  The
        SK75 protocol has no single magnetic-profile command; turning an
        Alt+Tab into a 75-key HID transaction can starve normal key reports.
        """
        if not getattr(self, "app_alive", True):
            return
        # Recent Flet desktop builds put the freshly selected key in ``data``
        # while ``control.value`` can still contain the previous menu item for
        # the duration of this callback.  Prefer that event payload whenever it
        # is one of our four preset keys; otherwise retain the normal control
        # fallback for programmatic calls and older Flet versions.
        event_value = getattr(event, "data", None)
        value = (
            event_value
            if str(event_value) in {str(item) for item in range(MAGNETIC_PROFILE_COUNT)}
            else getattr(getattr(event, "control", None), "value", None)
        )
        if value is None:
            value = getattr(getattr(self, "magnetic_profile_dropdown", None), "value", None)
        try:
            index = int(value)
        except (TypeError, ValueError):
            index = 0
        index = max(0, min(MAGNETIC_PROFILE_COUNT - 1, index))
        with _CONFIG_WRITE_LOCK:
            entry = self._active_device()
            if entry is None:
                return
        previous_index = max(
            0, min(MAGNETIC_PROFILE_COUNT - 1, getattr(self, "magnetic_profile_index", 0))
        )
        # Persist the still-visible outgoing values before moving the dropdown.
        self._store_magnetic_controls_in_profile(previous_index)
        self._cancel_pending_magnetic_writes()
        self.magnetic_profile_index = index
        # This selected-profile field is read by timer workers too.  Its
        # update must share ownership with their cache writes and config saves.
        with _CONFIG_WRITE_LOCK:
            current_entry = self._active_device()
            if current_entry is None:
                return
            current_entry["magnetic_selected_profile"] = index
        # The event's value can lag one paint in Flet.  Set it explicitly so
        # every cache helper immediately reads the newly chosen preset.
        self.magnetic_profile_dropdown.value = str(index)
        self._refresh_magnetic_profile_dropdown()
        # A Magnetic Lab preset contains HID thresholds only.  Persisting its
        # selected local slot must never restart the foreground switch runtime
        # and temporarily unhook normal keyboard input.
        self.save_config(reload_runtime=False)
        if hasattr(self, "magnetic_actuation_slider"):
            self._load_magnetic_controls(self.magnetic_selected_slot)
        if hasattr(self, "magnetic_rt_stab_dropdown"):
            options = self._cached_magnetic_keyboard_options()
            self.magnetic_rt_stab_dropdown.value = str(options.rt_stab)
            self.magnetic_anti_accidental_switch.value = options.anti_accidental
        self._refresh_sk75_keyboard_picker()
        if not apply_to_keyboard:
            # A previous explicit preset worker must not keep running after an
            # automatic foreground switch has changed the selected local set.
            # The revision check in that worker makes this cancellation safe
            # even if it has just completed a single key packet.
            self._stop_magnetic_profile_switching()
            if index != previous_index:
                self._set_magnetic_status(
                    "Набор выбран для профиля. Запись магнитных значений выполняется из Magnetic Lab.",
                    ft.Colors.ON_SURFACE_VARIANT,
                )
            return
        if index == previous_index:
            return
        self._set_magnetic_status("Применяю выбранный набор…", ft.Colors.ON_SURFACE_VARIANT)
        # Keep the manual call shape intact for older UI/test adapters.
        if apply_delay == 0.12:
            self._schedule_magnetic_profile_apply(index)
        else:
            self._schedule_magnetic_profile_apply(index, delay=apply_delay)

    def _select_magnetic_preset_for_keyboard_profile(
        self,
        profile_index,
        *,
        automatic=False,
        should_continue=None,
    ):
        """Keep one ordinary SK75 profile and its local magnetic preset paired.

        The keyboard's regular profile command and the magnetic-settings
        protocol are separate: the latter has no profile byte.  Consequently a
        regular profile switch used to leave Magnetic Lab pointed at whichever
        local preset happened to be open first (usually preset 1).  Route both
        manual/automatic regular-profile changes through the same selector so
        the UI and persisted local choice follow the regular profile.  Only an
        explicit Magnetic Lab selection writes the per-key preset to hardware;
        the automatic route must stay a one-packet operation during Alt+Tab.
        """
        entry = self._active_device()
        if entry is None or entry.get("keyboard_type") != "magnetic":
            return False
        try:
            index = int(profile_index)
        except (TypeError, ValueError):
            return False
        if not 0 <= index < MAGNETIC_PROFILE_COUNT:
            return False

        def select():
            if should_continue is not None:
                try:
                    if not should_continue():
                        return
                except Exception:
                    logger.debug("stale automatic magnetic selection", exc_info=True)
                    return
            # ``apply_payload`` can run before Magnetic Lab has been mounted
            # (for example when the service starts immediately).  Keep the
            # persisted selection correct in that rare case; the mounted Lab
            # will load this profile on its first paint.
            if not hasattr(self, "magnetic_profile_dropdown"):
                self.magnetic_profile_index = index
                with _CONFIG_WRITE_LOCK:
                    current_entry = self._active_device()
                    if current_entry is None:
                        return
                    current_entry["magnetic_selected_profile"] = index
                self.save_config(reload_runtime=False)
                return
            self._on_magnetic_profile_changed(
                SimpleNamespace(control=SimpleNamespace(value=str(index))),
                # Never schedule a per-key magnetic preset from Alt+Tab.  The
                # selector still changes so the visible/local profile matches
                # the ordinary keyboard profile, while a deliberate selection
                # inside Magnetic Lab retains the usual 120 ms debounce.
                apply_to_keyboard=not automatic,
            )

        self._ui_call(select)
        return True

    def _sync_magnetic_profile_controls_for_active_device(self):
        """Point the already-built Magnetic Lab at a newly active device."""
        entry = self._active_device()
        if entry is None:
            return
        try:
            index = int(entry.get("magnetic_selected_profile", 0))
        except (TypeError, ValueError):
            index = 0
        self.magnetic_profile_index = max(0, min(MAGNETIC_PROFILE_COUNT - 1, index))
        self._refresh_magnetic_profile_dropdown()
        if not hasattr(self, "magnetic_actuation_slider"):
            return
        self._load_magnetic_controls(self.magnetic_selected_slot)
        if hasattr(self, "magnetic_rt_stab_dropdown"):
            options = self._cached_magnetic_keyboard_options()
            self.magnetic_rt_stab_dropdown.value = str(options.rt_stab)
            self.magnetic_anti_accidental_switch.value = options.anti_accidental
        self._refresh_sk75_keyboard_picker()

    def _apply_selected_magnetic_profile_automatically(self, index, revision, device_key=None):
        """Apply and read-back-verify one local magnetic preset.

        The SK75 magnetic protocol has no firmware profile field.  A preset
        selection must therefore compare against an actual ``0xE5`` matrix
        read, rather than against this application's old cache.  Otherwise a
        cache left over from profile 1 can make profile 2 look selected while
        no HID writes are sent at all.
        """
        if not getattr(self, "app_alive", True):
            return
        entry = self._active_device()
        if entry is None or entry.get("keyboard_type") != "magnetic":
            return
        if not self._magnetic_profile_switch_is_current(index, revision, device_key):
            return
        profile = self._magnetic_profile_slot(index)
        if not isinstance(profile, dict) or not profile.get("initialized"):
            self._set_magnetic_status(
                "В наборе ещё нет значений — считываю текущие настройки.",
                ft.Colors.ON_SURFACE_VARIANT,
            )
            self._read_magnetic_matrix(
                silent=False,
                capture_to_profile_index=index,
            )
            return
        target_settings = _json_copy(profile.get("key_settings") or {})
        target_modes = _json_copy(profile.get("key_modes") or {})
        target_options = _json_copy(profile.get("keyboard_options") or {})
        label = self._magnetic_profile_label(index)
        self._set_magnetic_status(
            f"Проверяю и применяю набор «{label}»…",
            ft.Colors.ON_SURFACE_VARIANT,
        )

        def worker():
            try:
                # Read -> write -> read-back is one USB transaction.  It is
                # intentionally a little more deliberate than a slider
                # debounce: values chosen from a profile need proof that they
                # reached the keyboard, and it also protects a live Snap Key
                # from a stale app-side mode cache.
                with self.usb_lock:
                    if not self._magnetic_profile_switch_is_current(index, revision, device_key):
                        return
                    active = self._active_device()
                    if active is None:
                        raise MagneticProtocolError("клавиатура больше не выбрана")
                    physical_settings, physical_modes, physical_options = (
                        self._read_magnetic_matrix_locked(
                            f"magnetic_profile_{index + 1}_before",
                            include_keyboard_options=bool(target_options),
                        )
                    )
                    if not self._magnetic_profile_switch_is_current(index, revision, device_key):
                        return
                    key_packet_groups = []
                    changed_slots = []
                    skipped_advanced = 0
                    for raw_slot, stored in target_settings.items():
                        try:
                            slot = int(raw_slot)
                        except (TypeError, ValueError):
                            continue
                        desired = self._magnetic_settings_from_config(stored)
                        if slot not in SK75_KEY_BY_SLOT or desired is None:
                            continue
                        try:
                            target_mode = int(target_modes.get(str(slot), MagneticProtocol.MODE_NORMAL))
                        except (TypeError, ValueError):
                            target_mode = MagneticProtocol.MODE_NORMAL
                        try:
                            live_mode = int(
                                physical_modes.get(slot, MagneticProtocol.MODE_NORMAL)
                            )
                        except (TypeError, ValueError):
                            live_mode = MagneticProtocol.MODE_NORMAL
                        # Snap Key partner links are not included in the read
                        # matrix.  Do not claim to restore or erase them when a
                        # normal magnetic preset is selected.
                        if (
                            (target_mode & 0x7F) != MagneticProtocol.MODE_NORMAL
                            or (live_mode & 0x7F) != MagneticProtocol.MODE_NORMAL
                        ):
                            skipped_advanced += 1
                            continue
                        mismatch, _options_match = self._magnetic_profile_readback_mismatches(
                            {slot: desired},
                            physical_settings,
                            physical_modes,
                        )
                        if not mismatch:
                            continue
                        key_packet_groups.append(
                            (
                                slot,
                                MagneticProtocol.key_settings_packets(slot, desired),
                            )
                        )
                        changed_slots.append((slot, desired))

                    live_options = physical_options or self._live_magnetic_keyboard_options()
                    desired_options = self._keyboard_options_from_config(
                        target_options, live_options
                    )
                    options_changed = (
                        desired_options is not None and desired_options != live_options
                    )

                    if key_packet_groups or options_changed:
                        # Each ``key_settings_packets`` group has its own
                        # final marker.  The stock driver waits after that
                        # marker before it begins another key, so preserve
                        # that firmware commit boundary.  Flattening all
                        # groups into one feature-report burst is unreliable:
                        # the HID sends can succeed while a later key silently
                        # replaces the prior key's pending commit.
                        for slot, key_packets in key_packet_groups:
                            if not self._magnetic_profile_switch_is_current(
                                index, revision, device_key
                            ):
                                return
                            self._send_lighting_packets_locked(
                                key_packets,
                                f"magnetic_profile_{index + 1}_key_{slot}",
                                inter_packet_delay=0.01,
                            )
                            time.sleep(WOMIER_MAGNETIC_SIMPLE_COMMIT_DELAY_SEC)
                        if options_changed:
                            if not self._magnetic_profile_switch_is_current(
                                index, revision, device_key
                            ):
                                return
                            self._send_lighting_packets_locked(
                                [MagneticProtocol.keyboard_options_packet(desired_options)],
                                f"magnetic_profile_{index + 1}_options",
                            )
                            time.sleep(WOMIER_MAGNETIC_SIMPLE_COMMIT_DELAY_SEC)
                        confirmed_settings, confirmed_modes, confirmed_options = (
                            self._read_magnetic_matrix_locked(
                                f"magnetic_profile_{index + 1}_verify",
                                include_keyboard_options=options_changed,
                            )
                        )
                    else:
                        # The first physical read is already proof that this
                        # preset is active.  Do not issue needless writes or
                        # a second large matrix read in that case.
                        confirmed_settings = physical_settings
                        confirmed_modes = physical_modes
                        confirmed_options = physical_options

                    expected_settings = {
                        slot: desired for slot, desired in changed_slots
                    }
                    mismatched_slots, options_match = (
                        self._magnetic_profile_readback_mismatches(
                            expected_settings,
                            confirmed_settings,
                            confirmed_modes,
                            expected_options=(desired_options if options_changed else None),
                            actual_options=confirmed_options,
                        )
                    )
                    if mismatched_slots or not options_match:
                        names = [
                            SK75_KEY_BY_SLOT[slot].label
                            for slot in mismatched_slots[:4]
                            if slot in SK75_KEY_BY_SLOT
                        ]
                        tail = "…" if len(mismatched_slots) > len(names) else ""
                        detail = ", ".join(names) + tail if names else "RTStab/защита"
                        raise MagneticProtocolError(
                            "Клавиатура не подтвердила набор «%s» (%s). "
                            "Закройте Womier Driver и выберите набор ещё раз."
                            % (label, detail)
                        )
                    if not self._magnetic_profile_switch_is_current(index, revision, device_key):
                        return
                    # From this point the cache truly describes what the
                    # keyboard returned, not merely what was requested.  This
                    # worker can finish while a rapid RT-toggle event is
                    # saving the same nested configuration.  Keep the whole
                    # replacement under the config snapshot lock: otherwise
                    # ``json.dumps`` can see the mapping resize halfway
                    # through its traversal (``dictionary changed size during
                    # iteration``).
                    with _CONFIG_WRITE_LOCK:
                        if not self._magnetic_profile_switch_is_current(
                            index, revision, device_key
                        ):
                            return
                        active = self._active_device()
                        if active is None:
                            return
                        active["magnetic_key_settings"] = {
                            str(slot): self._magnetic_settings_to_config(settings)
                            for slot, settings in confirmed_settings.items()
                        }
                        active["magnetic_key_modes"] = {
                            str(slot): int(mode)
                            for slot, mode in confirmed_modes.items()
                        }
                        if confirmed_options is not None:
                            active["magnetic_keyboard_options"] = (
                                self._magnetic_options_to_config(confirmed_options)
                            )
                            self._magnetic_keyboard_options_cache = confirmed_options
                self.save_config(reload_runtime=False)
                if getattr(self, "_womier_cache_sync_lock", None) is not None:
                    # Only the normal keys that actually reached HID are
                    # mirrored.  Snap/other advanced entries were explicitly
                    # skipped above and must remain owned by their own UI.
                    self._queue_womier_cache_sync(
                        index,
                        key_settings={slot: desired for slot, desired in changed_slots},
                        key_modes={
                            slot: (
                                MagneticProtocol.MODE_NORMAL
                                | (
                                    MagneticProtocol.MODE_RAPID_TRIGGER_BIT
                                    if desired.rapid_trigger
                                    else 0
                                )
                            )
                            for slot, desired in changed_slots
                        },
                        rt_stab=(desired_options.rt_stab if options_changed else None),
                    )

                def finish():
                    # A later selection has its own worker.  Do not repaint its
                    # controls or status with a result from the older request.
                    if not self._magnetic_profile_switch_is_current(index, revision, device_key):
                        return
                    self._load_magnetic_controls(self.magnetic_selected_slot)
                    if options_changed:
                        self.magnetic_rt_stab_dropdown.value = str(confirmed_options.rt_stab)
                        self.magnetic_anti_accidental_switch.value = confirmed_options.anti_accidental
                    self._refresh_sk75_keyboard_picker()
                    suffix = " Snap Key не изменён." if skipped_advanced else ""
                    count = len(changed_slots)
                    self.magnetic_status.value = (
                        f"Набор «{label}» подтверждён клавиатурой"
                        + (f": {count} клавиш." if count else ".")
                        + suffix
                    )
                    self.magnetic_status.color = ft.Colors.GREEN_300
                    # All other affected subtrees have already received their
                    # narrow patches above.  A whole-page diff from this
                    # background completion can collide with a user toggling
                    # RT, so update only the fixed status leaf.
                    try:
                        self.magnetic_status.update()
                    except Exception:
                        pass

                self._ui_call(finish)
            except (MagneticProtocolError, LightingProtocolError) as exc:
                if self._magnetic_profile_switch_is_current(index, revision, device_key):
                    self._set_magnetic_status(str(exc), ft.Colors.ERROR)

        threading.Thread(target=worker, daemon=True, name=f"magnetic-profile-{index + 1}").start()

    def _cached_magnetic_keyboard_options(self):
        """Return options from the selected local preset, then live fallback."""
        profile = self._magnetic_profile_slot()
        live = self._live_magnetic_keyboard_options()
        options = self._keyboard_options_from_config(
            (profile or {}).get("keyboard_options"), live
        )
        return options or live

    def _cache_magnetic_keyboard_options(
        self,
        options,
        *,
        store_in_selected_profile=True,
        profile_index=None,
    ):
        with _CONFIG_WRITE_LOCK:
            entry = self._active_device()
            if entry is None:
                return
            stored = self._magnetic_options_to_config(options)
            entry["magnetic_keyboard_options"] = stored
            profile = self._magnetic_profile_slot(profile_index)
            if store_in_selected_profile and profile is not None:
                profile["keyboard_options"] = _json_copy(stored)
                profile["initialized"] = True
            self._magnetic_keyboard_options_cache = options

    def _store_magnetic_keyboard_options(
        self,
        options,
        *,
        store_in_selected_profile=True,
        profile_index=None,
    ):
        self._cache_magnetic_keyboard_options(
            options,
            store_in_selected_profile=store_in_selected_profile,
            profile_index=profile_index,
        )
        # Keyboard options are magnetic-only state; do not reset runtime input
        # monitoring merely because RTStab/anti-accidental changed.
        self.save_config(reload_runtime=False)

    def _magnetic_key_mode(self, slot):
        entry = self._active_device() or {}
        profile = self._magnetic_profile_slot()
        modes = (profile or {}).get("key_modes") or entry.get("magnetic_key_modes") or {}
        try:
            return int(modes.get(str(slot), MagneticProtocol.MODE_NORMAL))
        except (AttributeError, TypeError, ValueError):
            return MagneticProtocol.MODE_NORMAL

    def _magnetic_key_is_advanced(self, slot):
        """Advanced modes have their own dialogs and must not be reset by RT UI."""
        return (self._magnetic_key_mode(slot) & 0x7F) != MagneticProtocol.MODE_NORMAL

    @staticmethod
    def _magnetic_mode_is_snap(mode):
        """Return True only for the firmware's real Snap Key mode (7)."""
        try:
            return (int(mode) & 0x7F) == MagneticProtocol.MODE_SNAP
        except (TypeError, ValueError):
            return False

    def _magnetic_key_is_snap(self, slot):
        return self._magnetic_mode_is_snap(self._magnetic_key_mode(slot))

    def _known_snap_key_slots(self):
        """Collect every valid Snap slot stored for the active keyboard.

        The physical keyboard only has one active state, but the application
        keeps four local magnetic presets.  The clear-all action deliberately
        finds Snap slots in both the live cache and every preset so a stale
        amber marker cannot come back when the user changes a preset later.
        """
        # A Snap worker and a full matrix read can replace the same preset
        # maps while the main UI opens the dialog/summary.  Copy the small
        # mode mappings while config ownership is held, then inspect the
        # detached snapshots below.  Iterating ``profiles.values()`` or
        # ``modes.items()`` directly here was one remaining path to Python's
        # ``dictionary changed size during iteration`` error.
        with _CONFIG_WRITE_LOCK:
            entry = self._active_device() or {}
            raw_mappings = [entry.get("magnetic_key_modes")]
            profiles = entry.get("magnetic_profiles")
            if isinstance(profiles, dict):
                raw_mappings.extend(
                    profile.get("key_modes")
                    for profile in list(profiles.values())
                    if isinstance(profile, dict)
                )
            mappings = [dict(modes) for modes in raw_mappings if isinstance(modes, dict)]
        slots = set()
        for modes in mappings:
            for raw_slot, mode in modes.items():
                try:
                    slot = int(raw_slot)
                except (TypeError, ValueError):
                    continue
                if slot in SK75_KEY_BY_SLOT and QMKManager._magnetic_mode_is_snap(mode):
                    slots.add(slot)
        return sorted(slots)

    @staticmethod
    def _clear_snap_modes_from_entry(entry, slots):
        """Clear only cached Snap flags, preserving every per-key value.

        This mirrors the HID clear packet: key settings, RT separation and
        keyboard-wide options are intentionally not touched.
        """
        if not isinstance(entry, dict):
            return False
        normalized_slots = {
            str(slot)
            for slot in slots
            if isinstance(slot, int) and slot in SK75_KEY_BY_SLOT
        }
        if not normalized_slots:
            return False
        changed = False
        live_settings = entry.get("magnetic_key_settings")
        mappings = [(entry.get("magnetic_key_modes"), live_settings)]
        profiles = entry.get("magnetic_profiles")
        if isinstance(profiles, dict):
            mappings.extend(
                (profile.get("key_modes"), profile.get("key_settings", live_settings))
                for profile in profiles.values()
                if isinstance(profile, dict)
            )
        for modes, settings_by_slot in mappings:
            if not isinstance(modes, dict):
                continue
            for slot in normalized_slots:
                if QMKManager._magnetic_mode_is_snap(modes.get(slot)):
                    setting = (
                        settings_by_slot.get(slot)
                        if isinstance(settings_by_slot, dict)
                        else None
                    )
                    rapid_trigger = bool(
                        setting.get("rapid_trigger")
                        if isinstance(setting, dict)
                        else False
                    )
                    modes[slot] = (
                        MagneticProtocol.MODE_NORMAL
                        | (
                            MagneticProtocol.MODE_RAPID_TRIGGER_BIT
                            if rapid_trigger else 0
                        )
                    )
                    changed = True
        def remove_pairs(owner, field_name):
            nonlocal changed
            if not isinstance(owner, dict):
                return
            pairs = owner.get(field_name)
            if not isinstance(pairs, list):
                return
            retained = [
                pair
                for pair in pairs
                if not (
                    isinstance(pair, (list, tuple))
                    and len(pair) == 2
                    and (
                        str(pair[0]) in normalized_slots
                        or str(pair[1]) in normalized_slots
                    )
                )
            ]
            if retained != pairs:
                owner[field_name] = retained
                changed = True

        remove_pairs(entry, "magnetic_snap_pairs")
        if isinstance(profiles, dict):
            for profile in profiles.values():
                remove_pairs(profile, "snap_pairs")
        return changed

    def _update_magnetic_rt_label(self, update=False):
        # Typography lives in the persistent Material 3 mode surface, not in
        # the native Switch label.  This keeps the switch compact and lets the
        # whole selected-key panel switch cleanly between RT and normal mode.
        self.magnetic_rt_switch.label = None
        self._update_magnetic_parameter_mode_ui(update=update)

    def _magnetic_rt_is_separate(self, slot, settings):
        """Return the saved UI choice, or infer it from real hardware values."""
        entry = self._active_device() or {}
        profile = self._magnetic_profile_slot()
        choices = (profile or {}).get("rt_separate") or entry.get("magnetic_rt_separate")
        if isinstance(choices, dict) and isinstance(choices.get(str(slot)), bool):
            return choices[str(slot)]
        # Existing profiles may already use different press/release thresholds.
        # Keep that intentional keyboard state visible instead of collapsing it
        # when the new switch is introduced.
        return abs(settings.rapid_press - settings.rapid_release) >= 0.001

    def _store_magnetic_rt_separate(self, slot, separate, *, profile_index=None):
        if slot not in SK75_KEY_BY_SLOT:
            return
        # This can race a just-fired per-key HID timer.  Cooperate with
        # ``save_config`` so a rapid OFF/ON sequence never changes the nested
        # profile map while its JSON snapshot is being serialised.
        with _CONFIG_WRITE_LOCK:
            entry = self._active_device()
            if entry is None:
                return
            entry.setdefault("magnetic_rt_separate", {})[str(slot)] = bool(separate)
            profile = self._magnetic_profile_slot(profile_index)
            if profile is not None:
                profile.setdefault("rt_separate", {})[str(slot)] = bool(separate)
                profile["initialized"] = True
            # This local UI flag is persisted beside magnetic thresholds only.
            self.save_config(reload_runtime=False)

    @staticmethod
    def _magnetic_deactivation_is_separate(settings):
        """Infer normal-mode independence from the real firmware values.

        The SK75 exposes one ordinary deactivation field (``liftTravel``),
        not a second mode bit.  Keeping a separate local flag would therefore
        only be UI state and could disagree with Womier.  A differing value is
        the durable, device-backed source of truth.
        """
        try:
            return abs(float(settings.deactivation) - float(settings.actuation)) >= 0.001
        except (AttributeError, TypeError, ValueError):
            return False

    def _update_magnetic_rt_separation_ui(self, update=False):
        """Reveal the optional repeat-down card only in separate RT mode."""
        separate = bool(self.magnetic_rt_separate_switch.value)
        self.magnetic_rt_separate_switch.label = None
        if not separate:
            self._set_vertical_magnetic_value(
                self.magnetic_rt_press_slider,
                self.magnetic_rt_release_slider.value,
                update_controls=False,
            )
        # Keep the immediate visible state correct for lightweight/test
        # instances that do not mount the complete M3 mode surface.
        press_control = getattr(self, "magnetic_rt_press_control", None)
        if press_control is not None:
            rt_enabled = bool(getattr(getattr(self, "magnetic_rt_switch", None), "value", True))
            press_control.visible = separate and rt_enabled
        self._update_magnetic_parameter_mode_ui(update=update)

    def _update_magnetic_deactivation_separation_ui(self, update=False):
        """Keep the ordinary deactivation field tied to actuation by default."""
        switch = getattr(self, "magnetic_deactivation_separate_switch", None)
        if switch is None:
            return
        switch.label = None
        if (
            not bool(getattr(getattr(self, "magnetic_rt_switch", None), "value", True))
            and not bool(switch.value)
        ):
            deactivation_slider = getattr(self, "magnetic_deactivation_slider", None)
            actuation_slider = getattr(self, "magnetic_actuation_slider", None)
            if deactivation_slider is not None and actuation_slider is not None:
                self._set_vertical_magnetic_value(
                    deactivation_slider,
                    actuation_slider.value,
                    update_controls=False,
                )
        self._update_magnetic_parameter_mode_ui(update=update)

    def _on_magnetic_control_changed(self, *, defer_hid=False, refresh_keycap=True):
        """Reconcile one selected-key edit without blocking the Flet event loop.

        ``defer_hid`` is used only while a vertical ruler is actively dragged.
        The UI model and, at a bounded visual rate, the one selected keycap
        remain live, but the HID debounce is scheduled once when the gesture
        finishes.  This avoids creating/cancelling a Python timer for every
        pointer sample and keeps USB work out of the Flet UI callback.
        """
        if not self._magnetic_controls_are_ready():
            return
        selected_slot = getattr(self, "magnetic_selected_slot", None)
        if selected_slot not in SK75_KEY_BY_SLOT:
            return
        if not self.magnetic_rt_separate_switch.value:
            # With the optional downstroke threshold disabled, use the ordinary
            # release threshold for both firmware fields.
            self._set_vertical_magnetic_value(
                self.magnetic_rt_press_slider,
                self.magnetic_rt_release_slider.value,
                # The repeat-down card is hidden in this mode.  Its state must
                # stay in sync for HID, but a second child patch here can race
                # the single parent patch from the RT switch event.
                update_controls=False,
            )
        if (
            not bool(getattr(self.magnetic_rt_switch, "value", True))
            and not bool(
                getattr(
                    getattr(self, "magnetic_deactivation_separate_switch", None),
                    "value",
                    True,
                )
            )
        ):
            # In ordinary mode the optional liftTravel ruler is hidden until
            # requested.  Persisting the same value as actuation makes the
            # toggle safe and maps exactly to the one official HID field.
            self._set_vertical_magnetic_value(
                self.magnetic_deactivation_slider,
                self.magnetic_actuation_slider.value,
                update_controls=False,
            )
        # The full SK75 deck is deliberately not rebuilt while dragging: that
        # makes the entire page jump.  Patch only the edited cap instead, so
        # its three corner values stay live on every 0.01-mm change and match
        # the ruler before the debounced HID write completes.
        # A mode toggle changes several siblings at once.  Its handler sends
        # one patch for ``magnetic_parameter_panel`` below; adding a keycap
        # patch in the middle of that reconciliation was the remaining source
        # of Flet's "dictionary changed size during iteration" exception when
        # the separate-RT switch was clicked repeatedly.  Slider gestures are
        # still allowed to patch their one keycap immediately.
        if (
            refresh_keycap
            and not getattr(self, "_magnetic_parameter_mode_transition", False)
        ):
            try:
                slot = selected_slot
                settings = self._magnetic_settings_from_controls()
                self._patch_magnetic_picker_keycap(
                    slot,
                    selected=(getattr(self, "magnetic_visual_selected_slot", None) == slot),
                    settings=settings,
                )
            except (AttributeError, TypeError, ValueError, MagneticProtocolError):
                # During first construction the deck may not be mounted yet.
                # The controls and scheduled write remain valid, and the first
                # paint will read the same current values.
                pass
        if self._magnetic_key_is_advanced(selected_slot):
            # Do not patch the status text for every raw drag sample on an
            # advanced key.  It is still shown for a click/final value so the
            # user receives the same explanation without an update flood.
            if refresh_keycap or not defer_hid:
                self._set_magnetic_status(
                    "Эта клавиша связана через Snap Key. Изменяйте пару в окне Snap Key.",
                    ft.Colors.AMBER_300,
                )
            return
        if not defer_hid:
            self._schedule_magnetic_key_write()

    def _ensure_magnetic_parameter_mode_transition_lock(self):
        """Return the lock that owns a structural Magnetic Lab UI mutation.

        Production instances initialise it before Flet controls are built.
        The lazy fallback keeps the same transaction guarantee for focused
        protocol/UI tests that intentionally construct a manager via
        ``__new__``.
        """
        lock = getattr(self, "_magnetic_parameter_mode_transition_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._magnetic_parameter_mode_transition_lock = lock
        return lock

    def _commit_magnetic_parameter_mode_transition(
        self, *, patch_picker=False, prepare=None
    ):
        """Apply a mode/toggle change with one stable parent UI patch.

        The switch event changes visibility, optional ruler values and a
        debounced HID payload.  Keep all mutations local first, then patch the
        fixed parameter-panel parent once.  The optional separate-threshold
        switches deliberately do not patch a keycap in between: Flet may
        otherwise reconcile a changing set of children while the page is
        scrolled.  The primary RT mode keeps its immediate keycap metrics.
        """
        # Treat every mode toggle as one atomic UI transaction.  Native Flet
        # switch events can arrive back-to-back; allowing a selected keycap or
        # a ruler child to update halfway through the parent visibility change
        # is exactly what made the editor crash or become visually inert after
        # several quick clicks.
        lock = self._ensure_magnetic_parameter_mode_transition_lock()
        with lock:
            # A nested/native duplicate click has already updated the Switch
            # value; the outer transaction reads that latest value before it
            # patches the stable parent.  Running a second child-visibility
            # mutation halfway through was the remaining structural race.
            if bool(getattr(self, "_magnetic_parameter_mode_transition", False)):
                return False
            self._magnetic_parameter_mode_transition = True
            try:
                # Optional state linking (separate RT/deactivation) must be
                # inside this transaction too.  Calling it in the native
                # switch handler beforehand still changed child visibility
                # outside the lock and left a narrow Flet reconciliation race.
                if callable(prepare):
                    prepare()
                self._update_magnetic_parameter_mode_ui(update=False)
                self._on_magnetic_control_changed()
                self._update_magnetic_parameter_mode_ui(update=True)

                # The primary RT mode changes the meaning of the three corner
                # metrics.  Paint that one independent keycap only *after* the
                # stable parameter parent has been patched; it no longer shares
                # a reconciliation turn with an add/remove visibility change.
                if patch_picker:
                    selected_slot = getattr(self, "magnetic_selected_slot", None)
                    if selected_slot in SK75_KEY_BY_SLOT:
                        try:
                            self._patch_magnetic_picker_keycap(
                                selected_slot,
                                selected=(
                                    getattr(self, "magnetic_visual_selected_slot", None)
                                    == selected_slot
                                ),
                                settings=self._magnetic_settings_from_controls(),
                            )
                        except (
                            AttributeError,
                            TypeError,
                            ValueError,
                            MagneticProtocolError,
                        ):
                            # The key deck can still be detached during startup;
                            # a normal refresh will read the same values later.
                            pass
            finally:
                self._magnetic_parameter_mode_transition = False
        return True

    def _on_magnetic_rt_changed(self):
        self._commit_magnetic_parameter_mode_transition(patch_picker=True)

    def _on_magnetic_rt_separation_changed(self):
        separate = bool(self.magnetic_rt_separate_switch.value)
        self._store_magnetic_rt_separate(self.magnetic_selected_slot, separate)
        self._commit_magnetic_parameter_mode_transition(
            prepare=lambda: self._update_magnetic_rt_separation_ui(update=False)
        )

    def _on_magnetic_deactivation_separation_changed(self):
        """Toggle independent ordinary deactivation without inventing state."""
        self._commit_magnetic_parameter_mode_transition(
            prepare=lambda: self._update_magnetic_deactivation_separation_ui(
                update=False
            )
        )

    def _schedule_magnetic_key_write(self, delay=0.22):
        """Coalesce a scale-drag gesture into one safe per-key HID transaction."""
        # A queued native click can arrive just after tray quit has detached
        # the Flet session.  Keep its local controls untouched rather than
        # arming another daemon timer that could outlive the window.
        if not getattr(self, "app_alive", True):
            return
        if not self._magnetic_controls_are_ready():
            return
        try:
            slot = self.magnetic_selected_slot
            if slot not in SK75_KEY_BY_SLOT:
                return
            settings = self._magnetic_settings_from_controls()
            packets = MagneticProtocol.key_settings_packets(slot, settings)
        except (TypeError, ValueError, MagneticProtocolError) as exc:
            self._set_magnetic_status(f"Проверьте параметры магнитной клавиши: {exc}", ft.Colors.ERROR)
            return
        if self._magnetic_key_is_advanced(slot):
            return
        profile_index = self._selected_magnetic_profile_index()

        with self._magnetic_write_lock:
            revision = self._magnetic_write_revisions.get(slot, 0) + 1
            self._magnetic_write_revisions[slot] = revision
            previous_timer = self._magnetic_write_timers.get(slot)
            if previous_timer is not None:
                previous_timer.cancel()
            self._magnetic_pending_key_writes[slot] = (
                settings,
                packets,
                revision,
                profile_index,
            )
            timer = threading.Timer(
                delay,
                self._write_magnetic_key_automatically,
                args=(slot, settings, packets, revision, profile_index),
            )
            timer.daemon = True
            self._magnetic_write_timers[slot] = timer
            timer.start()

    def _write_magnetic_key_automatically(self, slot, settings, packets, revision, profile_index):
        # ``Timer.cancel()`` is best effort: a callback that was already due
        # may still enter here while Quit is flushing the final accepted
        # intent.  The explicit flush owns its separate worker, so this stale
        # debounce must simply leave the pending intent for that drain.
        if not getattr(self, "app_alive", True):
            return
        with self._magnetic_write_lock:
            if self._magnetic_write_revisions.get(slot) != revision:
                return
        try:
            # Keep the physical-cache update in the same USB critical section
            # as the write.  Otherwise a profile switch queued behind this
            # gesture can calculate its diff from values that are already
            # obsolete on the real keyboard.
            with self.usb_lock:
                with self._magnetic_write_lock:
                    if not QMKManager._claim_magnetic_key_write_locked(self, slot, revision, "timer"):
                        return
                self._send_lighting_packets_locked(
                    packets,
                    f"magnetic_key_{slot}",
                    inter_packet_delay=0.01,
                )
                # A queued import/profile change can invalidate this intent
                # while the synchronous HID call is in progress.  The packet
                # cannot be unsent, but it must not mutate or mirror the new
                # configuration after that cancellation boundary.
                with self._magnetic_write_lock:
                    if self._magnetic_write_revisions.get(slot) != revision:
                        return
                self._cache_magnetic_settings(
                    slot,
                    settings,
                    profile_index=profile_index,
                )
            if getattr(self, "_womier_cache_sync_lock", None) is not None:
                self._queue_womier_cache_sync(
                    profile_index,
                    key_settings={slot: settings},
                    key_modes={
                        slot: MagneticProtocol.MODE_NORMAL
                        | (
                            MagneticProtocol.MODE_RAPID_TRIGGER_BIT
                            if settings.rapid_trigger
                            else 0
                        )
                    },
                )
            # The keyboard/cache state above is authoritative immediately.
            # Serialising the full config is intentionally batched below so
            # a run of exact +/- edits cannot stall the next slider frame.
            schedule_persistence = getattr(self, "_schedule_magnetic_persistence", None)
            if callable(schedule_persistence) and isinstance(getattr(self, "config", None), dict):
                schedule_persistence()
            else:
                self.save_config(reload_runtime=False)
        except (MagneticProtocolError, LightingProtocolError) as exc:
            with self._magnetic_write_lock:
                is_current = self._magnetic_write_revisions.get(slot) == revision
            if is_current:
                self._set_magnetic_status(str(exc), ft.Colors.ERROR)
            return
        finally:
            QMKManager._finish_magnetic_key_write(self, slot, revision, "timer")

        with self._magnetic_write_lock:
            is_current = self._magnetic_write_revisions.get(slot) == revision
        if not is_current:
            return

        # The local vertical bars already show the new values.  Rebuilding the
        # complete visual keyboard here used to happen after every debounced
        # drag packet; that rebuild calls ``page.update()`` and makes the
        # whole magnetic page jump while a slider is being moved.  The cached
        # settings are used the next time the layout is refreshed normally
        # (key/profile selection), without disturbing the current gesture.

    def _schedule_magnetic_options_write(self, delay=0.25):
        if not getattr(self, "app_alive", True):
            return
        try:
            rt_stab = int(self.magnetic_rt_stab_dropdown.value)
            anti_accidental = bool(self.magnetic_anti_accidental_switch.value)
        except (TypeError, ValueError):
            return
        profile_index = self._selected_magnetic_profile_index()
        with self._magnetic_write_lock:
            self._magnetic_options_revision += 1
            revision = self._magnetic_options_revision
            if self._magnetic_options_timer is not None:
                self._magnetic_options_timer.cancel()
            self._magnetic_pending_options_write = (
                rt_stab,
                anti_accidental,
                revision,
                profile_index,
            )
            timer = threading.Timer(
                delay,
                self._write_magnetic_options_automatically,
                args=(rt_stab, anti_accidental, revision, profile_index),
            )
            timer.daemon = True
            self._magnetic_options_timer = timer
            timer.start()

    def _write_magnetic_options_automatically(self, rt_stab, anti_accidental, revision, profile_index):
        if not getattr(self, "app_alive", True):
            return
        with self._magnetic_write_lock:
            if self._magnetic_options_revision != revision:
                return
        try:
            # Preserve the physical keyboard's non-visible fields (Fn index,
            # system and WASD swap), rather than borrowing them from another
            # local preset.
            with self.usb_lock:
                with self._magnetic_write_lock:
                    if not QMKManager._claim_magnetic_options_write_locked(self, revision, "timer"):
                        return
                current = self._live_magnetic_keyboard_options()
                options = KeyboardOptions(
                    fn_index=current.fn_index,
                    anti_accidental=anti_accidental,
                    rt_stab=rt_stab,
                    wasd_swap=current.wasd_swap,
                    system=current.system,
                )
                self._send_lighting_packets_locked(
                    [MagneticProtocol.keyboard_options_packet(options)],
                    "magnetic_kboption",
                    inter_packet_delay=0.0,
                )
                with self._magnetic_write_lock:
                    if self._magnetic_options_revision != revision:
                        return
                self._cache_magnetic_keyboard_options(
                    options,
                    profile_index=profile_index,
                )
            if getattr(self, "_womier_cache_sync_lock", None) is not None:
                self._queue_womier_cache_sync(
                    profile_index, rt_stab=options.rt_stab
                )
            schedule_persistence = getattr(self, "_schedule_magnetic_persistence", None)
            if callable(schedule_persistence) and isinstance(getattr(self, "config", None), dict):
                schedule_persistence()
            else:
                self.save_config(reload_runtime=False)
        except (MagneticProtocolError, LightingProtocolError) as exc:
            with self._magnetic_write_lock:
                is_current = self._magnetic_options_revision == revision
            if is_current:
                self._set_magnetic_status(str(exc), ft.Colors.ERROR)
            return
        finally:
            QMKManager._finish_magnetic_options_write(self, revision, "timer")
        with self._magnetic_write_lock:
            if self._magnetic_options_revision != revision:
                return

    def _select_sk75_key(self, slot):
        key = SK75_KEY_BY_SLOT.get(slot)
        if key is None:
            return
        previous_slot = getattr(self, "magnetic_selected_slot", None)
        previous_visual_slot = getattr(self, "magnetic_visual_selected_slot", None)
        # A fresh Magnetic Lab has no implicit control target.  The first
        # click, including Q, must therefore always create a real selection.
        if previous_visual_slot == slot:
            # The same key is a deliberate toggle, not a no-op.  Return the
            # editor to its neutral, locked state without scheduling a HID
            # write; a user may click the selected cap again simply to stop
            # editing it.  Capture its current visible values first so the
            # one repainted cap does not briefly lose a just-adjusted metric
            # while an earlier debounce write is still pending.
            try:
                selected_settings = self._magnetic_settings_from_controls()
            except (AttributeError, TypeError, ValueError, MagneticProtocolError):
                selected_settings = self._cached_magnetic_settings(slot)
            self.magnetic_selected_slot = None
            self.magnetic_visual_selected_slot = None
            self._load_magnetic_controls(None, update=True)
            painted = self._patch_magnetic_picker_keycap(
                slot,
                selected=False,
                settings=selected_settings,
            )
            if not painted:
                # Same first-mount fallback as a normal selection, kept
                # outside the one-key fast path used by the mounted deck.
                self._refresh_sk75_keyboard_picker()
            return
        # Capture the controls before retargeting them, so a just-adjusted
        # old key immediately keeps its visible magnetic-value badges even while its
        # small automatic HID write is still waiting in the debounce queue.
        try:
            previous_settings = self._magnetic_settings_from_controls()
        except (AttributeError, TypeError, ValueError, MagneticProtocolError):
            previous_settings = None
        self.magnetic_selected_slot = slot
        self.magnetic_visual_selected_slot = slot
        self._load_magnetic_controls(slot, update=True)
        selected_settings = self._magnetic_settings_for_keyboard(slot)
        old_painted = self._patch_magnetic_picker_keycap(
            previous_slot, selected=False, settings=previous_settings
        )
        new_painted = self._patch_magnetic_picker_keycap(
            slot, selected=True, settings=selected_settings
        )
        if not (old_painted and new_painted):
            # This fallback only covers a click racing the initial Flet mount;
            # normal keyboard selection never rebuilds the 81-key deck.
            self._refresh_sk75_keyboard_picker()

    @staticmethod
    def _magnetic_settings_to_config(settings):
        return _magnetic_settings_config_values(
            MagneticProtocol.clamp_key_settings_to_official_bounds(settings)
        )

    @staticmethod
    def _default_magnetic_settings():
        return KeyMagneticSettings(1.50, True, 0.15, 0.20, 0.05, 0.10)

    def _cached_magnetic_settings(self, slot):
        # Return a detached settings value while the config lock is held.  A
        # timer may be adding the latest HID result to the profile at the same
        # time a keycap/slider asks for it; reading the nested map without this
        # boundary was another narrow route to a size-changing dictionary.
        with _CONFIG_WRITE_LOCK:
            entry = self._active_device() or {}
            profile = self._magnetic_profile_slot()
            stored = (profile or {}).get("key_settings", {}).get(str(slot))
            settings = self._magnetic_settings_from_config(stored)
            if settings is not None:
                return settings
            return self._magnetic_settings_from_config(
                (entry.get("magnetic_key_settings") or {}).get(str(slot))
            )

    def _cache_magnetic_settings(self, slot, settings, *, profile_index=None):
        # Timer-based HID writes can finish during a switch event.  Use the
        # same small state lock as the switch's profile choice before touching
        # persisted nested maps.
        with _CONFIG_WRITE_LOCK:
            entry = self._active_device()
            if entry is None:
                return
            stored = self._magnetic_settings_to_config(settings)
            entry.setdefault("magnetic_key_settings", {})[str(slot)] = stored
            mode = MagneticProtocol.MODE_NORMAL | (
                MagneticProtocol.MODE_RAPID_TRIGGER_BIT if settings.rapid_trigger else 0
            )
            entry.setdefault("magnetic_key_modes", {})[str(slot)] = mode
            profile = self._magnetic_profile_slot(profile_index)
            if profile is not None:
                profile.setdefault("key_settings", {})[str(slot)] = _json_copy(stored)
                profile.setdefault("key_modes", {})[str(slot)] = mode
                profile["initialized"] = True

    def _store_magnetic_settings(self, slot, settings, *, profile_index=None):
        self._cache_magnetic_settings(slot, settings, profile_index=profile_index)
        self.save_config(reload_runtime=False)

    def _set_magnetic_controls_ready_state(self, ready, *, update=True):
        """Lock magnetic editing until the startup HID read supplies real values.

        A cached preset can be useful for profile storage, but it must not
        look like a confirmed physical keyboard value on first launch.  While
        waiting, all five rulers deliberately display 0.00 mm and reject
        drag/step events.  No configuration or HID write occurs in this path.
        """
        ready = bool(ready)
        self._magnetic_values_ready = ready
        sliders = (
            getattr(self, "magnetic_actuation_slider", None),
            getattr(self, "magnetic_deactivation_slider", None),
            getattr(self, "magnetic_rt_release_slider", None),
            getattr(self, "magnetic_rt_press_slider", None),
            getattr(self, "magnetic_lower_dead_zone_slider", None),
            getattr(self, "magnetic_upper_dead_zone_slider", None),
        )
        for slider in sliders:
            if slider is None:
                continue
            slider.interaction_enabled = ready
            if not ready:
                # Do not use _set_vertical_magnetic_value here: it clamps to
                # a hardware minimum and would turn the requested neutral zero
                # display into 0.10/0.01 before the keyboard has replied.
                slider.value = 0.0
            # Startup/readiness can change every ruler at once.  Keep the
            # individual paints local and flush their common parent below so a
            # background read never races a native switch event through six
            # separate Flet patches.
            self._paint_vertical_magnetic_control(slider, update_controls=False)
        for control in (
            getattr(self, "magnetic_rt_switch", None),
            getattr(self, "magnetic_rt_separate_switch", None),
            getattr(self, "magnetic_deactivation_separate_switch", None),
        ):
            if control is None:
                continue
            control.disabled = not ready
        if update:
            self._patch_magnetic_parameter_panel()

    def _set_magnetic_controls_no_selection_state(self, *, update=True):
        """Show a neutral, locked editor until a physical key is selected.

        This deliberately does *not* change ``_magnetic_values_ready``.  The
        startup HID read can finish while no key is selected; its verified
        values stay cached for the first click, but the UI must still show
        zero and reject input rather than looking like Q is the target.
        """
        for slider in (
            getattr(self, "magnetic_actuation_slider", None),
            getattr(self, "magnetic_deactivation_slider", None),
            getattr(self, "magnetic_rt_release_slider", None),
            getattr(self, "magnetic_rt_press_slider", None),
            getattr(self, "magnetic_lower_dead_zone_slider", None),
            getattr(self, "magnetic_upper_dead_zone_slider", None),
        ):
            if slider is None:
                continue
            slider.interaction_enabled = False
            slider.value = 0.0
            self._paint_vertical_magnetic_control(slider, update_controls=False)
        for control in (
            getattr(self, "magnetic_rt_switch", None),
            getattr(self, "magnetic_rt_separate_switch", None),
            getattr(self, "magnetic_deactivation_separate_switch", None),
        ):
            if control is None:
                continue
            control.disabled = True
        if update:
            self._patch_magnetic_parameter_panel()

    def _magnetic_controls_are_ready(self):
        """Return true for live app instances and test managers without state."""
        return bool(getattr(self, "_magnetic_values_ready", True))

    def _load_magnetic_controls(self, slot, *, update=True):
        """Load one key's values into the already-mounted magnetic controls.

        ``update`` intentionally patches only the handful of affected fields.
        Selecting a key must not require rebuilding all 81 keycaps just to
        make its five vertical controls and two switches reflect the cache.
        """
        if slot not in SK75_KEY_BY_SLOT:
            self._set_magnetic_controls_no_selection_state(update=update)
            return
        if not self._magnetic_controls_are_ready():
            self._set_magnetic_controls_ready_state(False, update=update)
            return
        # Lightweight unit-test managers intentionally omit the live-startup
        # flag and construct only plain slider values.  Production instances
        # always have the flag and receive the full interactive-state paint.
        if hasattr(self, "_magnetic_values_ready"):
            self._set_magnetic_controls_ready_state(True, update=False)
        settings = self._cached_magnetic_settings(slot) or self._default_magnetic_settings()
        self.magnetic_rt_switch.value = settings.rapid_trigger
        # Load all values and dynamic visibility first, then patch their
        # stable parent once below.  Updating one child per field creates
        # visible selection lag on the 81-key deck.
        self._update_magnetic_rt_label(update=False)
        slider_values = (
            (self.magnetic_actuation_slider, settings.actuation),
            (self.magnetic_rt_release_slider, settings.rapid_release),
            (self.magnetic_rt_press_slider, settings.rapid_press),
            (self.magnetic_lower_dead_zone_slider, settings.lower_dead_zone),
            (self.magnetic_upper_dead_zone_slider, settings.upper_dead_zone),
        )
        deactivation_slider = getattr(self, "magnetic_deactivation_slider", None)
        if deactivation_slider is not None:
            slider_values = (
                (self.magnetic_actuation_slider, settings.actuation),
                (deactivation_slider, settings.deactivation),
                *slider_values[1:],
            )
        # A key selection replaces all values at once.  Keep those mutations
        # off the wire until the stable editor parent is complete below; six
        # child updates here could overlap a pending RT visibility update.
        for slider, value in slider_values:
            self._set_vertical_magnetic_value(
                slider, value * 100, update_controls=False
            )
        self.magnetic_rt_separate_switch.value = self._magnetic_rt_is_separate(slot, settings)
        deactivation_switch = getattr(
            self, "magnetic_deactivation_separate_switch", None
        )
        if deactivation_switch is not None:
            deactivation_switch.value = self._magnetic_deactivation_is_separate(settings)
        self._update_magnetic_rt_separation_ui(update=False)
        self._update_magnetic_deactivation_separation_ui(update=False)
        if update:
            self._patch_magnetic_parameter_panel()

    def _magnetic_settings_for_keyboard(self, slot):
        if slot == self.magnetic_selected_slot and self._magnetic_controls_are_ready():
            try:
                return self._magnetic_settings_from_controls()
            except (TypeError, ValueError, MagneticProtocolError):
                pass
        return self._cached_magnetic_settings(slot)

    def _magnetic_key_caption(self, slot):
        settings = self._magnetic_settings_for_keyboard(slot)
        if settings is None:
            return "—", "RT —"
        if settings.rapid_trigger:
            return f"{settings.actuation:.2f}", f"RT {settings.rapid_release:.2f}/{settings.rapid_press:.2f}"
        deactivation = settings.actuation if settings.deactivation is None else settings.deactivation
        return (
            f"{settings.actuation:.2f}",
            f"деактивация {float(deactivation):.2f}",
        )

    def _magnetic_key_compact_caption(self, slot, settings, advanced):
        """Return a short, always-fitting metric for a visual 1U keycap."""
        # A Snap Key is still a physical magnetic key with its own measured
        # values.  Keep the point and the ordinary release RT visible on amber
        # caps too; the marker and tooltip identify Snap mode without
        # concealing useful information.
        if settings is None:
            return ""
        if settings.rapid_trigger:
            # The optional repeat-down threshold stays in the controls.  On
            # the keyboard itself this leaves a legible point + ordinary
            # release threshold
            # without text overflowing into the neighbouring keycap.
            return f"{settings.actuation:.2f} · ↑{settings.rapid_release:.2f}"
        deactivation = settings.actuation if settings.deactivation is None else settings.deactivation
        return f"{settings.actuation:.2f} · {float(deactivation):.2f}"

    @staticmethod
    def _magnetic_key_corner_metrics(settings):
        """Return the real magnetic values in the driver's corner positions.

        The official Womier view uses the colour and the corner position to
        identify each value.  Keeping the values themselves as plain numbers
        makes them easier to scan and, importantly, leaves the leading zero
        in Rapid Trigger thresholds (``0.30``, not ``.30``).

        Every cap keeps the same three corner positions.  With Rapid Trigger
        off, the yellow upper-right number becomes the *ordinary* firmware
        deactivation threshold (``liftTravel``), while the lower-right
        repeat-down position deliberately becomes a neutral em dash.  This
        avoids showing stale RT values or pretending there is a second normal
        deactivation setting.
        """
        if settings is None:
            return ()
        if not settings.rapid_trigger:
            deactivation = float(
                settings.actuation
                if settings.deactivation is None
                else settings.deactivation
            )
            return (
                (
                    "top_left",
                    f"{settings.actuation:.2f}",
                    MAGNETIC_METRIC_COLORS["actuation"],
                ),
                (
                    "top_right",
                    f"{deactivation:.2f}",
                    MAGNETIC_METRIC_COLORS["rapid_release"],
                ),
                ("bottom_right", "—", ft.Colors.OUTLINE_VARIANT),
            )

        return (
            # Official-driver corner order.  The three values are deliberately
            # plain two-decimal numbers: colour and the permanent guide below
            # the board explain their roles without wasting a keycap's width
            # on A/up/down prefixes or badge backgrounds.
            (
                "top_left",
                f"{settings.actuation:.2f}",
                MAGNETIC_METRIC_COLORS["actuation"],
            ),
            (
                "top_right",
                f"{settings.rapid_release:.2f}",
                MAGNETIC_METRIC_COLORS["rapid_release"],
            ),
            (
                "bottom_right",
                f"{settings.rapid_press:.2f}",
                MAGNETIC_METRIC_COLORS["rapid_press"],
            ),
        )

    @staticmethod
    def _magnetic_key_tooltip(display_label, settings):
        """Describe the live metrics without making the cap text verbose."""
        if settings is None:
            return f"{display_label}: магнитные значения ещё не считаны"
        if settings.rapid_trigger:
            return (
                f"{display_label}: точка активации {settings.actuation:.2f} мм; "
                f"RT при отпускании {settings.rapid_release:.2f} мм; "
                f"RT при повторном нажатии вниз {settings.rapid_press:.2f} мм"
            )
        deactivation = float(
            settings.actuation
            if settings.deactivation is None
            else settings.deactivation
        )
        return (
            f"{display_label}: точка активации {settings.actuation:.2f} мм; "
            f"точка деактивации {deactivation:.2f} мм; Rapid Trigger выключен"
        )

    def _build_sk75_keyboard_layout(
        self,
        on_click,
        selected_colors=None,
        compact=False,
        on_secondary_click=None,
        capture_magnetic_keycaps=False,
        deck_width=None,
    ):
        """Render the SK75 in the compact physical order of Womier's driver."""
        selected_colors = selected_colors or {}
        if capture_magnetic_keycaps:
            # References make the ordinary Magnetic Lab selector a local
            # two-key repaint.  The Snap dialog deliberately does not opt in:
            # it owns a separate draft keyboard and still rebuilds only its
            # small modal when its own selection changes.
            self._magnetic_picker_keycaps = {}
        geometry = _sk75_visual_deck_geometry(
            compact=compact,
            deck_width=deck_width,
        )
        key_height = 42 if compact else 56
        key_corner_radius = 7 if compact else 9
        deck_width = geometry.deck_width
        deck_padding = geometry.deck_padding
        row_width = geometry.row_width
        rows = []
        for row_index, layout_row in enumerate(SK75_OFFICIAL_VISUAL_LAYOUT):
            key_controls = []
            for key_index, (slot, label, _width) in enumerate(layout_row):
                selected = slot in selected_colors
                selected_background, selected_foreground = selected_colors.get(
                    slot, (SK75_VISUAL_KEY_SELECTED_BACKGROUND, ft.Colors.ON_PRIMARY_CONTAINER)
                )
                if selected_background is None:
                    selected_background = SK75_VISUAL_KEY_SELECTED_BACKGROUND
                settings = self._magnetic_settings_for_keyboard(slot)
                snap_key = self._magnetic_key_is_snap(slot)
                key_x = geometry.key_x_positions[row_index][key_index]
                key_width = geometry.key_widths[row_index][key_index]
                # Keep the deck flat and close to Womier's driver: individual
                # caps carry the data, not a fake 3D keyboard case or a
                # painted actuation band behind every label.
                base_background = SK75_VISUAL_KEY_BACKGROUND
                display_label = {
                    "Back": "Backspace",
                    "R Shift": "Shift",
                }.get(label, label)
                label_control = ft.Container(
                    content=ft.Text(
                        display_label,
                        size=10 if compact else 12,
                        weight=ft.FontWeight.W_600,
                        color=selected_foreground if selected else ft.Colors.ON_SURFACE,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    width=max(20, key_width - 12),
                    alignment=ft.Alignment.CENTER,
                )
                settings_tooltip = self._magnetic_key_tooltip(display_label, settings)
                tooltip = (
                    f"{settings_tooltip}; Snap Key"
                    if snap_key else
                    settings_tooltip
                )
                key_stack_controls = [
                    ft.Container(
                        content=label_control,
                        width=key_width,
                        height=key_height - (12 if compact else 20),
                        top=6 if compact else 10,
                        alignment=ft.Alignment.CENTER,
                    )
                ]
                metric_controls = {}
                if not compact:
                    corner_positions = {
                        "top_left": {"top": 3, "left": 3},
                        "top_right": {"top": 3, "right": 3},
                        "bottom_right": {"bottom": 3, "right": 3},
                    }
                    # Values are bold coloured text only, in the same outer
                    # corners as the official driver.  Give every corner its
                    # own full interior width: Stack lets these three narrow
                    # text rows overlap, but their left/right alignment keeps
                    # a metric on the true edge of *any* key, including
                    # Space, Backspace, Enter and both Shift keys.  Splitting
                    # a long cap into percentage columns put its RT values in
                    # the middle instead of at the physical right edge.
                    metric_width = max(1, key_width - 6)
                    for corner, value, metric_color in self._magnetic_key_corner_metrics(settings):
                        metric_text = ft.Text(
                            value,
                            size=9,
                            weight=ft.FontWeight.W_800,
                            color=metric_color,
                            no_wrap=True,
                            # A complete threshold is more useful than an
                            # ellipsis.  Full-size caps have room for the
                            # longest valid ``↓3.50`` representation.
                            overflow=ft.TextOverflow.CLIP,
                            text_align=(
                                ft.TextAlign.LEFT
                                if corner == "top_left"
                                else ft.TextAlign.RIGHT
                            ),
                        )
                        metric_control = ft.Container(
                            content=metric_text,
                            width=metric_width,
                            height=11,
                            padding=0,
                            # ``Text.text_align`` alone does not alter the
                            # child's placement inside a full-width Container.
                            # Keep the three readouts physically pinned to
                            # their outer corners; otherwise the two top
                            # values occupy the same centre point and the
                            # later yellow RT text hides the red actuation.
                            alignment=(
                                ft.Alignment.CENTER_LEFT
                                if corner == "top_left"
                                else ft.Alignment.CENTER_RIGHT
                            ),
                            **corner_positions[corner],
                        )
                        metric_controls[corner] = (metric_control, metric_text)
                        key_stack_controls.append(metric_control)
                keycap = ft.Container(
                    content=ft.Stack(
                        key_stack_controls,
                        width=key_width,
                        height=key_height,
                        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    ),
                    width=key_width,
                    height=key_height,
                    alignment=ft.Alignment.CENTER,
                    padding=0,
                    ink=True,
                    ink_color=ft.Colors.PRIMARY_CONTAINER,
                    tooltip=tooltip,
                    bgcolor=selected_background if selected else base_background,
                    gradient=None,
                    border=ft.Border.all(
                        2 if selected or snap_key else 1,
                        SK75_VISUAL_KEY_SELECTED_BORDER if selected else ft.Colors.AMBER_400 if snap_key else SK75_VISUAL_KEY_BORDER,
                    ),
                    border_radius=key_corner_radius,
                    shadow=(
                        ft.BoxShadow(
                            blur_radius=8,
                            spread_radius=-2,
                            offset=(0, 0),
                            color=ft.Colors.with_opacity(0.55, SK75_VISUAL_KEY_SELECTED_BORDER),
                        )
                        if selected else None
                    ),
                )
                if capture_magnetic_keycaps:
                    # The selected ruler updates one corner value at a time.
                    # Persist a small local paint snapshot so that path can
                    # patch only the changed Text leaf instead of asking Flet
                    # to diff the full keycap (label, Stack, three badges,
                    # border, ink and shadow) after every 0.01-mm step.
                    metric_snapshot = {
                        corner: (value_text.value, value_text.color, badge.visible)
                        for corner, (badge, value_text) in metric_controls.items()
                    }
                    self._magnetic_picker_keycaps[slot] = SimpleNamespace(
                        keycap=keycap,
                        label_text=label_control.content,
                        metric_controls=metric_controls,
                        metric_snapshot=metric_snapshot,
                        selected=selected,
                        display_label=display_label,
                        snap_key=snap_key,
                        base_background=base_background,
                        base_foreground=ft.Colors.ON_SURFACE,
                        selected_background=selected_background,
                        selected_foreground=selected_foreground,
                    )
                if on_secondary_click is None:
                    keycap.on_click = lambda e, key_slot=slot: on_click(key_slot)
                    key_control = keycap
                else:
                    # A GestureDetector is needed because Container only has a
                    # primary click callback.  This keeps the ordinary
                    # magnetic-picker behavior intact and adds a precise
                    # right-click target exclusively where a second key is
                    # meaningful (Snap Key).
                    key_control = ft.GestureDetector(
                        content=keycap,
                        width=key_width,
                        height=key_height,
                        mouse_cursor=ft.MouseCursor.CLICK,
                        on_tap=lambda e, key_slot=slot: on_click(key_slot),
                        on_secondary_tap=lambda e, key_slot=slot: on_secondary_click(key_slot),
                    )
                # Absolute x positions are the physical 75% grid.  There is
                # intentionally no expanding row control between alpha and
                # navigation keys.
                key_control.left = key_x
                key_control.top = 0
                key_controls.append(key_control)

            rows.append(
                ft.Stack(
                    key_controls,
                    width=row_width,
                    height=key_height,
                    # The measured x coordinates keep every cap in bounds;
                    # letting shadows breathe into the intentional 10 px
                    # inter-key gaps makes this look like a keyboard, not a
                    # table with hard-clipped cells.
                    clip_behavior=ft.ClipBehavior.NONE,
                )
            )
        legend = [] if compact else [
            ft.Row(
                [
                    ft.Container(width=9, height=9, bgcolor=ft.Colors.PRIMARY, border_radius=5),
                    ft.Text("выбранная клавиша", size=10, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Container(width=9, height=9, bgcolor=ft.Colors.AMBER_400, border_radius=5),
                    ft.Text("Snap Key", size=10, color=ft.Colors.ON_SURFACE_VARIANT),
                ],
                spacing=6,
                alignment=ft.MainAxisAlignment.CENTER,
            )
        ]
        return ft.Container(
            content=ft.Column(
                [*legend, *rows],
                spacing=7 if compact else 10,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=deck_width,
            padding=deck_padding,
            alignment=ft.Alignment.CENTER,
            clip_behavior=ft.ClipBehavior.NONE,
        )

    def _refresh_sk75_keyboard_picker(self):
        deck_width = self._sk75_deck_width_for_current_viewport()
        self._sk75_rendered_deck_width = deck_width
        visual_selected_slot = getattr(self, "magnetic_visual_selected_slot", None)
        selected_colors = (
            {
                visual_selected_slot: (
                    ft.Colors.PRIMARY_CONTAINER,
                    ft.Colors.ON_PRIMARY_CONTAINER,
                )
            }
            if visual_selected_slot in SK75_KEY_BY_SLOT
            else {}
        )
        self.keyboard_picker_root.content = self._build_sk75_keyboard_layout(
            self._select_sk75_key,
            selected_colors,
            capture_magnetic_keycaps=True,
            deck_width=deck_width,
        )
        self._update_magnetic_rt_label()
        self._refresh_snap_key_summary(update=True)
        try:
            # A keycap refresh is local to the board.  A whole-page update is
            # expensive enough to make the selected key appear a frame late.
            self.keyboard_picker_root.update()
        except Exception:
            # During initial construction this root is not attached yet; the
            # normal first paint will include its freshly assigned content.
            pass

    def _patch_magnetic_picker_keycap(self, slot, *, selected, settings=None):
        """Repaint one already-mounted full-size keycap in place.

        The renderer stores a reference for each main Magnetic Lab cap. A
        selection change still updates the cap shell (border, shadow and
        centred label), but a ruler edit normally changes only one of the
        three corner metric texts. Flet 0.85 walks the whole subtree supplied
        to ``update()``, so keep that hot 0.01-mm path to the changed Text
        leaves rather than diffing the complete cap after every +/- click.
        The outer-cap update remains the safe fallback for a detached or
        settling leaf.
        """
        try:
            reference = getattr(self, "_magnetic_picker_keycaps", {}).get(slot)
        except (AttributeError, TypeError):
            reference = None
        if reference is None:
            return False

        keycap = reference.keycap
        selected = bool(selected)
        shell_dirty = getattr(reference, "selected", None) is not selected
        if selected:
            keycap.bgcolor = reference.selected_background
            keycap.gradient = None
            keycap.border = ft.Border.all(2, SK75_VISUAL_KEY_SELECTED_BORDER)
            keycap.shadow = ft.BoxShadow(
                blur_radius=8,
                spread_radius=-2,
                offset=(0, 0),
                color=ft.Colors.with_opacity(0.55, SK75_VISUAL_KEY_SELECTED_BORDER),
            )
            reference.label_text.color = reference.selected_foreground
        else:
            keycap.bgcolor = reference.base_background
            keycap.gradient = None
            keycap.border = ft.Border.all(
                2 if reference.snap_key else 1,
                ft.Colors.AMBER_400 if reference.snap_key else SK75_VISUAL_KEY_BORDER,
            )
            keycap.shadow = None
            reference.label_text.color = reference.base_foreground

        leaf_targets = []
        snapshot = getattr(reference, "metric_snapshot", None)
        if not isinstance(snapshot, dict):
            snapshot = {}
        if settings is not None:
            # Tooltip belongs to the cap shell. Keep its model current, but do
            # not let a hover-only string force a full keycap patch on every
            # exact ruler step. The next shell update safely publishes it.
            keycap.tooltip = self._magnetic_key_tooltip(
                getattr(reference, "display_label", "Клавиша"), settings
            )
            metrics = {
                corner: (value, color)
                for corner, value, color in self._magnetic_key_corner_metrics(settings)
            }
            # The deck can be rebuilt by a resize while a delayed value patch
            # arrives.  Take a snapshot to avoid iterating a map that Flet is
            # replacing during that reconciliation.
            for corner, (badge, value_text) in list(reference.metric_controls.items()):
                value_and_color = metrics.get(corner)
                previous_value, previous_color, previous_visible = snapshot.get(
                    corner,
                    (value_text.value, value_text.color, bool(badge.visible)),
                )
                if value_and_color is None:
                    badge.visible = False
                    if previous_visible:
                        leaf_targets.append(badge)
                    snapshot[corner] = (
                        value_text.value,
                        value_text.color,
                        False,
                    )
                    continue
                value, metric_color = value_and_color
                value_text.value = value
                value_text.color = metric_color
                badge.visible = True
                if previous_value != value or previous_color != metric_color:
                    leaf_targets.append(value_text)
                if not previous_visible:
                    leaf_targets.append(badge)
                snapshot[corner] = (value, metric_color, True)
        reference.metric_snapshot = snapshot

        # A selection/deselection deliberately affects the whole key shell.
        # It happens at click cadence, not slider cadence, so a single full
        # patch is both correct and visually immediate.
        if shell_dirty:
            try:
                keycap.update()
                reference.selected = selected
                return True
            except Exception:
                # Unit tests and first construction intentionally use detached
                # controls. Their mutable values are still correct for paint.
                return True

        if not leaf_targets:
            return True
        try:
            # De-duplicate a badge/text when a mode change both reveals it and
            # changes its contents. Text leaves preserve the live corner
            # values without a GPU redraw of the rest of the 75% layout.
            seen = set()
            for target in leaf_targets:
                target_id = id(target)
                if target_id in seen:
                    continue
                seen.add(target_id)
                target.update()
            return True
        except Exception:
            logger.debug(
                "could not patch magnetic key metric leaf; using keycap",
                exc_info=True,
            )
            try:
                keycap.update()
            except Exception:
                pass
        return True

    def _refresh_snap_key_summary(self, update=False):
        """Show stored Snap Key state without treating a dialog draft as live."""
        summary = getattr(self, "magnetic_snap_summary", None)
        if summary is None:
            return
        active = [
            key.label
            for key in SK75_KEYS
            if self._magnetic_key_is_advanced(key.slot)
        ]
        if not active:
            summary.value = "Нет активной пары. Выберите ЛКМ и ПКМ в окне."
        elif len(active) <= 2:
            summary.value = f"Активно: {' + '.join(active)}"
        else:
            summary.value = f"Активно: {' + '.join(active[:2])} и ещё {len(active) - 2}"
        if update:
            try:
                summary.update()
            except Exception:
                pass

    def _open_snap_key_dialog(self):
        """Select a Snap Key pair directly on a visual keyboard.

        The two mouse buttons deliberately have fixed roles.  The selection
        remains a local draft until ``Применить пару`` is pressed: Cancel and
        closing the dialog cannot leave a yellow Snap Key marker behind.
        """
        prompt = ft.Text(
            "ЛКМ — первая клавиша, ПКМ — вторая. Затем нажмите «Применить пару».",
            size=12,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        pair = ft.Text(size=13, weight=ft.FontWeight.W_600)
        keyboard = ft.Container(
            padding=10,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
            border_radius=14,
        )
        draft = {"first": None, "second": None}
        clear_all_slots = {"value": []}
        clear_all_confirmation = ft.Container(visible=False)

        def selected_name(slot, empty):
            return self._sk75_key_name(slot) if isinstance(slot, int) else empty

        def refresh():
            selected_colors = {}
            if isinstance(draft["first"], int):
                selected_colors[draft["first"]] = (
                    ft.Colors.SECONDARY_CONTAINER,
                    ft.Colors.ON_SECONDARY_CONTAINER,
                )
            if isinstance(draft["second"], int):
                selected_colors[draft["second"]] = (
                    ft.Colors.TERTIARY_CONTAINER,
                    ft.Colors.ON_TERTIARY_CONTAINER,
                )
            pair.value = (
                f"Пара: {selected_name(draft['first'], 'ЛКМ: выберите')} "
                f"+ {selected_name(draft['second'], 'ПКМ: выберите')}"
            )
            keyboard.content = self._build_sk75_keyboard_layout(
                select_first,
                selected_colors,
                compact=True,
                on_secondary_click=select_second,
            )

        def select_first(slot):
            draft["first"] = slot
            if draft["second"] == slot:
                draft["second"] = None
            prompt.value = "Первая клавиша выбрана. Нажмите ПКМ по её паре."
            prompt.color = ft.Colors.ON_SURFACE_VARIANT
            refresh()
            self.page.update()

        def select_second(slot):
            if not isinstance(draft["first"], int):
                prompt.value = "Сначала выберите первую клавишу левой кнопкой мыши."
                prompt.color = ft.Colors.ERROR
                self.page.update()
                return
            if slot == draft["first"]:
                prompt.value = "Для Snap Key нужны две разные клавиши."
                prompt.color = ft.Colors.ERROR
                self.page.update()
                return
            draft["second"] = slot
            prompt.value = "Пара готова. Нажмите «Применить пару» для записи в клавиатуру."
            prompt.color = ft.Colors.ON_SURFACE_VARIANT
            refresh()
            self.page.update()

        def apply_pair(event):
            first, second = draft["first"], draft["second"]
            if not isinstance(first, int) or not isinstance(second, int):
                prompt.value = "Выберите две разные клавиши: ЛКМ и ПКМ."
                prompt.color = ft.Colors.ERROR
                self.page.update()
                return
            self._magnetic_set_snap_pair(first, second)
            draft["first"] = None
            draft["second"] = None
            self.page.pop_dialog()

        def cancel_clear_all(_event=None):
            clear_all_confirmation.visible = False
            clear_all_slots["value"] = []
            prompt.value = "Очистка всех Snap Key отменена. Вы можете выбрать новую пару."
            prompt.color = ft.Colors.ON_SURFACE_VARIANT
            self.page.update()

        def confirm_clear_all(_event=None):
            slots = list(clear_all_slots["value"])
            if not slots:
                cancel_clear_all()
                return
            # The worker changes only mode + Snap-partner fields.  It does not
            # touch the actuation/RT/dead-zone values displayed on keycaps.
            self._magnetic_clear_all_snap_keys(slots)
            draft["first"] = None
            draft["second"] = None
            self.page.pop_dialog()

        def request_clear_all(_event=None):
            slots = self._known_snap_key_slots()
            if not slots:
                prompt.value = "Сохранённых Snap Key сейчас нет — очищать нечего."
                prompt.color = ft.Colors.ON_SURFACE_VARIANT
                self.page.update()
                return
            clear_all_slots["value"] = slots
            names = ", ".join(self._sk75_key_name(slot) for slot in slots[:6])
            suffix = "…" if len(slots) > 6 else ""
            clear_all_confirmation.content = ft.Column(
                [
                    ft.Text("Убрать все Snap Key?", weight=ft.FontWeight.W_600),
                    ft.Text(
                        f"Будут очищены {len(slots)} клавиш: {names}{suffix}. "
                        "Точки активации, RT и дез-зоны сохранятся.",
                        size=11,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    ft.Row(
                        [
                            ft.TextButton("Отмена", on_click=cancel_clear_all),
                            ft.FilledButton(
                                "Да, убрать все",
                                icon=ft.Icons.LINK_OFF_ROUNDED,
                                on_click=confirm_clear_all,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.END,
                        spacing=6,
                    ),
                ],
                spacing=5,
                tight=True,
            )
            clear_all_confirmation.visible = True
            clear_all_confirmation.padding = 10
            clear_all_confirmation.bgcolor = ft.Colors.ERROR_CONTAINER
            clear_all_confirmation.border = ft.Border.all(1, ft.Colors.ERROR)
            clear_all_confirmation.border_radius = 12
            prompt.value = "Подтвердите очистку ниже."
            prompt.color = ft.Colors.ERROR
            self.page.update()

        def clear_draft(_event=None):
            # Do not touch stored modes here: any amber dot on the ordinary
            # keyboard now means a deliberately applied pair, never a canceled
            # dialog draft.
            draft["first"] = None
            draft["second"] = None
            self.snap_first_slot = None
            self.snap_second_slot = None
            self._refresh_snap_key_summary()

        refresh()
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Snap Key · выбор пары"),
            content=ft.Container(
                ft.Column(
                    [
                        prompt,
                        ft.Row(
                            [
                                ft.Icon(ft.Icons.MOUSE_ROUNDED, size=16, color=ft.Colors.SECONDARY),
                                ft.Text("ЛКМ — первая", size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                                ft.Icon(ft.Icons.MOUSE_ROUNDED, size=16, color=ft.Colors.TERTIARY),
                                ft.Text("ПКМ — вторая", size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                            ],
                            spacing=6,
                        ),
                        pair,
                        keyboard,
                        clear_all_confirmation,
                    ],
                    spacing=10,
                    tight=True,
                ),
                width=820,
            ),
            actions=[
                ft.TextButton("Отмена", on_click=lambda e: self.page.pop_dialog()),
                ft.OutlinedButton(
                    "Убрать все Snap Key",
                    icon=ft.Icons.DELETE_SWEEP_ROUNDED,
                    on_click=request_clear_all,
                ),
                ft.FilledButton("Применить пару", icon=ft.Icons.LINK_ROUNDED, on_click=apply_pair),
            ],
            shape=ft.RoundedRectangleBorder(radius=20),
            on_dismiss=clear_draft,
        )
        self.page.show_dialog(dialog)

    def _paint_magnetic_travel_tester(
        self, ui, travel_mm, direction=None, *, active_slot=None
    ):
        """Patch the fixed live overlay, never the dialog's static subtree.

        ``visual_region`` used to include the complete illustrated switch,
        ruler and tick labels.  Updating it at sensor rate made the packaged
        Flutter renderer serialise and repaint static controls repeatedly.
        New dialogs provide ``dynamic_overlay`` instead; only the handful of
        controls which can move are children of that isolated subtree.

        Return ``False`` on a live Flet paint failure so the async owner can
        stop the firmware stream and show one clear error.  A bare ``except:
        pass`` here used to leave a running HID session with a frozen meter.
        """
        try:
            travel_mm = max(0.0, min(float(travel_mm), ui.full_travel_mm))
            # The label has two decimal places, and the rail has a finite
            # pixel height.  Coalesce finer raw changes before entering
            # Flet's patch pipeline: no user-visible value is lost, while a
            # high-rate report stream no longer creates identical GPU work.
            visible_mm = round(travel_mm, 2)
            progress = visible_mm / ui.full_travel_mm
            fill_height = round(ui.meter_inner_height * progress)
            rail_height = getattr(
                ui,
                "meter_rail_height",
                ui.meter_top + ui.meter_inner_height + ui.meter_cursor.height,
            )
            cursor_top = max(
                0,
                min(
                    rail_height - ui.meter_cursor.height,
                    round(ui.meter_top + ui.meter_inner_height * progress - 1),
                ),
            )
            # Zero travel means zero travel: the old five-pixel minimum left
            # a neutral-grey strip visible above the moving switch at 0.00 mm.
            switch_fill_height = round(70 * progress)
            switch_stem_top = round(10 + 46 * progress)
            render_state = (
                visible_mm,
                direction,
                active_slot,
                fill_height,
                cursor_top,
                switch_fill_height,
                switch_stem_top,
            )
            if getattr(ui, "_last_travel_render_state", None) == render_state:
                return True
            ui._last_travel_render_state = render_state

            # A depth alone is not a press state: a key can be held anywhere
            # along its travel.  Colour the test from its stable physical
            # direction.  Flat in-rail overlays deliberately replace dynamic
            # gradients and blurred shadows here: the previous effects forced
            # expensive shader repaints for every noisy report in packaged
            # builds, while these retain the same readable glow/cue.
            is_downstroke = direction == "down"
            is_upstroke = direction == "up"
            accent = (
                ft.Colors.GREEN_400
                if is_downstroke
                else ft.Colors.CYAN_ACCENT_400
                if is_upstroke
                else ft.Colors.ON_SURFACE_VARIANT
            )
            accent_container = (
                ft.Colors.GREEN_900
                if is_downstroke
                else ft.Colors.CYAN_900
                if is_upstroke
                else ft.Colors.SURFACE_CONTAINER_HIGHEST
            )
            direction_changed = getattr(ui, "_last_travel_direction", object()) != direction
            ui._last_travel_direction = direction

            ui.meter_fill.height = fill_height
            meter_fill_glow = getattr(ui, "meter_fill_glow", None)
            if meter_fill_glow is not None:
                meter_fill_glow.height = fill_height
            ui.meter_cursor.top = cursor_top
            # At the physical zero there is no active travel segment for a
            # cursor halo to cap.  Hiding both cursor layers removes the grey
            # strip which otherwise protruded above a zero-height fill.
            cursor_opacity = 1.0 if fill_height > 0 else 0.0
            ui.meter_cursor.opacity = cursor_opacity
            meter_cursor_glow = getattr(ui, "meter_cursor_glow", None)
            if meter_cursor_glow is not None:
                meter_cursor_glow.opacity = cursor_opacity
                meter_cursor_glow.top = max(
                    0,
                    min(
                        rail_height - meter_cursor_glow.height,
                        cursor_top - (meter_cursor_glow.height - ui.meter_cursor.height) // 2,
                    ),
                )
            ui.switch_fill.height = switch_fill_height
            ui.switch_stem.top = switch_stem_top

            if direction_changed:
                ui.meter_fill.bgcolor = accent
                ui.meter_cursor.bgcolor = accent
                ui.switch_fill.bgcolor = accent
                ui.switch_stem.bgcolor = accent_container
                ui.switch_stem.border = ft.Border.all(2, accent)
                ui.switch_chamber.border = ft.Border.all(2, accent)
                if meter_fill_glow is not None:
                    meter_fill_glow.bgcolor = ft.Colors.with_opacity(0.18, accent)
                if meter_cursor_glow is not None:
                    meter_cursor_glow.bgcolor = ft.Colors.with_opacity(0.42, accent)

            ui.value_text.value = f"{visible_mm:.2f} мм"
            ui.value_text.color = accent
            key_text = getattr(ui, "key_text", None)
            if key_text is not None and active_slot in SK75_KEY_BY_SLOT:
                key_text.value = f"Проверяется: {self._sk75_key_name(active_slot)}"
                key_text.color = accent
            ui.state_text.value = (
                "Вниз · зелёный"
                if is_downstroke
                else "Вверх · голубой"
                if is_upstroke
                else "Ожидание движения"
            )
            ui.state_text.color = accent
            # One local patch for the moving overlay avoids queuing a separate
            # Flet update for every part.  The immutable meter shell, ticks
            # and switch body intentionally live outside this subtree.
            dynamic_overlay = getattr(ui, "dynamic_overlay", None)
            if dynamic_overlay is not None:
                dynamic_overlay.update()
            else:
                # Compatibility for integrations that pass a minimal UI object
                # or for older test harnesses.  ``visual_region`` remains a
                # local fallback, never a full page update.
                visual_region = getattr(ui, "visual_region", None)
                if visual_region is not None:
                    visual_region.update()
                else:
                    for control in (
                        ui.meter_fill,
                        ui.meter_cursor,
                        ui.value_text,
                        ui.state_text,
                    ):
                        update = getattr(control, "update", None)
                        if callable(update):
                            update()
            return True
        except Exception as exc:
            # A late dialog-dismiss can legitimately detach controls.  Do not
            # turn that expected shutdown race into a scary error, but never
            # silently swallow an exception while this tester still owns the
            # firmware report stream.
            tester = getattr(self, "_magnetic_travel_tester", None)
            stop_event = getattr(tester, "stop_event", None)
            is_live_tester = (
                tester is not None
                and getattr(tester, "ui", None) is ui
                and stop_event is not None
                and not stop_event.is_set()
            )
            if is_live_tester:
                error_text = "Не удалось обновить индикатор проверки хода."
                if not getattr(ui, "_travel_paint_error_logged", False):
                    ui._travel_paint_error_logged = True
                    logger.exception("magnetic travel tester paint failed")
                tester.paint_error = error_text
            return False

    def _stop_magnetic_travel_tester(self, reset_ui=True, expected_dialog_token=None):
        """End a tester session without affecting any other keyboard hook."""
        tester = getattr(self, "_magnetic_travel_tester", None)
        if tester is None:
            return
        # Flet dispatches an AlertDialog's dismiss event asynchronously.  If a
        # user closes one tester and immediately opens another, the late
        # dismiss of the old dialog must never stop the new session.
        if (
            expected_dialog_token is not None
            and getattr(tester, "dialog_token", None) != expected_dialog_token
        ):
            return
        tester.stop_event.set()
        self._magnetic_travel_tester = None
        self._magnetic_travel_tester_token = getattr(
            self, "_magnetic_travel_tester_token", 0
        ) + 1
        if reset_ui:
            self._paint_magnetic_travel_tester(tester.ui, 0.0, None)
            try:
                tester.ui.start_button.disabled = False
                tester.ui.stop_button.disabled = True
                tester.ui.start_button.update()
                tester.ui.stop_button.update()
            except Exception:
                pass

    def _start_magnetic_travel_tester(self, ui, dialog_token):
        """Start one reversible, live millimetre-reading SK75 test session.

        Unlike a Windows pressed/not-pressed hook, the keyboard's test stream
        reports the actual magnetic position.  Its vendor input endpoint is
        held only while this dialog's session is running.  The worker owns the
        matching ``Stop report`` command in ``finally``, so closing the dialog,
        hiding the application and errors all restore normal keyboard input.
        """
        self._stop_magnetic_travel_tester(reset_ui=False)
        stop_event = threading.Event()
        samples = queue.Queue(maxsize=1)
        token = getattr(self, "_magnetic_travel_tester_token", 0) + 1
        self._magnetic_travel_tester_token = token
        tester = SimpleNamespace(
            token=token,
            dialog_token=dialog_token,
            stop_event=stop_event,
            samples=samples,
            ui=ui,
            active_slot=None,
        )
        self._magnetic_travel_tester = tester
        try:
            ui.start_button.disabled = True
            ui.stop_button.disabled = False
            ui.state_text.value = "Запускаю тест хода…"
            ui.state_text.color = ft.Colors.ON_SURFACE_VARIANT
            ui.start_button.update()
            ui.stop_button.update()
            ui.state_text.update()
        except Exception:
            pass

        def publish(sample):
            """Keep only the most recent HID sample; UI never queues a backlog."""
            try:
                while True:
                    samples.get_nowait()
            except queue.Empty:
                pass
            try:
                samples.put_nowait(sample)
            except queue.Full:
                pass

        def read_live_travel():
            input_device = None
            report_enabled = False
            session_lock = self._magnetic_travel_session_lock
            session_lock.acquire()
            try:
                # Version only determines the firmware's raw units/mm and is
                # a read-only feature request.  0.01 mm is the safe SK75
                # fallback if an older firmware does not answer it.
                step = 100
                try:
                    version = MagneticProtocol.decode_usb_version(
                        self._query_magnetic_packet(
                            MagneticProtocol.get_usb_version_packet(),
                            "magnetic_travel_usb_version",
                        )
                    )
                    step = MagneticProtocol.magnetic_travel_step(version)
                except Exception as exc:
                    logger.debug("magnetic travel version read failed: %s", exc)

                # Keeping the USB lock throughout the short exclusive test
                # prevents profile/lighting writes from interleaving between
                # Start and Stop.  A new tester waits for an old one to send
                # Stop before it can enable another stream.
                with self.usb_lock:
                    if stop_event.is_set():
                        return
                    self._send_lighting_packets_locked(
                        [MagneticProtocol.magnetism_report_packet(True)],
                        "magnetic_travel_start",
                    )
                    report_enabled = True
                    last_error = None
                    for path in self._magnetic_travel_input_paths():
                        try:
                            input_device = hid.device()
                            input_device.open_path(path)
                            input_device.set_nonblocking(1)
                            break
                        except Exception as exc:
                            last_error = exc
                            try:
                                if input_device is not None:
                                    input_device.close()
                            except Exception:
                                pass
                            input_device = None
                    if input_device is None:
                        raise MagneticProtocolError(
                            f"не найден поток проверки хода: {last_error or 'HID input endpoint'}"
                        )

                    next_sample_at = time.monotonic()
                    last_published_visible_mm = None
                    while (
                        not stop_event.is_set()
                        and getattr(self, "_magnetic_travel_tester_token", None) == token
                        and getattr(self, "_magnetic_travel_tester", None) is tester
                    ):
                        # Do not busy-drain a high-polling HID endpoint.  One
                        # bounded drain per 144 Hz display interval discards
                        # the native backlog and publishes the most recent
                        # report without competing with Flet for a CPU core.
                        wait_seconds = next_sample_at - time.monotonic()
                        if wait_seconds > 0:
                            stop_event.wait(wait_seconds)
                            continue
                        try:
                            # HID queues reports independently of the Flet
                            # event loop.  Drain a bounded batch to discard
                            # stale depths instead of reading one old packet,
                            # sleeping, and visibly replaying the key motion.
                            travel_mm, _reports_read = self._drain_magnetic_travel_samples(
                                lambda: input_device.read(64),
                                step=step,
                                max_reports=TRAVEL_TESTER_HID_DRAIN_LIMIT,
                            )
                        except Exception as exc:
                            raise MagneticProtocolError(f"поток проверки хода прерван: {exc}") from exc
                        # Schedule from the completed drain, not an old clock
                        # deadline.  A slow native call can therefore never
                        # cause an immediate catch-up burst of extra drains.
                        next_sample_at = (
                            time.monotonic() + TRAVEL_TESTER_HID_SAMPLE_INTERVAL_SEC
                        )
                        if travel_mm is not None:
                            bounded_mm = max(
                                0.0,
                                min(
                                    float(travel_mm),
                                    MagneticProtocol.OFFICIAL_SK75_ACTUATION_MAX_MM,
                                ),
                            )
                            visible_mm = round(bounded_mm, 2)
                            # Stationary 1 kHz firmware reports must not wake
                            # Flutter 144 times per second.  Direction and the
                            # label both operate at 0.01 mm precision, so this
                            # removes only visually identical samples.
                            if visible_mm != last_published_visible_mm:
                                last_published_visible_mm = visible_mm
                                publish((bounded_mm, None))
            except Exception as exc:
                logger.debug("magnetic travel tester failed: %s", exc)
                if not stop_event.is_set():
                    publish((None, str(exc)))
                    # Leave the event clear long enough for the UI task to
                    # present the error and restore Start/Stop controls.  The
                    # reader is already ending; paint_loop owns the final
                    # stop token after it consumes this message.
            finally:
                try:
                    try:
                        if input_device is not None:
                            input_device.close()
                    except Exception:
                        pass
                    if report_enabled:
                        try:
                            # The session lock remains held until this write
                            # succeeds or fails, so a later tester cannot be
                            # enabled and then accidentally disabled by this
                            # previous reader's cleanup.
                            with self.usb_lock:
                                self._send_lighting_packets_locked(
                                    [MagneticProtocol.magnetism_report_packet(False)],
                                    "magnetic_travel_stop",
                                )
                        except Exception as exc:
                            logger.debug("magnetic travel stop failed: %s", exc)
                finally:
                    session_lock.release()

        async def paint_loop():
            shown = 0.0
            direction_anchor = 0.0
            direction = None
            error_text = None
            last_key_poll_at = 0.0
            active_render_until = 0.0
            while (
                not stop_event.is_set()
                and getattr(self, "_magnetic_travel_tester_token", None) == token
                and getattr(self, "_magnetic_travel_tester", None) is tester
            ):
                frame_started_at = time.monotonic()
                received_sample = False
                try:
                    while True:
                        sample, error_text = samples.get_nowait()
                        if sample is not None:
                            received_sample = True
                            # Show the hardware value directly.  Only colour
                            # direction gets a tiny physical hysteresis so
                            # sensor noise cannot thrash expensive recolours.
                            sample_direction, direction_anchor = (
                                self._magnetic_travel_stable_direction(
                                    direction_anchor,
                                    sample,
                                )
                            )
                            if sample_direction is not None:
                                direction = sample_direction
                            shown = sample
                except queue.Empty:
                    pass
                if received_sample:
                    active_render_until = (
                        frame_started_at + TRAVEL_TESTER_ACTIVE_RENDER_HOLD_SEC
                    )
                    # The HID stream tells us the current depth but does not
                    # identify its key slot.  Poll Windows at a low, bounded
                    # cadence rather than scanning the whole key matrix for
                    # every high-rate sensor sample.
                    now = time.monotonic()
                    if now - last_key_poll_at >= TRAVEL_TESTER_KEY_POLL_INTERVAL_SEC:
                        pressed_slot = self._travel_tester_pressed_slot(tester.active_slot)
                        if pressed_slot is not None and pressed_slot != tester.active_slot:
                            tester.active_slot = pressed_slot
                        last_key_poll_at = now
                    painted = self._paint_magnetic_travel_tester(
                        ui,
                        shown,
                        direction,
                        active_slot=tester.active_slot,
                    )
                    if not painted and getattr(tester, "paint_error", None):
                        error_text = tester.paint_error
                if error_text:
                    # The painter already logged the originating Flet failure
                    # once.  This branch owns the visible, graceful shutdown:
                    # end the exclusive HID mode and restore the dialog's
                    # buttons without attempting to rebuild the page.
                    try:
                        ui.state_text.value = error_text
                        ui.state_text.color = ft.Colors.ERROR
                        ui.start_button.disabled = False
                        ui.stop_button.disabled = True
                        ui.state_text.update()
                        ui.start_button.update()
                        ui.stop_button.update()
                    except Exception as surface_exc:
                        logger.debug(
                            "could not surface magnetic travel tester error: %s",
                            surface_exc,
                        )
                    if not getattr(tester, "error_notified", False):
                        tester.error_notified = True
                        try:
                            self._snack(error_text)
                        except Exception as snack_exc:
                            logger.debug(
                                "could not show magnetic travel tester error: %s",
                                snack_exc,
                            )
                    stop_event.set()
                    if getattr(self, "_magnetic_travel_tester", None) is tester:
                        self._magnetic_travel_tester = None
                        self._magnetic_travel_tester_token = token + 1
                    break
                # Motion can be presented at up to 144 FPS, matching a fast
                # monitor.  An unchanged key performs no Flet update and the
                # lightweight queue poll backs off to 60 Hz.  Subtract work
                # already done this iteration so update time cannot create a
                # catch-up burst or a growing render backlog.
                now = time.monotonic()
                interval = (
                    TRAVEL_TESTER_RENDER_INTERVAL_SEC
                    if now < active_render_until
                    else TRAVEL_TESTER_IDLE_RENDER_INTERVAL_SEC
                )
                await asyncio.sleep(max(0.0, interval - (now - frame_started_at)))

        reader = threading.Thread(
            target=read_live_travel,
            daemon=True,
            name="sk75-live-travel-reader",
        )
        tester.reader_thread = reader
        reader.start()
        try:
            self.page.run_task(paint_loop)
        except Exception:
            stop_event.set()
            if getattr(self, "_magnetic_travel_tester", None) is tester:
                self._magnetic_travel_tester = None

    def _open_magnetic_travel_tester(self):
        """Open a self-contained live travel check for any magnetic key."""
        self._stop_magnetic_travel_tester(reset_ui=False)
        dialog_token = getattr(self, "_magnetic_travel_dialog_token", 0) + 1
        self._magnetic_travel_dialog_token = dialog_token
        full_travel_mm, travel_ticks = self._magnetic_travel_tester_scale()

        # Build the tester in two fixed layers.  ``tester_static_base`` never
        # changes after opening the dialog (shell, rail and ruler labels).
        # ``dynamic_overlay`` contains only the moving fill/cursor/switch and
        # its text.  Flet can therefore patch a tiny stable overlay instead
        # of repeatedly serialising the illustrated switch and ruler.
        #
        # The live meter itself still has a separate clipped rail subtree. Its
        # fill and glow are local to that 52×234 px rail, while ruler labels
        # remain static and fully visible.
        meter_frame_left = 5
        meter_frame_top = 5
        meter_rail_width = 52
        meter_rail_height = 234
        meter_fill_top = 7
        meter_top = meter_frame_top + meter_fill_top
        meter_inner_height = 220
        ticks = [
            ft.Container(
                content=ft.Row(
                    [
                        ft.Container(
                            width=18 if index % 2 == 0 else 10,
                            height=1,
                            bgcolor=ft.Colors.OUTLINE_VARIANT,
                        ),
                        ft.Text(
                            # The regular half-millimetre ticks stay compact;
                            # the current 3.30 mm endpoint is shown exactly.
                            f"{travel:.2f} мм"
                            if travel == full_travel_mm
                            else f"{travel:.1f} мм",
                            size=9,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                    spacing=6,
                ),
                left=66,
                top=round(meter_top + meter_inner_height * travel / full_travel_mm - 7),
            )
            for index, travel in enumerate(travel_ticks)
        ]
        meter_track = ft.Container(
            left=meter_frame_left,
            top=meter_frame_top,
            width=meter_rail_width,
            height=meter_rail_height,
            # Neutral rail at rest.  TERTIARY_CONTAINER follows the colour
            # seed and became maroon in the packaged dark theme.
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=8,
        )
        meter_static_base = ft.Stack(
            [meter_track, *ticks],
            width=160,
            height=244,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

        meter_fill_glow = ft.Container(
            left=3,
            top=meter_fill_top,
            width=46,
            height=0,
            # A flat translucent halo is substantially cheaper than a live
            # gradient/shadow in the packaged Flutter renderer.  It remains
            # inside the clipped rail and is recoloured only when direction
            # genuinely changes.
            bgcolor=ft.Colors.with_opacity(0.18, ft.Colors.PRIMARY),
            border_radius=0,
        )
        meter_fill = ft.Container(
            left=6,
            top=meter_fill_top,
            width=40,
            height=0,
            bgcolor=ft.Colors.PRIMARY,
            border_radius=5,
        )
        meter_cursor_glow = ft.Container(
            left=3,
            top=meter_fill_top - 1,
            width=46,
            height=10,
            bgcolor=ft.Colors.with_opacity(0.42, ft.Colors.PRIMARY),
            border_radius=0,
            opacity=0,
        )
        meter_cursor = ft.Container(
            left=3,
            top=meter_fill_top - 1,
            width=46,
            height=2,
            bgcolor=ft.Colors.PRIMARY,
            border_radius=3,
            opacity=0,
        )
        meter_rail_layers = ft.Stack(
            [
                meter_fill_glow,
                meter_fill,
                meter_cursor_glow,
                meter_cursor,
            ],
            width=meter_rail_width,
            height=meter_rail_height,
            # The glow must end at the real physical top/bottom of the rail;
            # never let a press paint a coloured fragment past either cap.
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        meter_dynamic_rail = ft.Container(
            content=meter_rail_layers,
            left=220 + meter_frame_left,
            top=meter_frame_top,
            width=meter_rail_width,
            height=meter_rail_height,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

        switch_fill = ft.Container(
            left=19,
            top=19,
            width=72,
            height=0,
            bgcolor=ft.Colors.PRIMARY_CONTAINER,
            border_radius=6,
        )
        switch_stem = ft.Container(
            left=14,
            top=10,
            width=82,
            height=28,
            bgcolor=ft.Colors.SECONDARY_CONTAINER,
            border=ft.Border.all(2, ft.Colors.PRIMARY),
            border_radius=8,
        )
        switch_chamber = ft.Container(
            left=40,
            top=27,
            width=110,
            height=118,
            # The static base paints the shell.  This transparent overlay
            # carries only the direction-sensitive outline.
            bgcolor=None,
            border=ft.Border.all(2, ft.Colors.DEEP_PURPLE_300),
            border_radius=14,
        )
        switch_motion_clip = ft.Container(
            left=40,
            top=27,
            width=110,
            height=118,
            content=ft.Stack(
                [switch_fill, switch_stem],
                width=110,
                height=118,
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
            ),
            # Both the stem and its coloured travel fill are physically
            # confined to the illustrated switch chamber.
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        drawn_switch_static = ft.Stack(
            [
                ft.Container(
                    width=172,
                    height=172,
                    bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
                    border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                    border_radius=20,
                ),
            ],
            width=172,
            height=172,
        )
        value_text = ft.Text("0.00 мм", size=22, weight=ft.FontWeight.W_600)
        key_text = ft.Text(
            "Нажмите любую магнитную клавишу",
            width=190,
            size=11,
            color=ft.Colors.ON_SURFACE_VARIANT,
            text_align=ft.TextAlign.CENTER,
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        state_text = ft.Text(
            "Ожидание",
            width=190,
            size=11,
            color=ft.Colors.ON_SURFACE_VARIANT,
            text_align=ft.TextAlign.CENTER,
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        # The shell/tick base and the dynamic controls share fixed geometry.
        # Replacing the invitation with a detected key name therefore never
        # moves the ruler, and live paint only updates ``dynamic_overlay``.
        tester_visual_width = 380
        tester_key_column = ft.Container(
            # The illustrated switch and its three live captions must share
            # the same 190 px centre line.  The old 9 px offset centred the
            # static card differently from the moving chamber/text below it.
            left=0,
            top=0,
            width=190,
            alignment=ft.Alignment.TOP_CENTER,
            content=drawn_switch_static,
        )
        tester_static_base = ft.Stack(
            [
                tester_key_column,
                ft.Container(left=220, top=0, content=meter_static_base),
            ],
            width=tester_visual_width,
            height=244,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        dynamic_overlay = ft.Stack(
            [
                switch_chamber,
                switch_motion_clip,
                meter_dynamic_rail,
                ft.Container(
                    left=0,
                    top=177,
                    width=190,
                    alignment=ft.Alignment.TOP_CENTER,
                    content=value_text,
                ),
                ft.Container(
                    left=0,
                    top=205,
                    width=190,
                    alignment=ft.Alignment.TOP_CENTER,
                    content=key_text,
                ),
                ft.Container(
                    left=0,
                    top=223,
                    width=190,
                    alignment=ft.Alignment.TOP_CENTER,
                    content=state_text,
                ),
            ],
            width=tester_visual_width,
            height=244,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        visual_region = ft.Container(
            width=470,
            alignment=ft.Alignment.CENTER,
            content=ft.Stack(
                [
                    tester_static_base,
                    dynamic_overlay,
                ],
                width=tester_visual_width,
                height=244,
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
            ),
        )
        start_button = ft.FilledButton("Старт", icon=ft.Icons.PLAY_ARROW_ROUNDED)
        stop_button = ft.OutlinedButton(
            "Стоп", icon=ft.Icons.STOP_ROUNDED, disabled=True
        )
        ui = SimpleNamespace(
            full_travel_mm=full_travel_mm,
            # Local rail coordinate used by the sample-driven painter.  Tick labels
            # use the outer ``meter_top`` above and do not move.
            meter_top=meter_fill_top,
            meter_inner_height=meter_inner_height,
            meter_rail_height=meter_rail_height,
            meter_fill_glow=meter_fill_glow,
            meter_fill=meter_fill,
            meter_cursor_glow=meter_cursor_glow,
            meter_cursor=meter_cursor,
            meter_track=meter_track,
            switch_fill=switch_fill,
            switch_stem=switch_stem,
            switch_chamber=switch_chamber,
            value_text=value_text,
            key_text=key_text,
            state_text=state_text,
            visual_region=visual_region,
            dynamic_overlay=dynamic_overlay,
            start_button=start_button,
            stop_button=stop_button,
        )

        def start(_event):
            self._start_magnetic_travel_tester(ui, dialog_token)

        def stop(_event=None):
            self._stop_magnetic_travel_tester(
                reset_ui=True, expected_dialog_token=dialog_token
            )

        def close(_event=None):
            self._stop_magnetic_travel_tester(
                reset_ui=False, expected_dialog_token=dialog_token
            )
            self.page.pop_dialog()

        start_button.on_click = start
        stop_button.on_click = stop
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Проверка хода"),
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            "Нажмите любую магнитную клавишу. Зелёный — движение вниз, "
                            "голубой — вверх.",
                            size=12,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        visual_region,
                        ft.Text(
                            "Старт показывает живую глубину той клавиши, которую вы нажмёте. "
                            "На время теста обычный ввод может отключиться; Стоп или закрытие "
                            "этого окна всегда возвращают его.",
                            size=10,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        ft.Row([start_button, stop_button], spacing=10),
                    ],
                    spacing=10,
                    tight=True,
                ),
                width=470,
            ),
            actions=[ft.TextButton("Закрыть", on_click=close)],
            on_dismiss=lambda _event: self._stop_magnetic_travel_tester(
                reset_ui=False, expected_dialog_token=dialog_token
            ),
            shape=ft.RoundedRectangleBorder(radius=22),
        )
        self.page.show_dialog(dialog)

    @staticmethod
    def _womier_driver_close_message(result):
        """Return one compact, honest status line for the close button."""
        if not result.found:
            return "Официальный Womier Driver и iot_driver не запущены."
        parts = []
        if result.closed:
            parts.append(
                "Закрыто: " + ", ".join(match.display_name for match in result.closed)
            )
        if result.remaining:
            parts.append(
                "остались: "
                + ", ".join(match.display_name for match in result.remaining)
            )
        if result.skipped:
            parts.append(
                "пропущены (путь/процесс изменился): "
                + ", ".join(match.display_name for match in result.skipped)
            )
        if result.errors:
            parts.append("ошибка закрытия: " + "; ".join(result.errors[:2]))
        return ". ".join(parts) or "Процессы Womier уже закрыты."

    def _set_womier_driver_open_busy(self, busy):
        """Keep the exact-driver launch tile responsive during one launch."""
        button = getattr(self, "womier_driver_open_nav_button", None)
        label = getattr(self, "womier_driver_open_label", None)
        if button is not None:
            try:
                button.disabled = bool(busy)
                button.update()
            except Exception:
                pass
        if label is not None:
            try:
                label.color = (
                    ft.Colors.ON_SURFACE_VARIANT if busy else ft.Colors.PRIMARY
                )
                label.update()
            except Exception:
                pass

    def _open_official_womier_driver(self):
        """Launch the canonical Womier executable without touching HID state."""
        open_lock = getattr(self, "_womier_driver_open_lock", None)
        if open_lock is None:
            open_lock = threading.Lock()
            self._womier_driver_open_lock = open_lock
        if not open_lock.acquire(blocking=False):
            self._snack("Открытие Womier уже выполняется.")
            return
        self._set_womier_driver_open_busy(True)

        def worker():
            try:
                opened, message = _launch_exact_womier_driver()
                if opened:
                    message += " Для изменений в Magnetic Lab сначала закройте драйвер."
                logger.info("stock Womier open action: %s", message)
            except Exception as exc:
                logger.exception("stock Womier open action failed")
                message = f"Не удалось открыть Womier: {exc}"
            finally:
                try:
                    open_lock.release()
                except RuntimeError:
                    pass

            def finish():
                self._set_womier_driver_open_busy(False)
                self._snack(message)

            self._ui_call(finish)

        threading.Thread(
            target=worker,
            daemon=True,
            name="womier-driver-open",
        ).start()

    def _set_womier_driver_close_busy(self, busy):
        """Keep the narrow close action single-flight without touching HID."""
        button = getattr(self, "womier_driver_close_nav_button", None)
        label = getattr(self, "womier_driver_close_label", None)
        if button is not None:
            try:
                button.disabled = bool(busy)
                button.update()
            except Exception:
                pass
        if label is not None:
            try:
                label.color = (
                    ft.Colors.ON_SURFACE_VARIANT if busy else ft.Colors.ERROR
                )
                label.update()
            except Exception:
                pass

    def _close_womier_driver_processes_after_confirmation(self):
        """Run the exact-path close work off Flet's event/UI thread."""
        close_lock = getattr(self, "_womier_driver_close_lock", None)
        if close_lock is None:
            close_lock = threading.Lock()
            self._womier_driver_close_lock = close_lock
        if not close_lock.acquire(blocking=False):
            self._snack("Закрытие Womier уже выполняется.")
            return
        self._set_womier_driver_close_busy(True)

        def worker():
            try:
                result = _close_exact_womier_driver_processes()
                message = self._womier_driver_close_message(result)
                logger.info("stock Womier close action: %s", message)
            except Exception as exc:
                logger.exception("stock Womier close action failed")
                message = f"Не удалось закрыть Womier: {exc}"
            finally:
                try:
                    close_lock.release()
                except RuntimeError:
                    pass

            def finish():
                self._set_womier_driver_close_busy(False)
                self._snack(message)

            self._ui_call(finish)

        threading.Thread(
            target=worker,
            daemon=True,
            name="womier-driver-close",
        ).start()

    def _confirm_close_womier_driver_processes(self):
        """Ask before closing only the two known stock Womier executables."""
        matches = _find_exact_womier_driver_processes()
        if not matches:
            self._snack("Официальный Womier Driver и iot_driver не запущены.")
            return

        found_list = ft.Column(
            [
                ft.Row(
                    [
                        ft.Icon(ft.Icons.DESKTOP_WINDOWS_ROUNDED, size=16),
                        ft.Text(match.display_name, size=12, weight=ft.FontWeight.W_600),
                    ],
                    spacing=7,
                )
                for match in matches
            ],
            spacing=5,
            tight=True,
        )
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Закрыть официальный Womier Driver?"),
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            "Это освободит доступ к клавиатуре для Magnetic Lab.",
                            size=12,
                        ),
                        found_list,
                        ft.Text(
                            "Будут закрыты только процессы с точным именем и путём "
                            "установки WOMIER Driver / iot_driver. Другие программы "
                            "с похожим именем не затрагиваются.",
                            size=10,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                    spacing=10,
                    tight=True,
                ),
                width=480,
            ),
            shape=ft.RoundedRectangleBorder(radius=20),
        )

        def cancel(_event=None):
            self.page.pop_dialog()

        def confirm(_event=None):
            self.page.pop_dialog()
            # Re-scan/revalidate in the worker; the visible list is only an
            # explanation for this confirmation, never a PID kill list.
            self._close_womier_driver_processes_after_confirmation()

        dialog.actions = [
            ft.TextButton("Отмена", on_click=cancel),
            ft.FilledButton(
                "Закрыть процессы",
                icon=ft.Icons.POWER_SETTINGS_NEW_ROUNDED,
                on_click=confirm,
            ),
        ]
        self.page.show_dialog(dialog)

    def _build_sk75_calibration_layout(self, *, deck_width=850):
        """Build the compact, locally paintable SK75 calibration board.

        Calibration deliberately owns a second visual board instead of
        reusing the main Magnetic Lab keycaps.  A live refresh therefore
        changes only this dialog's fill layers and can never rebuild the
        regular settings deck while the user is pressing every switch.
        """
        # The fixed 1360×820 desktop window leaves less usable width inside an
        # AlertDialog than the full Magnetic Lab.  Keep the *inner* physical
        # case at 850 px and add padding outside of it; previously the 930 px
        # case was put inside a padded 970 px dialog and its Home/End/arrow
        # column could be clipped on the right.
        geometry = _sk75_visual_deck_geometry(compact=True, deck_width=deck_width)
        key_height = 32
        fill_height = key_height - 2
        keycaps = {}
        rows = []
        for row_index, layout_row in enumerate(SK75_OFFICIAL_VISUAL_LAYOUT):
            key_controls = []
            for key_index, (slot, label, _width) in enumerate(layout_row):
                key_width = geometry.key_widths[row_index][key_index]
                display_label = {
                    "Back": "Backspace",
                    "R Shift": "Shift",
                }.get(label, label)
                # The fill is clipped inside the cap and grows upward exactly
                # like Womier's calibration SVG.  Do not add a pre-start
                # reference line here: it looked like a stale purple press and
                # made a fresh calibration visually ambiguous.
                fill = ft.Container(
                    left=1,
                    bottom=1,
                    width=max(1, key_width - 2),
                    height=0,
                    bgcolor="#20C77A",
                    opacity=0.72,
                )
                label_text = ft.Text(
                    display_label,
                    size=8,
                    weight=ft.FontWeight.W_700,
                    color=ft.Colors.ON_SURFACE,
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    text_align=ft.TextAlign.CENTER,
                )
                keycap = ft.Container(
                    content=ft.Stack(
                        [
                            fill,
                            ft.Container(
                                content=label_text,
                                width=key_width,
                                height=key_height,
                                alignment=ft.Alignment.CENTER,
                            ),
                        ],
                        width=key_width,
                        height=key_height,
                        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    ),
                    width=key_width,
                    height=key_height,
                    bgcolor=SK75_VISUAL_KEY_BACKGROUND,
                    border=ft.Border.all(1, SK75_VISUAL_KEY_BORDER),
                    border_radius=7,
                    padding=0,
                    tooltip=f"{display_label}: уровень калибровки",
                )
                keycaps[slot] = SimpleNamespace(
                    fill=fill,
                    keycap=keycap,
                    label_text=label_text,
                )
                keycap.left = geometry.key_x_positions[row_index][key_index]
                keycap.top = 0
                key_controls.append(keycap)
            row = ft.Stack(
                key_controls,
                width=geometry.row_width,
                height=key_height,
                clip_behavior=ft.ClipBehavior.NONE,
            )
            # Keeping the row reference lets the live painter patch only the
            # one changed physical row instead of serialising the 81-key deck
            # every time the firmware produces a calibration sample.
            for slot, _label, _width in layout_row:
                keycaps[slot].row = row
            rows.append(row)
        board_padding = 8
        board = ft.Container(
            content=ft.Column(
                rows,
                spacing=4,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            # ``Container.width`` includes padding in Flet.  Add it to the
            # case width so the positioned final key remains inside the
            # clipped board, rather than quietly losing the right nav column.
            width=geometry.deck_width + board_padding * 2,
            padding=board_padding,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=16,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        )
        return board, keycaps, fill_height

    def _paint_magnetic_calibration(
        self,
        ui,
        levels,
        *,
        baseline_levels=None,
        firmware_version=None,
        completed_slots=None,
        allow_raw_completion=True,
        completed_only=False,
    ):
        """Patch changed calibration caps, grouped by their physical row.

        Every official ``0xFE`` poll carries 81 raw values.  The old painter
        mutated all caps and updated the enclosing dialog for every poll, which
        made calibration visibly stutter while a switch was held.  This is a
        presentation-only diff: the same read-only snapshots are retained, but
        Flet receives only the row whose pixels changed.
        """
        try:
            levels = levels or {}
            # Kept as a compatibility argument for the calibration worker,
            # but deliberately not rendered.  Older builds drew a purple
            # baseline for every pre-start 0xFE value; those marks were easily
            # mistaken for accidental presses.
            del baseline_levels
            # Preserve the partial-calibration state owned by the dialog.  A
            # completed switch remains complete after Stop because Womier's
            # 0x1E/0 leaves it calibrated; Stop is not an undo command.
            completed_slots = set(completed_slots or ()) | set(
                getattr(ui, "completed_slots", set()) or ()
            )
            previous_levels = getattr(ui, "rendered_levels", {}) or {}
            changed_rows = {}

            def raw_for(source, slot):
                try:
                    return max(0, int(source.get(slot, 0)))
                except (AttributeError, TypeError, ValueError):
                    return 0

            for slot, reference in ui.keycaps.items():
                raw_level = raw_for(levels, slot)
                try:
                    current_fraction = MagneticProtocol.calibration_progress_fraction(
                        raw_level, firmware_version
                    )
                except MagneticProtocolError:
                    current_fraction = 0.0
                # During an active calibration run the firmware can report a
                # full cap for a key that was already held when the mode came
                # up.  Do not paint that as a completed key until the worker
                # has seen a fresh release -> press edge.  The read-only
                # dialog-open snapshot deliberately keeps the old behaviour:
                # previously completed firmware keys are immediately shown.
                is_complete = slot in completed_slots or (
                    allow_raw_completion and current_fraction >= 1.0
                )
                if allow_raw_completion and current_fraction >= 1.0:
                    completed_slots.add(slot)

                # While a switch is being pressed, the separate live rail is
                # the responsive visual.  Repainting raw cap fills across the
                # 81-key board on every matrix poll starves that rail on some
                # desktops.  During a live session therefore patch a board row
                # only when a key's confirmed completion state changes.
                if completed_only:
                    if getattr(reference, "rendered_complete", None) == is_complete:
                        continue
                elif (
                    raw_level == raw_for(previous_levels, slot)
                ):
                    continue

                # Quantise to the actual rendered pixels.  A raw sensor change
                # that cannot move a 30 px cap fill should not enqueue a Flet
                # patch at all.
                displayed_fraction = max(current_fraction, 1.0 if is_complete else 0.0)
                fill_height = round(ui.fill_height * displayed_fraction)
                fill_color = "#42D989" if is_complete else "#20C77A"
                fill_opacity = 0.84 if is_complete else 0.62
                visual_changed = False
                if getattr(reference, "rendered_fill_height", None) != fill_height:
                    reference.fill.height = fill_height
                    reference.rendered_fill_height = fill_height
                    visual_changed = True
                if (
                    getattr(reference, "rendered_fill_color", None) != fill_color
                    or getattr(reference, "rendered_fill_opacity", None) != fill_opacity
                ):
                    reference.fill.bgcolor = fill_color
                    reference.fill.opacity = fill_opacity
                    reference.rendered_fill_color = fill_color
                    reference.rendered_fill_opacity = fill_opacity
                    visual_changed = True
                if getattr(reference, "rendered_complete", None) != is_complete:
                    reference.keycap.border = ft.Border.all(
                        1, "#5BE39A" if is_complete else SK75_VISUAL_KEY_BORDER
                    )
                    reference.label_text.color = (
                        ft.Colors.WHITE if is_complete else ft.Colors.ON_SURFACE
                    )
                    reference.rendered_complete = is_complete
                    visual_changed = True
                if visual_changed:
                    row = getattr(reference, "row", None)
                    if row is not None:
                        changed_rows[id(row)] = row

            ui.rendered_levels = dict(levels)
            # Do not replace the dialog's set object here.  The HID worker
            # holds the same object while it records a just-finished key, and
            # replacing it would make a quick Stop → Start forget the last
            # green key even though the firmware never received an undo.
            stored_completed_slots = getattr(ui, "completed_slots", None)
            if isinstance(stored_completed_slots, set):
                stored_completed_slots.update(completed_slots)
                completed_slots = stored_completed_slots
            else:
                ui.completed_slots = completed_slots
            complete_count = len(completed_slots)
            progress_changed = getattr(ui, "rendered_complete_count", None) != complete_count
            if progress_changed:
                ui.progress_text.value = f"Готово: {complete_count}/{len(ui.keycaps)}"
                ui.progress_text.color = (
                    "#5BE39A" if complete_count else ft.Colors.ON_SURFACE_VARIANT
                )
                ui.rendered_complete_count = complete_count

            # A single press normally changes one row.  Do not call
            # ``visual_region.update()`` here: it causes Flet to walk the
            # complete dialog/deck for every four-chunk HID snapshot.
            for row in changed_rows.values():
                try:
                    row.update()
                except Exception:
                    pass
            if progress_changed:
                try:
                    ui.progress_text.update()
                except Exception:
                    pass
        except Exception:
            # Closing a Flet dialog races an occasional queued paint.  The
            # session token below makes that harmless.
            pass

    @staticmethod
    def _calibration_completed_slots(
        levels, firmware_version=None, visible_slots=None
    ):
        """Return only keys that reached Womier's official completion mark.

        A completed key is discovered from the same read-only ``0xFE`` words
        that the stock driver paints.  No local guess based on a key's
        millimetres is made here, and no command is sent by this helper.
        """
        levels = levels or {}
        allowed = set(visible_slots) if visible_slots is not None else None
        threshold = MagneticProtocol.calibration_completion_raw(firmware_version)
        completed = set()
        for raw_slot, raw_level in levels.items():
            try:
                slot = int(raw_slot)
                level = int(raw_level)
            except (TypeError, ValueError):
                continue
            if allowed is not None and slot not in allowed:
                continue
            if level >= threshold:
                completed.add(slot)
        return completed

    @staticmethod
    def _calibration_released_slots(levels, firmware_version=None, visible_slots=None):
        """Return keys observed below Womier's completion threshold.

        A calibration session is armed only after a key has been seen released.
        This makes a held key at dialog start ineligible until it is released
        and pressed again.  It is deliberately based on the same read-only
        ``0xFE`` matrix as the official visual feedback; it sends no extra HID
        packet and cannot alter a key's calibration by itself.
        """
        levels = levels or {}
        allowed = set(visible_slots) if visible_slots is not None else None
        threshold = MagneticProtocol.calibration_completion_raw(firmware_version)
        released = set()
        for raw_slot, raw_level in levels.items():
            try:
                slot = int(raw_slot)
                level = max(0, int(raw_level))
            except (TypeError, ValueError):
                continue
            if allowed is not None and slot not in allowed:
                continue
            if level < threshold:
                released.add(slot)
        return released

    @staticmethod
    def _calibration_new_completion_edges(
        previous_levels,
        levels,
        armed_slots,
        completed_slots=None,
        firmware_version=None,
        visible_slots=None,
    ):
        """Return newly completed keys only after a fresh release -> press.

        The stock protocol has no rollback command.  The UI must therefore
        not call an arbitrary initial full ``0xFE`` reading a completed key:
        that would make a stray held switch look intentional.  ``armed_slots``
        is updated in place where possible, so a temporary high level becomes
        eligible only after that key later returns below Womier's completion
        threshold.
        """
        previous_levels = previous_levels or {}
        levels = levels or {}
        completed_slots = set(completed_slots or ())
        allowed = set(visible_slots) if visible_slots is not None else None
        threshold = MagneticProtocol.calibration_completion_raw(firmware_version)
        if not isinstance(armed_slots, set):
            armed_slots = set(armed_slots or ())

        # Seeing the key below the completion threshold proves it is no longer
        # one of the keys held at the start of the session.
        armed_slots.update(
            QMKManager._calibration_released_slots(
                levels, firmware_version, allowed
            )
        )
        newly_completed = set()
        for raw_slot, raw_level in levels.items():
            try:
                slot = int(raw_slot)
                current = max(0, int(raw_level))
                previous = max(0, int(previous_levels.get(slot, 0)))
            except (TypeError, ValueError):
                continue
            if allowed is not None and slot not in allowed:
                continue
            if (
                slot in armed_slots
                and slot not in completed_slots
                and previous < threshold
                and current >= threshold
            ):
                newly_completed.add(slot)
        return armed_slots, newly_completed

    @staticmethod
    def _calibration_stable_released_slots(
        levels,
        release_streaks=None,
        firmware_version=None,
        visible_slots=None,
        required_samples=CALIBRATION_RELEASE_STABLE_SAMPLES,
    ):
        """Return keys that have been *stably* released after calibration starts.

        A raw 0xFE value is a calibration-progress value, not a normal key-up
        event.  In particular, the first few values after ``0x1E/1`` can be
        stale while the firmware changes reference points.  Requiring several
        consecutive near-zero samples makes a key eligible only after the
        board has settled with that switch released.

        ``release_streaks`` is intentionally supplied by the worker so this
        helper stays a pure state transform and sends no HID packets.
        """
        levels = levels or {}
        allowed = set(visible_slots) if visible_slots is not None else None
        if not isinstance(release_streaks, dict):
            release_streaks = {}
        try:
            required_samples = max(1, int(required_samples))
        except (TypeError, ValueError):
            required_samples = CALIBRATION_RELEASE_STABLE_SAMPLES

        # Current SK75 firmware reports a 0..300 progress scale.  Treat only
        # the tiny bottom noise band as released; ``< 300`` was far too broad
        # and armed half-pressed/transient keys during startup.  Legacy 0/1
        # firmware remains exact-zero only.
        threshold = MagneticProtocol.calibration_completion_raw(firmware_version)
        release_limit = 0 if threshold <= 1 else min(3, max(0, threshold // 100))
        observed = set()
        for raw_slot, raw_level in levels.items():
            try:
                slot = int(raw_slot)
                level = max(0, int(raw_level))
            except (TypeError, ValueError):
                continue
            if allowed is not None and slot not in allowed:
                continue
            observed.add(slot)
            if level <= release_limit:
                release_streaks[slot] = int(release_streaks.get(slot, 0)) + 1
            else:
                release_streaks.pop(slot, None)

        # A missing chunk/sample must never leave an old low reading armed.
        for slot in tuple(release_streaks):
            if slot not in observed:
                release_streaks.pop(slot, None)

        stable_slots = {
            slot
            for slot, count in release_streaks.items()
            if count >= required_samples
        }
        return release_streaks, stable_slots

    @staticmethod
    def _calibration_confirmed_completion_edges(
        previous_levels,
        levels,
        armed_slots,
        completed_slots=None,
        *,
        release_streaks=None,
        full_since=None,
        now=None,
        firmware_version=None,
        visible_slots=None,
        required_release_samples=CALIBRATION_RELEASE_STABLE_SAMPLES,
        required_full_seconds=CALIBRATION_FULL_HOLD_SECONDS,
    ):
        """Confirm a fresh, stable release -> held-full calibration press.

        The stock driver clears its visual completed-key set on entry and
        waits 500 ms after starting.  We keep that protocol intact but make
        the UI gate stricter: a key must first be stably released *after* the
        startup guard, then reach Womier's full value and remain there briefly.
        This prevents startup/stale 0xFE samples from turning arbitrary keys
        green, while a deliberate one-second press (the official instruction)
        is still accepted normally.
        """
        previous_levels = previous_levels or {}
        levels = levels or {}
        completed_slots = set(completed_slots or ())
        allowed = set(visible_slots) if visible_slots is not None else None
        if not isinstance(armed_slots, set):
            armed_slots = set(armed_slots or ())
        if not isinstance(full_since, dict):
            full_since = {}
        if now is None:
            now = time.monotonic()
        try:
            now = float(now)
        except (TypeError, ValueError):
            now = time.monotonic()
        try:
            required_full_seconds = max(0.0, float(required_full_seconds))
        except (TypeError, ValueError):
            required_full_seconds = CALIBRATION_FULL_HOLD_SECONDS

        release_streaks, stable_releases = QMKManager._calibration_stable_released_slots(
            levels,
            release_streaks,
            firmware_version,
            allowed,
            required_release_samples,
        )
        armed_slots.update(stable_releases)
        threshold = MagneticProtocol.calibration_completion_raw(firmware_version)
        newly_completed = set()
        observed = set()
        for raw_slot, raw_level in levels.items():
            try:
                slot = int(raw_slot)
                current = max(0, int(raw_level))
                previous = max(0, int(previous_levels.get(slot, 0)))
            except (TypeError, ValueError):
                continue
            if allowed is not None and slot not in allowed:
                continue
            observed.add(slot)
            if slot in completed_slots or slot not in armed_slots:
                full_since.pop(slot, None)
                continue
            if current < threshold:
                full_since.pop(slot, None)
                continue
            # Do not create a candidate from an already-full first sample.
            # It needs a new below-threshold -> full edge after this key was
            # armed, otherwise the board's startup residue could be accepted.
            if previous < threshold:
                full_since.setdefault(slot, now)
            started_at = full_since.get(slot)
            if (
                started_at is not None
                and now - started_at >= required_full_seconds
            ):
                newly_completed.add(slot)
                full_since.pop(slot, None)

        for slot in tuple(full_since):
            if slot not in observed:
                full_since.pop(slot, None)
        return armed_slots, newly_completed, release_streaks, full_since

    @staticmethod
    def _calibration_live_change(previous_levels, levels, visible_slots=None):
        """Return the visible key whose calibration reading just changed.

        Calibration's four ``0xFE`` snapshots expose *calibration levels*, not
        the millimetre stream used by ``Проверка хода``.  This tiny selector is
        deliberately kept on those read-only snapshots: issuing the normal
        0x1B live-travel request while the firmware is calibrating could make
        the two diagnostic modes fight over the keyboard.  The UI can still
        show a useful last-pressed-key indicator without sending any extra HID
        command.
        """
        previous_levels = previous_levels or {}
        levels = levels or {}
        allowed = set(visible_slots) if visible_slots is not None else None
        candidates = []
        for slot, raw_level in levels.items():
            if allowed is not None and slot not in allowed:
                continue
            try:
                current = max(0, int(raw_level))
                previous = max(0, int(previous_levels.get(slot, 0)))
            except (TypeError, ValueError):
                continue
            change = abs(current - previous)
            if change:
                # A deterministic tie break keeps the indicator from jumping
                # between equal, batched matrix changes on one HID refresh.
                candidates.append((change, current, -int(slot), slot))
        if not candidates:
            return None, 0
        _change, current, _stable_slot, slot = max(candidates)
        return slot, current

    def _paint_magnetic_calibration_telemetry(
        self, ui, slot, fraction, history
    ):
        """Paint the small, read-only calibration indicator as one subtree.

        ``fraction`` is a normalized calibration level.  The visible ruler
        maps it onto SK75's configured 0.00–3.30 mm *calibration range*, not a
        claimed live physical-travel measurement.  Keeping it in this modal's
        local region avoids rebuilding the 81-key deck.
        """
        try:
            try:
                fraction = max(0.0, min(1.0, float(fraction)))
            except (TypeError, ValueError):
                fraction = 0.0
            calibrated_range_mm = float(
                getattr(ui, "calibrated_range_mm", 3.30)
            )
            fill_height = max(0, round(ui.live_meter_inner_height * fraction))
            cursor_top = max(
                ui.live_meter_inner_top,
                min(
                    ui.live_meter_inner_top
                    + ui.live_meter_inner_height
                    - ui.live_meter_cursor.height,
                    round(
                        ui.live_meter_inner_top
                        + ui.live_meter_inner_height * (1.0 - fraction)
                        - ui.live_meter_cursor.height // 2
                    ),
                ),
            )
            key_label = (
                f"Последняя: {self._sk75_key_name(slot)}"
                if slot in SK75_KEY_BY_SLOT
                else "Нажмите любую клавишу"
            )
            # This is intentionally written as a range position, rather than
            # raw ``mm``: calibration's 0xFE report has no live depth field.
            range_position_mm = calibrated_range_mm * fraction
            value_label = (
                f"{range_position_mm:.2f} / {calibrated_range_mm:.2f} мм"
            )

            # The trace is intentionally a finite, newest-at-right history.
            # It remains on screen after a key is released, which makes it
            # easy to see that a press was actually observed by calibration.
            history = list(history or [])[-len(ui.live_trace_bars):]
            padded = [None] * (len(ui.live_trace_bars) - len(history)) + history
            trace_signature = []
            for point, value in zip(ui.live_trace_bars, padded):
                if value is None:
                    point_height = 1
                    point_opacity = 0.14
                else:
                    point_height = max(2, round(ui.live_trace_height * value))
                    point_opacity = 0.95
                trace_signature.append((point_height, point_opacity))
            meter_signature = (fill_height, cursor_top)
            details_signature = (
                slot,
                key_label,
                value_label,
                tuple(trace_signature),
            )
            previous_meter_signature = getattr(
                ui, "rendered_calibration_meter_signature", None
            )
            previous_details_signature = getattr(
                ui, "rendered_calibration_details_signature", None
            )
            # If a HID report only changes raw values within the same rendered
            # pixel, leave the small Flet subtree alone.  This local coalescing
            # is what keeps a held switch smooth on slower desktops.
            if (
                previous_meter_signature == meter_signature
                and previous_details_signature == details_signature
            ):
                return
            ui.live_meter_fill.height = fill_height
            ui.live_meter_fill_glow.height = fill_height
            ui.live_meter_cursor.top = cursor_top
            ui.live_key_text.value = key_label
            ui.live_value_text.value = value_label
            for point, (point_height, point_opacity) in zip(
                ui.live_trace_bars, trace_signature
            ):
                point.height = point_height
                point.opacity = point_opacity
            # The rail is the part that must feel immediate.  Updating the old
            # enclosing 410 px region also serialised the text, all ruler
            # labels and 28 history bars for each level change, causing the
            # visible stutter reported on slower PCs.  The real dialog keeps
            # the moving rail and the slower-changing details in independent
            # local Flet subtrees.  Crucially, one paint tick updates *one*
            # subtree at most: an occasional detail frame takes priority,
            # otherwise the moving rail gets the frame.  This prevents two
            # websocket patches fighting each other for the same UI frame.
            meter_changed = previous_meter_signature != meter_signature
            details_changed = previous_details_signature != details_signature
            meter_region = getattr(ui, "live_meter_layers", None)
            details_region = getattr(ui, "live_summary_region", None)
            if meter_region is not None and details_region is not None:
                now = time.monotonic()
                detail_interval = max(
                    0.12,
                    float(getattr(ui, "calibration_detail_interval", 0.28)),
                )
                last_details_at = float(
                    getattr(ui, "last_calibration_detail_paint_at", float("-inf"))
                )
                details_due = details_changed and now - last_details_at >= detail_interval
                if details_due:
                    details_region.update()
                    ui.rendered_calibration_details_signature = details_signature
                    ui.last_calibration_detail_paint_at = now
                elif meter_changed:
                    meter_region.update()
                    ui.rendered_calibration_meter_signature = meter_signature
                # If the small detail card is deliberately rate-limited and
                # the rail has not moved a whole pixel, no update is required.
                # Its local controls already hold the newest value for the
                # next allowed detail frame.
            else:
                # Lightweight compatibility path for tests/extensions that
                # supplied only the former single `live_region` control.
                ui.live_region.update()
                ui.rendered_calibration_meter_signature = meter_signature
                ui.rendered_calibration_details_signature = details_signature
            # Preserve this attribute for older extensions/tests that inspect
            # the calibration UI state directly.  It denotes the newest local
            # model, not necessarily the rate-limited details paint.
            ui.rendered_telemetry_signature = (meter_signature, details_signature)
        except Exception:
            # A scheduled visual tick may arrive after the modal is dismissed.
            # It is safe to drop the paint because the worker's stop token owns
            # the firmware cleanup independently from this UI.
            pass

    @staticmethod
    def _wait_for_magnetic_calibration_cleanup(calibration, timeout=1.5):
        """Boundedly wait for a detached worker to send its one stop packet."""
        completion = getattr(calibration, "cleanup_complete", None)
        if completion is None:
            return True
        try:
            return bool(completion.wait(max(0.0, float(timeout))))
        except (AttributeError, TypeError, ValueError):
            return False

    def _stop_magnetic_calibration(self, reset_ui=True, expected_dialog_token=None):
        """Request a safe 0x1E/0 cleanup from an active calibration worker."""
        lifecycle_lock = getattr(self, "_magnetic_calibration_lifecycle_lock", None)
        if lifecycle_lock is not None:
            lifecycle_lock.acquire()
        try:
            calibration = getattr(self, "_magnetic_calibration", None)
            if calibration is None:
                return None
            if (
                expected_dialog_token is not None
                and getattr(calibration, "dialog_token", None) != expected_dialog_token
            ):
                return None
            calibration.stop_event.set()
            self._magnetic_calibration = None
            self._magnetic_calibration_token = getattr(
                self, "_magnetic_calibration_token", 0
            ) + 1
            if reset_ui:
                try:
                    calibration.ui.start_button.disabled = False
                    calibration.ui.stop_button.disabled = True
                    completion_lock = getattr(calibration, "completion_lock", None)
                    if completion_lock is not None:
                        with completion_lock:
                            completed_slots = set(
                                getattr(calibration, "completed_slots", set()) or ()
                            )
                    else:
                        completed_slots = set(
                            getattr(calibration, "completed_slots", set()) or ()
                        )
                    total = len(getattr(calibration.ui, "keycaps", {}) or {})
                    if completed_slots and total:
                        calibration.ui.status_text.value = (
                            f"Остановлено. Завершено: {len(completed_slots)}/{total}. "
                            "Можно нажать «Старт» и прожать только остальные клавиши."
                        )
                    else:
                        calibration.ui.status_text.value = (
                            "Калибровка остановлена. Можно начать снова и прожать "
                            "только нужные клавиши."
                        )
                    calibration.ui.status_text.color = ft.Colors.ON_SURFACE_VARIANT
                    calibration.ui.control_region.update()
                except Exception:
                    pass
            # The worker is the sole owner of 0x1E/0.  Returning its session
            # lets the real application exit wait briefly for that single
            # cleanup without issuing a duplicate stop from the UI thread.
            return calibration
        finally:
            if lifecycle_lock is not None:
                lifecycle_lock.release()

    def _open_magnetic_calibration_feature_device_locked(self, label):
        """Open one temporary feature-report handle for a calibration pass.

        The calibration worker already owns both the session lock and
        ``usb_lock``.  Keeping one handle alive while it reads the four 0xFE
        chunks avoids four HID open/close cycles for every sample.  This is
        read-only and deliberately limited to calibration: ordinary settings
        retain the more defensive one-query-per-handle path.
        """
        paths = self.get_keyboard_paths()
        if not paths:
            raise MagneticProtocolError("клавиатура не найдена среди HID-интерфейсов")
        last_error = None
        for path in paths:
            device = None
            try:
                device = hid.device()
                device.open_path(path)
                device.set_nonblocking(0)
                entry = self._active_device() or {}
                cache_key = self._device_key(
                    entry.get("vid", 0),
                    entry.get("pid", 0),
                    entry.get("usage_page", 0),
                )
                self._working_hid_path[cache_key] = path
                logger.debug("calibration [%s] opened reusable HID handle on %s", label, path)
                return device
            except Exception as exc:
                last_error = exc
                logger.debug("calibration [%s] could not open %s: %s", label, path, exc)
                try:
                    if device is not None:
                        device.close()
                except Exception:
                    pass
        raise MagneticProtocolError(
            f"не удалось открыть HID для калибровки: {last_error}"
        )

    @staticmethod
    def _close_magnetic_calibration_feature_device(device):
        """Close a reusable calibration read handle without masking cleanup."""
        try:
            if device is not None:
                device.close()
        except Exception:
            pass

    def _query_magnetic_calibration_packet_with_device_locked(
        self, device, packet, label
    ):
        """Read one official calibration chunk through a live HID handle.

        A feature report is synchronous; the brief settle mirrors the official
        driver's very short calibration polling delay.  On any failure the
        caller discards the handle and falls back to the generic safe reader.
        """
        if len(packet) != MagneticProtocol.REPORT_SIZE:
            raise MagneticProtocolError("некорректная HID-команда")
        try:
            sent = device.send_feature_report([0] + list(packet))
            if sent is None or sent <= 0:
                raise OSError(f"send_feature_report returned {sent}")
            time.sleep(CALIBRATION_FEATURE_REPORT_SETTLE_SECONDS)
            response = list(device.get_feature_report(0, 65))
            if not response:
                raise OSError("empty feature response")
            logger.debug("calibration [%s] response=%s", label, response[:16])
            return response
        except Exception as exc:
            raise MagneticProtocolError(
                f"не удалось прочитать калибровку: {exc}"
            ) from exc

    def _read_magnetic_calibration_progress_locked(
        self, label_prefix, feature_device=None
    ):
        """Read official 0xE5/0xFE chunks while ``usb_lock`` is held.

        ``feature_device`` is an optional calibration-only reusable handle.
        Omitting it preserves the generic read path for startup probes and
        test doubles, while the live worker avoids avoidable HID churn.
        """
        reports = {}
        for chunk_index in range(MagneticProtocol.CALIBRATION_PROGRESS_CHUNKS):
            packet = MagneticProtocol.calibration_progress_packet(chunk_index)
            chunk_label = f"{label_prefix}_{chunk_index}"
            if feature_device is None:
                response = self._query_magnetic_packet_locked(packet, chunk_label)
            else:
                response = self._query_magnetic_calibration_packet_with_device_locked(
                    feature_device, packet, chunk_label
                )
            reports[(MagneticProtocol.OP_CALIBRATION_PROGRESS, chunk_index)] = response
        return MagneticProtocol.decode_calibration_progress(reports)

    def _prime_magnetic_calibration_progress(self, ui, dialog_token):
        """Optionally sample 0xFE before Start without promoting it to a key.

        Womier's own calibration page clears its *visual* completed-key set
        every time that page is opened.  That matters: old ``0xFE == 300``
        values are historical calibration progress, not a new intentional
        press in this dialog.  This helper is kept read-only for diagnostics,
        but it never turns a pre-existing raw value green and never mutates
        ``ui.completed_slots``.  Partial work is retained only by the open
        dialog's Stop -> Start session, where the app has observed a fresh
        post-start press.
        """
        cancel_event = getattr(ui, "pre_read_cancel_event", None)
        if cancel_event is None:
            cancel_event = threading.Event()
            ui.pre_read_cancel_event = cancel_event

        def is_current():
            return (
                not cancel_event.is_set()
                and getattr(self, "_magnetic_calibration_dialog_token", None)
                == dialog_token
                and getattr(self, "_magnetic_calibration", None) is None
            )

        def worker():
            session_lock = getattr(self, "_magnetic_calibration_session_lock", None)
            acquired = False
            try:
                if not is_current():
                    return
                if session_lock is not None:
                    session_lock.acquire()
                    acquired = True
                if not is_current():
                    return
                firmware_version = None
                with self.usb_lock:
                    if not is_current():
                        return
                    try:
                        firmware_version = MagneticProtocol.decode_usb_version(
                            self._query_magnetic_packet_locked(
                                MagneticProtocol.get_usb_version_packet(),
                                "magnetic_calibration_open_usb_version",
                            )
                        )
                    except Exception as exc:
                        logger.debug(
                            "magnetic calibration open version read failed: %s", exc
                        )
                    levels = self._read_magnetic_calibration_progress_locked(
                        "magnetic_calibration_open"
                    )
                if not is_current():
                    return

                def remember_probe_only():
                    if not is_current():
                        return
                    ui.pre_start_levels = dict(levels)
                    ui.pre_start_firmware_version = firmware_version
                    ui.status_text.value = (
                        "Калибровка не запущена. Нажмите «Старт», затем прожимайте "
                        "только нужные клавиши."
                    )
                    ui.status_text.color = ft.Colors.ON_SURFACE_VARIANT
                    ui.control_region.update()

                self._ui_call(remember_probe_only)
            except Exception as exc:
                logger.debug("magnetic calibration open progress read failed: %s", exc)
                if is_current():
                    def show_read_failure():
                        if not is_current():
                            return
                        ui.status_text.value = (
                            "Калибровка не запущена. Можно начать и прожать только "
                            "нужные клавиши."
                        )
                        ui.status_text.color = ft.Colors.ON_SURFACE_VARIANT
                        ui.control_region.update()

                    self._ui_call(show_read_failure)
            finally:
                if acquired:
                    try:
                        session_lock.release()
                    except Exception:
                        pass

        reader = threading.Thread(
            target=worker,
            daemon=True,
            name="sk75-calibration-open-read",
        )
        ui.pre_read_thread = reader
        reader.start()

    def _start_magnetic_calibration(self, ui, dialog_token):
        """Run Womier's exact calibration sequence and poll its raw key levels.

        This is intentionally separate from the 0x1B travel tester.  The
        official driver starts calibration with 0x1C/1, 0x1C/0, 0x1E/1 and
        reads 0xE5/0xFE matrix chunks.  Its worker owns 0x1E/0 in ``finally``
        so Stop, close, hide, quit and I/O failures all restore normal input.
        """
        # Flet can deliver a queued button click after the dialog was already
        # closed, or deliver the same click twice while it is switching the
        # button into its disabled state.  Neither event may create a fresh
        # firmware session.  The dialog generation is deliberately separate
        # from the HID-session token because Close/on_dismiss advance at
        # different times.
        closing_event = getattr(ui, "dialog_closing_event", None)
        if closing_event is not None and closing_event.is_set():
            return
        if dialog_token != getattr(
            self, "_magnetic_calibration_dialog_token", dialog_token
        ):
            return
        entry = self._active_device()
        if entry is None or entry.get("keyboard_type") != "magnetic":
            self._snack("Сначала выберите магнитную клавиатуру SK75.")
            return
        self._stop_magnetic_travel_tester(reset_ui=False)
        # A dialog-open pre-read is only observational.  Cancel it before the
        # firmware-owning session is created so its read-only packets cannot
        # queue behind or race the official 0x1C/0x1E sequence.
        pre_read_cancel_event = getattr(ui, "pre_read_cancel_event", None)
        if pre_read_cancel_event is not None:
            pre_read_cancel_event.set()
        if not isinstance(getattr(ui, "completed_slots", None), set):
            ui.completed_slots = set()
        lifecycle_lock = getattr(self, "_magnetic_calibration_lifecycle_lock", None)
        if lifecycle_lock is not None:
            lifecycle_lock.acquire()
        try:
            active = getattr(self, "_magnetic_calibration", None)
            if (
                active is not None
                and getattr(active, "dialog_token", None) == dialog_token
                and not getattr(active, "stop_event", threading.Event()).is_set()
            ):
                # Do not let a duplicate click tear down and restart a live
                # calibration.  Besides being confusing, that used to leave
                # the newly reopened dialog with a disabled-looking Start
                # button while its predecessor still held the HID lock.
                return
            self._stop_magnetic_calibration(reset_ui=False)
            stop_event = threading.Event()
            samples = queue.Queue(maxsize=1)
            token = getattr(self, "_magnetic_calibration_token", 0) + 1
            self._magnetic_calibration_token = token
            calibration = SimpleNamespace(
                token=token,
                dialog_token=dialog_token,
                stop_event=stop_event,
                samples=samples,
                ui=ui,
                # The same set belongs to the open dialog, rather than a
                # single Start/Stop pass.  This mirrors Womier's lack of an
                # undo packet on Cancel and lets the user resume only the
                # still-unfilled keys after pressing Stop.
                completed_slots=ui.completed_slots,
                completion_lock=threading.Lock(),
                # ``cleanup_complete`` is signalled only after the worker has
                # released the exclusive firmware mode.  The tray quit path
                # uses it instead of sending its own duplicate 0x1E/0.
                cleanup_complete=threading.Event(),
                stop_packet_attempted=False,
                # A key becomes eligible only after the calibration mode has
                # observed it stably released *after* the startup guard.  This
                # prevents a held/stale startup value from becoming a false
                # "completed" key.
                armed_slots=set(),
                release_streaks={},
                full_since={},
            )
            self._magnetic_calibration = calibration
        finally:
            if lifecycle_lock is not None:
                lifecycle_lock.release()
        try:
            ui.start_button.disabled = True
            ui.stop_button.disabled = False
            if ui.completed_slots:
                ui.status_text.value = (
                    f"Продолжаю: уже завершено {len(ui.completed_slots)}/{len(ui.keycaps)}. "
                    "Отпустите все клавиши — короткая защитная пауза…"
                )
            else:
                ui.status_text.value = (
                    "Отпустите все клавиши — короткая защитная пауза перед калибровкой…"
                )
            ui.status_text.color = ft.Colors.ON_SURFACE_VARIANT
            ui.control_region.update()
        except Exception:
            pass

        def publish(sample):
            """Keep exactly one most recent matrix snapshot for the dialog."""
            try:
                while True:
                    samples.get_nowait()
            except queue.Empty:
                pass
            try:
                samples.put_nowait(sample)
            except queue.Full:
                pass

        def worker():
            start_attempted = False
            firmware_version = None
            baseline_levels = {}
            previous_progress_levels = {}
            calibration_feature_device = None
            calibration_feature_reuse_unavailable = False

            def read_calibration_progress(label_prefix):
                """Use one fast read handle, then safely fall back if needed.

                HID read failures must never strand the firmware in calibration
                mode merely because the optional performance path was not
                accepted by a particular Windows HID stack.  A failed reusable
                handle is closed, disabled for this session and the established
                per-query reader takes over.
                """
                nonlocal calibration_feature_device
                nonlocal calibration_feature_reuse_unavailable
                if not calibration_feature_reuse_unavailable:
                    if calibration_feature_device is None:
                        try:
                            calibration_feature_device = (
                                self._open_magnetic_calibration_feature_device_locked(
                                    label_prefix
                                )
                            )
                        except Exception as exc:
                            calibration_feature_reuse_unavailable = True
                            logger.debug(
                                "calibration reusable HID handle unavailable; "
                                "using generic reader: %s",
                                exc,
                            )
                    if calibration_feature_device is not None:
                        try:
                            return self._read_magnetic_calibration_progress_locked(
                                label_prefix,
                                feature_device=calibration_feature_device,
                            )
                        except Exception as exc:
                            self._close_magnetic_calibration_feature_device(
                                calibration_feature_device
                            )
                            calibration_feature_device = None
                            calibration_feature_reuse_unavailable = True
                            logger.debug(
                                "calibration reusable HID read failed; "
                                "using generic reader: %s",
                                exc,
                            )
                return self._read_magnetic_calibration_progress_locked(label_prefix)

            session_lock = self._magnetic_calibration_session_lock
            session_lock.acquire()
            try:
                # Calibration is a firmware mode, not an ordinary one-shot
                # settings read.  Keep the feature-report transport exclusive
                # from the exact start sequence through the final progress
                # poll: profile automation or a delayed slider write must not
                # land between 0x1E/1 and 0x1E/0.
                with self.usb_lock:
                    if stop_event.is_set():
                        return
                    # Firmware version changes only the official completion
                    # threshold (300 on current SK75, 1 on legacy boards).
                    try:
                        firmware_version = MagneticProtocol.decode_usb_version(
                            self._query_magnetic_packet_locked(
                                MagneticProtocol.get_usb_version_packet(),
                                "magnetic_calibration_usb_version",
                            )
                        )
                    except Exception as exc:
                        logger.debug("magnetic calibration version read failed: %s", exc)
                    # This is a diagnostic snapshot only.  It must never add
                    # a visual green key or arm a press before calibration.
                    # A board that refuses this optional pre-read may still
                    # start safely.
                    try:
                        baseline_levels = self._read_magnetic_calibration_progress_locked(
                            "magnetic_calibration_baseline"
                        )
                    except Exception as exc:
                        logger.debug("magnetic calibration baseline read failed: %s", exc)
                    if stop_event.is_set():
                        return
                    # Do not enter the firmware's calibration mode at the
                    # exact moment a user has just opened/clicked the dialog.
                    # This pre-start pause is intentionally outside Womier's
                    # protocol: no 0x1C/0x1E write is sent until it finishes,
                    # so accidental first keystrokes cannot be calibrated.
                    if stop_event.wait(CALIBRATION_START_SETTLE_SECONDS):
                        return
                    if stop_event.is_set():
                        return
                    start_attempted = True
                    self._send_lighting_packets_locked(
                        MagneticProtocol.calibration_start_packets(),
                        "magnetic_calibration_start",
                        inter_packet_delay=0.01,
                    )
                    # Take one post-start reference snapshot without marking
                    # completions.  Do not arm any key from this first sample:
                    # the firmware can still be switching calibration reference
                    # points and its 0xFE values may be stale for a moment.
                    baseline_levels = read_calibration_progress(
                        "magnetic_calibration_arm_baseline"
                    )
                    previous_progress_levels = dict(baseline_levels)
                    arming_deadline = (
                        time.monotonic() + CALIBRATION_POST_START_GUARD_SECONDS
                    )
                    arming_announced = False
                    publish(("baseline", baseline_levels, firmware_version, None))
                    while (
                        not stop_event.is_set()
                        and getattr(self, "_magnetic_calibration_token", None) == token
                        and getattr(self, "_magnetic_calibration", None) is calibration
                    ):
                        levels = read_calibration_progress(
                            "magnetic_calibration_progress"
                        )
                        now = time.monotonic()
                        # Mirror Womier's 500 ms wait after 0x1E/1, but use
                        # those samples only to prove that a key is stably
                        # released.  Crucially, a key that is full at the end
                        # of the guarded phase remains unarmed and must be
                        # released and pressed again; this filters the random
                        # green caps some SK75s report immediately on entry.
                        if now < arming_deadline:
                            with calibration.completion_lock:
                                (
                                    calibration.release_streaks,
                                    _stable_releases,
                                ) = self._calibration_stable_released_slots(
                                    levels,
                                    calibration.release_streaks,
                                    firmware_version,
                                    calibration.ui.keycaps.keys(),
                                    CALIBRATION_RELEASE_STABLE_SAMPLES,
                                )
                            previous_progress_levels = dict(levels)
                        elif not arming_announced:
                            with calibration.completion_lock:
                                (
                                    calibration.release_streaks,
                                    stable_releases,
                                ) = self._calibration_stable_released_slots(
                                    levels,
                                    calibration.release_streaks,
                                    firmware_version,
                                    calibration.ui.keycaps.keys(),
                                    CALIBRATION_RELEASE_STABLE_SAMPLES,
                                )
                                # Only currently, stably released keys become
                                # eligible.  Reset edge/dwell memory at this
                                # exact boundary so a high level observed
                                # during the guard cannot leak through.
                                calibration.armed_slots = set(stable_releases)
                                calibration.full_since.clear()
                            previous_progress_levels = dict(levels)
                            arming_announced = True

                            def announce_armed():
                                if (
                                    getattr(self, "_magnetic_calibration", None)
                                    is calibration
                                    and not stop_event.is_set()
                                ):
                                    calibration.ui.status_text.value = (
                                        "Калибровка готова. Нажмите и удерживайте "
                                        "нужную клавишу примерно секунду до зелёного цвета."
                                    )
                                    calibration.ui.status_text.color = (
                                        ft.Colors.ON_SURFACE_VARIANT
                                    )
                                    calibration.ui.control_region.update()

                            try:
                                self._ui_call(announce_armed)
                            except Exception:
                                pass
                        else:
                            with calibration.completion_lock:
                                (
                                    calibration.armed_slots,
                                    newly_completed,
                                    calibration.release_streaks,
                                    calibration.full_since,
                                ) = self._calibration_confirmed_completion_edges(
                                    previous_progress_levels,
                                    levels,
                                    calibration.armed_slots,
                                    calibration.completed_slots,
                                    release_streaks=calibration.release_streaks,
                                    full_since=calibration.full_since,
                                    now=now,
                                    firmware_version=firmware_version,
                                    visible_slots=calibration.ui.keycaps.keys(),
                                    required_release_samples=CALIBRATION_RELEASE_STABLE_SAMPLES,
                                    required_full_seconds=CALIBRATION_FULL_HOLD_SECONDS,
                                )
                                calibration.completed_slots.update(newly_completed)
                            previous_progress_levels = dict(levels)
                        publish(("progress", levels, firmware_version, None))
                        # The reusable four-chunk feature read is fast enough
                        # for a responsive local rail.  This small cooperative
                        # pause avoids a busy HID loop while keeping the
                        # pressed-cap fill close to the Flutter frame cadence.
                        # Event.wait releases no lock, deliberately keeping
                        # this exclusive firmware mode free from unrelated
                        # magnetic writes until its matching stop command.
                        stop_event.wait(CALIBRATION_PROGRESS_POLL_PAUSE_SECONDS)
            except Exception as exc:
                logger.debug("magnetic calibration failed: %s", exc)
                if not stop_event.is_set():
                    publish(("error", None, firmware_version, str(exc)))
            finally:
                try:
                    # Close the optional long-lived reader before the normal
                    # stop helper opens its own feature-report handle.  It is
                    # only a performance optimisation; closing it cannot alter
                    # any calibration reference held by the firmware.
                    self._close_magnetic_calibration_feature_device(
                        calibration_feature_device
                    )
                    # Always send Womier's official stop packet after even a
                    # partially failed start attempt.  0x1E/0 is harmless when
                    # the board was never put into calibration mode and is the
                    # only write used for recovery.
                    if start_attempted and not calibration.stop_packet_attempted:
                        # Only this worker ever writes the stop packet.  The
                        # lifecycle callbacks merely set ``stop_event`` and
                        # detach the session, which prevents duplicate writes
                        # when Close and on_dismiss arrive back-to-back.
                        calibration.stop_packet_attempted = True
                        with self.usb_lock:
                            self._send_lighting_packets_locked(
                                [MagneticProtocol.calibration_stop_packet()],
                                "magnetic_calibration_stop",
                                inter_packet_delay=0.0,
                            )
                except Exception as exc:
                    logger.debug("magnetic calibration stop failed: %s", exc)
                finally:
                    try:
                        session_lock.release()
                    finally:
                        calibration.cleanup_complete.set()

        async def paint_loop():
            levels = {}
            baseline_levels = {}
            previous_live_levels = {}
            firmware_version = None
            error_text = None
            active_slot = None
            live_history = []
            deck_dirty = False
            telemetry_dirty = False
            last_deck_paint_at = 0.0
            last_telemetry_paint_at = 0.0
            while (
                not stop_event.is_set()
                and getattr(self, "_magnetic_calibration_token", None) == token
                and getattr(self, "_magnetic_calibration", None) is calibration
            ):
                received_snapshot = False
                telemetry_changed = False
                try:
                    while True:
                        kind, payload, observed_firmware, error_text = samples.get_nowait()
                        if observed_firmware is not None:
                            firmware_version = observed_firmware
                        if kind == "baseline":
                            baseline_levels = payload or {}
                            # Do not treat the pre-existing calibration state
                            # as a fresh keypress.  The next progress snapshot
                            # is compared to this baseline instead.
                            previous_live_levels = dict(baseline_levels)
                            received_snapshot = True
                        elif kind == "progress":
                            levels = payload or {}
                            changed_slot, raw_level = self._calibration_live_change(
                                previous_live_levels,
                                levels,
                                visible_slots=ui.keycaps.keys(),
                            )
                            previous_live_levels = dict(levels)
                            if changed_slot is not None:
                                active_slot = changed_slot
                                try:
                                    live_fraction = MagneticProtocol.calibration_progress_fraction(
                                        raw_level, firmware_version
                                    )
                                except MagneticProtocolError:
                                    live_fraction = 0.0
                                live_history.append(live_fraction)
                                # A finite history stays cheap to paint even if
                                # firmware delivers hundreds of samples while
                                # a user is holding one switch down.
                                del live_history[:-28]
                                telemetry_changed = True
                            received_snapshot = True
                except queue.Empty:
                    pass
                # Reports are deliberately coalesced: if the device produces
                # a newer matrix before the next local frame, paint only that
                # newest state.  This never changes the official HID cadence
                # or safety sequence; it just prevents a UI backlog.
                deck_dirty = deck_dirty or received_snapshot
                telemetry_dirty = telemetry_dirty or telemetry_changed
                now = time.monotonic()
                if deck_dirty and (
                    now - last_deck_paint_at >= getattr(ui, "deck_paint_interval", 0.10)
                ):
                    with calibration.completion_lock:
                        completed_slots = set(calibration.completed_slots)
                    self._paint_magnetic_calibration(
                        ui,
                        levels,
                        baseline_levels=baseline_levels,
                        firmware_version=firmware_version,
                        completed_slots=completed_slots,
                        # The worker owns completion eligibility.  A raw full
                        # reading alone is not enough while the startup guard
                        # is active, otherwise a held key could become green
                        # before it has been released and pressed again.
                        allow_raw_completion=False,
                        # The full deck is refreshed only for a confirmed
                        # green completion.  The live rail below is the sole
                        # high-frame-rate visual while a key is moving.
                        completed_only=True,
                    )
                    deck_dirty = False
                    last_deck_paint_at = now
                if telemetry_dirty and (
                    now - last_telemetry_paint_at
                    >= getattr(ui, "telemetry_paint_interval", 0.08)
                ):
                    self._paint_magnetic_calibration_telemetry(
                        ui,
                        active_slot,
                        live_history[-1],
                        live_history,
                    )
                    telemetry_dirty = False
                    last_telemetry_paint_at = now
                if error_text:
                    try:
                        ui.status_text.value = f"Не удалось калибровать: {error_text}"
                        ui.status_text.color = ft.Colors.ERROR
                        ui.start_button.disabled = False
                        ui.stop_button.disabled = True
                        ui.control_region.update()
                    except Exception:
                        pass
                    self._stop_magnetic_calibration(
                        reset_ui=False, expected_dialog_token=dialog_token
                    )
                    break
                # Queue holds only the newest snapshot.  A short task sleep
                # lets the meter repaint on the next small local frame while
                # the heavier 81-key deck remains separately capped above.
                await asyncio.sleep(0.025)

        reader = threading.Thread(
            target=worker,
            daemon=True,
            name="sk75-calibration-reader",
        )
        calibration.reader_thread = reader
        reader.start()
        try:
            self.page.run_task(paint_loop)
        except Exception:
            self._stop_magnetic_calibration(
                reset_ui=False, expected_dialog_token=dialog_token
            )

    def _open_magnetic_calibration(self):
        """Open the self-contained official-style SK75 calibration panel."""
        self._stop_magnetic_travel_tester(reset_ui=False)
        self._stop_magnetic_calibration(reset_ui=False)
        dialog_token = getattr(self, "_magnetic_calibration_dialog_token", 0) + 1
        self._magnetic_calibration_dialog_token = dialog_token
        board, keycaps, fill_height = self._build_sk75_calibration_layout()
        progress_text = ft.Text(
            f"Готово: —/{len(keycaps)}",
            size=12,
            weight=ft.FontWeight.W_700,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        status_text = ft.Text(
            "Калибровка не запущена. Нажмите «Старт», затем прожимайте только нужные клавиши.",
            size=11,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        # This is deliberately based on the calibration matrix snapshots, not
        # on a second 0x1B live-travel session.  Calibration owns the firmware
        # exclusively, while this local visual makes the last observed press
        # understandable without adding any competing HID command.
        # ``0xFE`` gives a calibration *level*, not a live travel depth.  It
        # is still much clearer to render that level against SK75's official
        # calibrated range than as an opaque 0–100% bar.  The caption below
        # makes the distinction explicit so this can never be mistaken for
        # the separate ``Проверка хода`` measurement.
        calibrated_range_mm = 3.30
        live_meter_rail_width = 40
        live_meter_rail_height = 166
        live_meter_inner_top = 5
        live_meter_inner_height = 156
        live_meter_track = ft.Container(
            width=live_meter_rail_width,
            height=live_meter_rail_height,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=0,
        )
        live_meter_fill_glow = ft.Container(
            left=3,
            bottom=live_meter_inner_top,
            width=34,
            height=0,
            gradient=ft.LinearGradient(
                begin=ft.Alignment.CENTER_LEFT,
                end=ft.Alignment.CENTER_RIGHT,
                colors=[
                    ft.Colors.TRANSPARENT,
                    ft.Colors.with_opacity(0.32, "#39D98A"),
                    ft.Colors.TRANSPARENT,
                ],
            ),
        )
        live_meter_fill = ft.Container(
            left=7,
            bottom=live_meter_inner_top,
            width=26,
            height=0,
            bgcolor="#39D98A",
            border_radius=0,
        )
        live_meter_cursor = ft.Container(
            left=3,
            top=live_meter_inner_top + live_meter_inner_height - 1,
            width=34,
            height=2,
            bgcolor="#7AF0B0",
            border_radius=0,
        )
        live_meter_layers = ft.Stack(
            [live_meter_track, live_meter_fill_glow, live_meter_fill, live_meter_cursor],
            width=live_meter_rail_width,
            height=live_meter_rail_height,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        # Match the travel tester's ruler language: labelled major divisions
        # and short intermediate marks to the right of a clipped rail.  The
        # direction is deliberately progress-oriented (0.00 at the bottom,
        # 3.30 at the top): the green calibration fill grows upward.
        live_tick_values = (0.00, 0.50, 1.00, 1.50, 2.00, 2.50, 3.00, 3.30)
        live_minor_ticks = []
        for tick_index in range(34):
            tick_value = min(calibrated_range_mm, tick_index * 0.10)
            tick_fraction = tick_value / calibrated_range_mm
            tick_top = round(
                live_meter_inner_top
                + live_meter_inner_height * (1.0 - tick_fraction)
            )
            major = tick_index % 5 == 0 or tick_index == 33
            live_minor_ticks.append(
                ft.Container(
                    left=49,
                    top=max(0, min(live_meter_rail_height - 1, tick_top)),
                    width=19 if major else 9,
                    height=1 if not major else 2,
                    bgcolor=ft.Colors.with_opacity(
                        0.72 if major else 0.34, ft.Colors.ON_SURFACE
                    ),
                )
            )
        live_tick_labels = []
        for tick_value in live_tick_values:
            tick_fraction = tick_value / calibrated_range_mm
            tick_top = round(
                live_meter_inner_top
                + live_meter_inner_height * (1.0 - tick_fraction)
            )
            live_tick_labels.append(
                ft.Container(
                    content=ft.Text(
                        f"{tick_value:.2f} мм",
                        size=9,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                        no_wrap=True,
                    ),
                    left=74,
                    top=max(0, min(live_meter_rail_height - 12, tick_top - 6)),
                )
            )
        live_meter = ft.Stack(
            [live_meter_layers, *live_minor_ticks, *live_tick_labels],
            width=150,
            height=live_meter_rail_height,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        live_trace_height = 42
        live_trace_count = 28
        live_trace_bars = [
            ft.Container(
                left=4 + index * 6,
                bottom=2,
                width=3,
                height=1,
                bgcolor="#39D98A",
                opacity=0.14,
                border_radius=1,
            )
            for index in range(live_trace_count)
        ]
        live_trace = ft.Stack(
            [
                ft.Container(
                    left=0,
                    right=0,
                    bottom=1,
                    height=1,
                    bgcolor=ft.Colors.OUTLINE_VARIANT,
                ),
                *live_trace_bars,
            ],
            width=176,
            height=live_trace_height + 3,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        live_key_text = ft.Text(
            "Нажмите любую клавишу",
            size=11,
            weight=ft.FontWeight.W_600,
            color=ft.Colors.ON_SURFACE,
            no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS,
            width=182,
        )
        live_value_text = ft.Text(
            f"0.00 / {calibrated_range_mm:.2f} мм",
            size=18,
            weight=ft.FontWeight.W_700,
            color="#39D98A",
        )
        # Keep the moving rail and the history/text bundle as separate local
        # update roots.  A calibration snapshot may change dozens of raw
        # values, but the rail itself should not wait for Flet to reserialize
        # its static ruler or every trace bar before it moves.
        live_summary_region = ft.Container(
            content=ft.Column(
                [
                    ft.Text(
                        "Живая индикация калибровки",
                        size=11,
                        weight=ft.FontWeight.W_700,
                    ),
                    live_key_text,
                    live_value_text,
                    ft.Text(
                        "История последних измерений",
                        size=9,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    live_trace,
                    ft.Text(
                        "Позиция в диапазоне калибровки, не измеренный ход клавиши и не ход в мм: физическую глубину показывает «Проверка хода».",
                        size=9,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                        width=220,
                        no_wrap=False,
                    ),
                ],
                spacing=3,
                tight=True,
            ),
            width=220,
        )
        live_region = ft.Container(
            content=ft.Row(
                [
                    live_meter,
                    live_summary_region,
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            width=410,
            height=188,
            padding=10,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=14,
        )
        start_button = ft.FilledButton("Старт", icon=ft.Icons.PLAY_ARROW_ROUNDED)
        stop_button = ft.OutlinedButton(
            "Стоп", icon=ft.Icons.STOP_ROUNDED, disabled=True
        )
        control_region = ft.Column(
            [
                ft.Row([start_button, stop_button, progress_text], spacing=10),
                status_text,
            ],
            spacing=5,
            tight=True,
        )
        visual_region = ft.Container(
            content=board,
            width=900,
            alignment=ft.Alignment.CENTER,
        )
        ui = SimpleNamespace(
            keycaps=keycaps,
            fill_height=fill_height,
            # Stays with this opened dialog across multiple Start/Stop
            # passes.  It is populated by a read-only open pre-read and by
            # live official 0xFE progress snapshots.
            completed_slots=set(),
            pre_read_cancel_event=threading.Event(),
            # Close and on_dismiss may both be emitted for one Flet dialog.
            # This event also makes any queued Start click a no-op after the
            # panel begins closing, which is essential when immediately
            # reopening calibration.
            dialog_closing_event=threading.Event(),
            pre_start_levels={},
            pre_start_firmware_version=None,
            progress_text=progress_text,
            status_text=status_text,
            start_button=start_button,
            stop_button=stop_button,
            visual_region=visual_region,
            live_region=live_region,
            calibrated_range_mm=calibrated_range_mm,
            # Separate frame caps keep a rapid HID reader from outpacing the
            # Flutter client.  Snapshots are retained locally and painted on
            # the next due frame, so no calibration state is discarded.
            # The full 81-key board only needs an occasional completion
            # refresh; keep the small live rail much more responsive and
            # independent from those heavier row patches.
            deck_paint_interval=0.18,
            telemetry_paint_interval=0.045,
            calibration_detail_interval=0.28,
            live_meter_rail_height=live_meter_rail_height,
            live_meter_inner_top=live_meter_inner_top,
            live_meter_inner_height=live_meter_inner_height,
            live_meter_fill_glow=live_meter_fill_glow,
            live_meter_fill=live_meter_fill,
            live_meter_cursor=live_meter_cursor,
            live_meter_layers=live_meter_layers,
            live_summary_region=live_summary_region,
            live_trace_bars=live_trace_bars,
            live_trace_height=live_trace_height,
            live_key_text=live_key_text,
            live_value_text=live_value_text,
            control_region=control_region,
        )

        def start(_event=None):
            self._start_magnetic_calibration(ui, dialog_token)

        def stop(_event=None):
            if ui.dialog_closing_event.is_set():
                return
            ui.pre_read_cancel_event.set()
            self._stop_magnetic_calibration(
                reset_ui=True, expected_dialog_token=dialog_token
            )

        def close_dialog(notification=None):
            """Close this exact dialog before opening any feedback snackbar.

            ``SnackBar`` is a ``DialogControl`` in Flet 0.85.  The previous
            Cancel path showed the snackbar first and then called
            ``page.pop_dialog()``; that popped the snackbar instead of this
            calibration panel.  The panel remained mounted as a transparent
            modal and intercepted clicks after the next open.  Marking this
            concrete dialog closed first is stack-independent and leaves its
            later on_dismiss callback harmless.
            """
            if ui.dialog_closing_event.is_set():
                return
            ui.dialog_closing_event.set()
            ui.pre_read_cancel_event.set()
            self._stop_magnetic_calibration(
                reset_ui=False, expected_dialog_token=dialog_token
            )
            try:
                if getattr(dialog, "open", False):
                    dialog.open = False
                    try:
                        dialog.update()
                    except Exception:
                        # ``open`` has already been flipped.  If the dialog
                        # was concurrently removed by Flet, do *not* pop the
                        # stack here: a feedback snackbar could now be on
                        # top and must never be closed in its place.
                        pass
            except Exception:
                # Fallback for an older portable Flet runtime.  It is only
                # used if this dialog's own ``open`` state could not be
                # changed; in the normal path we never pop a possible
                # snackbar above it.
                try:
                    self.page.pop_dialog()
                except Exception:
                    pass
            if notification:
                self._snack(notification)

        def cancel_calibration(_event=None):
            """Leave calibration mode and close without pretending to undo it.

            Womier's documented cancel path is only ``0x1E/0``.  A key that
            has already reached the firmware's completion point cannot be
            safely rolled back from this application, so the button is an
            explicit *stop and close* action rather than a destructive reset.
            """
            close_dialog(
                "Калибровка отменена. Уже завершённые клавиши Womier не откатывает."
            )

        def close(_event=None):
            close_dialog()

        def dismiss(_event=None):
            ui.dialog_closing_event.set()
            ui.pre_read_cancel_event.set()
            self._stop_magnetic_calibration(
                reset_ui=False, expected_dialog_token=dialog_token
            )

        start_button.on_click = start
        stop_button.on_click = stop
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Калибровка магнитных клавиш"),
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            "Старт запускает штатную калибровку Womier. Зелёная заливка "
                            "означает, что завершена именно эта клавиша; для полной "
                            "калибровки Womier советует пройти все клавиши. Можно остановиться "
                            "после нужных 1–2 клавиш и потом продолжить только оставшиеся. "
                            "Перед стартом есть защитная пауза: отпустите все клавиши и "
                            "дождитесь текста «Калибровка готова».",
                            size=12,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        visual_region,
                        live_region,
                        ft.Text(
                            "На время калибровки обычный ввод может быть отключён. "
                            "Стоп, закрытие окна, скрытие в трей и выход всегда отправляют "
                            "команду остановки.",
                            size=10,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        control_region,
                    ],
                    spacing=10,
                    tight=True,
                ),
                width=970,
            ),
            actions=[
                ft.OutlinedButton(
                    "Отменить калибровку",
                    icon=ft.Icons.CANCEL_ROUNDED,
                    on_click=cancel_calibration,
                ),
                ft.TextButton("Закрыть", on_click=close),
            ],
            on_dismiss=dismiss,
            shape=ft.RoundedRectangleBorder(radius=22),
        )
        self.page.show_dialog(dialog)
        # Match Womier's own calibration page: every freshly opened dialog
        # starts with an empty *visual* completion set.  Reusing historical
        # 0xFE=300 values here made ordinary old records look like accidental
        # presses.  Stop -> Start in this same dialog still retains only the
        # keys confirmed during this run.

    def _set_magnetic_status(self, text, color=None):
        def update():
            try:
                self.magnetic_status.value = text
                self.magnetic_status.color = color or ft.Colors.ON_SURFACE_VARIANT
                # A status line is a fixed leaf.  Updating the complete page
                # from a delayed HID worker can otherwise overlap the selected
                # key panel's native switch patch and make Flet diff a changing
                # control tree.  Keep this cosmetic update strictly local.
                self.magnetic_status.update()
            except Exception:
                pass
        self._ui_call(update)

    def _send_magnetic_packets(self, packets, label):
        try:
            return self._send_lighting_packets(packets, label, inter_packet_delay=0.01)
        except LightingProtocolError as exc:
            raise MagneticProtocolError(str(exc)) from exc

    def _query_magnetic_packet(self, packet, label):
        """Send a read command and return the raw 65-byte feature response."""
        with self.usb_lock:
            return self._query_magnetic_packet_locked(packet, label)

    def _query_magnetic_packet_locked(self, packet, label):
        """Read one magnetic feature report while the caller owns ``usb_lock``.

        Long-lived diagnostic modes (travel testing and calibration) keep the
        transport exclusive from their start command through the final stop.
        Splitting this from the public wrapper avoids attempting to acquire the
        non-reentrant lock again for every read chunk.
        """
        if len(packet) != MagneticProtocol.REPORT_SIZE:
            raise MagneticProtocolError("некорректная HID-команда")
        paths = self.get_keyboard_paths()
        if not paths:
            raise MagneticProtocolError("клавиатура не найдена среди HID-интерфейсов")
        last_error = None
        for path in paths:
            device = None
            try:
                device = hid.device()
                device.open_path(path)
                device.set_nonblocking(0)
                sent = device.send_feature_report([0] + list(packet))
                if sent is None or sent <= 0:
                    raise OSError(f"send_feature_report returned {sent}")
                time.sleep(0.03)
                response = list(device.get_feature_report(0, 65))
                if not response:
                    raise OSError("empty feature response")
                logger.debug("magnetic [%s] response=%s", label, response[:16])
                return response
            except Exception as exc:
                last_error = exc
                logger.debug("magnetic [%s] query failed on %s: %s", label, path, exc)
            finally:
                try:
                    if device is not None:
                        device.close()
                except Exception:
                    pass
        raise MagneticProtocolError(f"не удалось прочитать настройки клавиатуры: {last_error}")

    def _read_magnetic_matrix_locked(self, label_prefix, *, include_keyboard_options=False):
        """Read and decode the physical SK75 magnetic state under ``usb_lock``.

        Profile presets are application-side snapshots; their values cannot be
        trusted as proof that a previous HID write reached the keyboard.  This
        helper uses only Womier's documented ``0xE5`` / ``0x89`` feature reads
        and is used after a profile write as a readback acknowledgement.  The
        caller must already own ``usb_lock`` so a different profile, slider or
        diagnostic session cannot land between the write and this check.
        """
        reports = {}
        for operation, chunk_count in (
            (MagneticProtocol.OP_MODE, 2),
            (MagneticProtocol.OP_ACTUATION, 4),
            (MagneticProtocol.OP_DEACTIVATION, 4),
            (MagneticProtocol.OP_RAPID_PRESS, 4),
            (MagneticProtocol.OP_RAPID_RELEASE, 4),
            (MagneticProtocol.OP_LOWER_DEAD_ZONE, 4),
            (MagneticProtocol.OP_UPPER_DEAD_ZONE, 4),
        ):
            for chunk_index in range(chunk_count):
                reports[(operation, chunk_index)] = self._query_magnetic_packet_locked(
                    MagneticProtocol.get_multi_magnetism_packet(operation, chunk_index),
                    f"{label_prefix}_{operation}_{chunk_index}",
                )
        settings = MagneticProtocol.decode_multi_magnetism(reports)
        if not settings:
            raise MagneticProtocolError("клавиатура не вернула значения магнитных клавиш")
        modes = MagneticProtocol.decode_multi_magnetism_modes(reports)
        options = None
        if include_keyboard_options:
            options = MagneticProtocol.decode_keyboard_options(
                self._query_magnetic_packet_locked(
                    MagneticProtocol.get_keyboard_options_packet(),
                    f"{label_prefix}_kboption",
                )
            )
        return settings, modes, options

    @staticmethod
    def _magnetic_profile_readback_mismatches(
        expected_settings,
        actual_settings,
        actual_modes,
        *,
        expected_options=None,
        actual_options=None,
    ):
        """Return only the profile values that the keyboard did not confirm.

        A non-RT key has stored RT thresholds too, but Womier's own simple
        write intentionally does not update them while RT is off.  Verify the
        thresholds only when the selected profile actually enables RT; the
        mode, actuation and both dead zones are always checked.
        """
        mismatches = []
        for slot, expected in expected_settings.items():
            actual = actual_settings.get(slot)
            actual_mode = actual_modes.get(slot)
            expected_mode = (
                MagneticProtocol.MODE_NORMAL
                | (
                    MagneticProtocol.MODE_RAPID_TRIGGER_BIT
                    if expected.rapid_trigger
                    else 0
                )
            )
            if actual is None or actual_mode is None:
                mismatches.append(slot)
                continue
            values_match = (
                actual.actuation == expected.actuation
                and actual.deactivation == expected.deactivation
                and actual.rapid_trigger == expected.rapid_trigger
                and actual.lower_dead_zone == expected.lower_dead_zone
                and actual.upper_dead_zone == expected.upper_dead_zone
            )
            if expected.rapid_trigger:
                values_match = values_match and (
                    actual.rapid_press == expected.rapid_press
                    and actual.rapid_release == expected.rapid_release
                )
            mode_match = (int(actual_mode) & 0xFF) == expected_mode
            if not values_match or not mode_match:
                mismatches.append(slot)
        options_match = (
            expected_options is None or actual_options == expected_options
        )
        return mismatches, options_match

    def _read_magnetic_matrix(
        self,
        silent=False,
        capture_to_selected=False,
        capture_to_profile_index=None,
    ):
        """Read all SK75 per-key values using only GET_MULTI_MAGNETISM packets."""
        entry = self._active_device()
        if entry is None or entry.get("keyboard_type") != "magnetic":
            if not silent:
                self._set_magnetic_status("Сначала выберите магнитную клавиатуру SK75.", ft.Colors.ERROR)
            return
        if not silent:
            self._set_magnetic_status("Читаю реальные значения магнитных клавиш…")
        # Retain the old keyword for configuration migrations/tests, but bind
        # its target before the worker begins.  A user may select another
        # preset while a full HID matrix read is running.
        if capture_to_profile_index is None and capture_to_selected:
            capture_to_profile_index = self._selected_magnetic_profile_index()
        # The full read runs in a worker.  Remember which device was selected
        # when it began, so a late report cannot populate a newly selected
        # device's cache while it is being serialised by another UI action.
        device_key = self.config.get("active_device")
        if capture_to_profile_index is not None:
            try:
                capture_to_profile_index = int(capture_to_profile_index)
            except (TypeError, ValueError):
                capture_to_profile_index = 0
            capture_to_profile_index = max(
                0, min(MAGNETIC_PROFILE_COUNT - 1, capture_to_profile_index)
            )

        def worker():
            try:
                # Treat the 26 matrix chunks and the optional global options
                # as one physical snapshot.  Taking/releasing ``usb_lock``
                # for each request allowed a debounced slider write to land
                # between chunks; the resulting hybrid response could then
                # overwrite the just-dragged key in the local cache.  That
                # looked like a ruler that only refreshed after flipping an
                # unrelated switch.  The startup controls stay disabled while
                # this short transaction runs, so holding the lock here does
                # not add interaction latency.
                with self.usb_lock:
                    reports = {}
                    for operation, chunk_count in (
                        (MagneticProtocol.OP_MODE, 2),
                        (MagneticProtocol.OP_ACTUATION, 4),
                        (MagneticProtocol.OP_DEACTIVATION, 4),
                        (MagneticProtocol.OP_RAPID_PRESS, 4),
                        (MagneticProtocol.OP_RAPID_RELEASE, 4),
                        (MagneticProtocol.OP_LOWER_DEAD_ZONE, 4),
                        (MagneticProtocol.OP_UPPER_DEAD_ZONE, 4),
                    ):
                        for chunk_index in range(chunk_count):
                            reports[(operation, chunk_index)] = self._query_magnetic_packet_locked(
                                MagneticProtocol.get_multi_magnetism_packet(operation, chunk_index),
                                f"magnetic_matrix_{operation}_{chunk_index}",
                            )
                    decoded = MagneticProtocol.decode_multi_magnetism(reports)
                    if not decoded:
                        raise MagneticProtocolError("клавиатура не вернула значения магнитных клавиш")
                    modes = MagneticProtocol.decode_multi_magnetism_modes(reports)
                    # Read the global options in the same snapshot so RTStab
                    # cannot briefly show a value from another HID state.
                    try:
                        options = MagneticProtocol.decode_keyboard_options(
                            self._query_magnetic_packet_locked(
                                MagneticProtocol.get_keyboard_options_packet(),
                                "magnetic_get_kboption_startup",
                            )
                        )
                    except MagneticProtocolError as exc:
                        options = None
                        logger.debug("initial keyboard options read failed: %s", exc)
                # A raw matrix read is still read-only.  Store the same values
                # through the official UI bounds so the first paint cannot
                # advertise an unselectable 3.50 mm threshold from an old
                # protocol cache.  No HID write is queued here.  The cache
                # mutation must use the same lock as ``save_config``: a
                # delayed startup read can otherwise resize this mapping while
                # a rapid RT toggle is taking its JSON snapshot.
                with _CONFIG_WRITE_LOCK:
                    if self.config.get("active_device") != device_key:
                        return
                    entry = self._active_device()
                    if entry is None:
                        return
                    entry["magnetic_key_settings"] = {
                        str(slot): self._magnetic_settings_to_config(settings)
                        for slot, settings in decoded.items()
                    }
                    entry["magnetic_key_modes"] = {
                        str(slot): mode for slot, mode in modes.items()
                    }
                    if options is not None:
                        entry["magnetic_keyboard_options"] = {
                            "fn_index": options.fn_index,
                            "anti_accidental": options.anti_accidental,
                            "rt_stab": options.rt_stab,
                            "wasd_swap": options.wasd_swap,
                            "system": options.system,
                        }
                        self._magnetic_keyboard_options_cache = options
                    if capture_to_profile_index is not None:
                        captured_index = capture_to_profile_index
                        self._copy_live_magnetic_to_profile(entry, captured_index)
                    else:
                        # Fresh installs have no profile snapshots yet.  Seed them
                        # once from the real read, but never replace an existing
                        # independent preset merely because the app starts.
                        self._seed_uninitialized_magnetic_profiles(entry)
                self.save_config(reload_runtime=False)

                def update():
                    # The physical cache is now verified, but a fresh editor
                    # still has no selected key.  Keep its rulers neutral and
                    # disabled until the first layout click instead of briefly
                    # exposing Q's cached values.
                    self._magnetic_values_ready = True
                    self._load_magnetic_controls(self.magnetic_selected_slot)
                    if options is not None:
                        self.magnetic_rt_stab_dropdown.value = str(options.rt_stab)
                        self.magnetic_anti_accidental_switch.value = options.anti_accidental
                    self._refresh_sk75_keyboard_picker()
                    if capture_to_profile_index is not None:
                        self.magnetic_status.value = (
                            f"Значения добавлены в набор «{self._magnetic_profile_label(captured_index)}»."
                        )
                    else:
                        # The controls themselves are the useful feedback;
                        # do not leave a long technical readout under them.
                        self.magnetic_status.value = ""
                    self.magnetic_status.color = ft.Colors.GREEN_300
                    # `_load_magnetic_controls()` and the keyboard picker each
                    # patch their own stable subtree.  A full page diff from a
                    # late HID read can otherwise race a user clicking RT and
                    # make Flet traverse the changing parameter-control map.
                    try:
                        self.magnetic_status.update()
                    except Exception:
                        pass

                self._ui_call(update)
            except MagneticProtocolError as exc:
                if not silent:
                    self._set_magnetic_status(str(exc), ft.Colors.ERROR)
                else:
                    logger.debug("initial magnetic matrix read failed: %s", exc)

        threading.Thread(target=worker, daemon=True, name="magnetic-matrix-read").start()

    @staticmethod
    def _magnetic_slider_mm(slider):
        return float(slider.value) / 100.0

    def _magnetic_settings_from_controls(self):
        selected_slot = getattr(self, "magnetic_selected_slot", None)
        if hasattr(self, "magnetic_selected_slot") and selected_slot not in SK75_KEY_BY_SLOT:
            raise MagneticProtocolError("сначала выберите клавишу на раскладке")
        # Release is the single, ordinary RT value.  The repeat-down field is
        # only independent after the user explicitly enables the additional
        # control; otherwise both protocol fields deliberately get the same
        # threshold.
        rapid_release = self._magnetic_slider_mm(self.magnetic_rt_release_slider)
        rapid_press = (
            self._magnetic_slider_mm(self.magnetic_rt_press_slider)
            if self.magnetic_rt_separate_switch.value
            else rapid_release
        )
        # Do not pass ``self.magnetic_actuation_slider`` as ``getattr``'s
        # default: Python evaluates that default eagerly, which breaks the
        # intentionally tiny test/legacy managers that expose only the
        # deactivation control.  Production owns both controls.
        deactivation_slider = getattr(self, "magnetic_deactivation_slider", None)
        if deactivation_slider is None:
            deactivation_slider = self.magnetic_actuation_slider
        actuation = self._magnetic_slider_mm(self.magnetic_actuation_slider)
        deactivation = self._magnetic_slider_mm(deactivation_slider)
        # With Rapid Trigger disabled the optional ordinary release threshold
        # is intentionally tied to activation until the user enables its
        # dedicated switch.  This maps to the official ``liftTravel`` value;
        # no synthetic profile-only setting is introduced.
        if (
            not bool(self.magnetic_rt_switch.value)
            and not bool(
                getattr(
                    getattr(self, "magnetic_deactivation_separate_switch", None),
                    "value",
                    True,
                )
            )
        ):
            deactivation = actuation
        return MagneticProtocol.clamp_key_settings_to_official_bounds(
            KeyMagneticSettings(
                actuation=actuation,
                rapid_trigger=bool(self.magnetic_rt_switch.value),
                rapid_press=rapid_press,
                rapid_release=rapid_release,
                lower_dead_zone=self._magnetic_slider_mm(self.magnetic_lower_dead_zone_slider),
                upper_dead_zone=self._magnetic_slider_mm(self.magnetic_upper_dead_zone_slider),
                deactivation=deactivation,
            )
        )

    def _magnetic_apply_key(self):
        try:
            slot = self.magnetic_selected_slot
            if self._magnetic_key_is_advanced(slot):
                raise MagneticProtocolError(
                    "эта клавиша использует Snap Key; измените её пару в отдельном окне"
                )
            settings = self._magnetic_settings_from_controls()
            packets = MagneticProtocol.key_settings_packets(slot, settings)
        except (TypeError, ValueError, MagneticProtocolError) as exc:
            self._set_magnetic_status(f"Проверьте параметры магнитной клавиши: {exc}", ft.Colors.ERROR)
            return

        def worker():
            try:
                self._send_magnetic_packets(packets, f"magnetic_key_{slot}")
                self._store_magnetic_settings(slot, settings)
                if getattr(self, "_womier_cache_sync_lock", None) is not None:
                    self._queue_womier_cache_sync(
                        self._selected_magnetic_profile_index(),
                        key_settings={slot: settings},
                        key_modes={
                            slot: MagneticProtocol.MODE_NORMAL
                            | (
                                MagneticProtocol.MODE_RAPID_TRIGGER_BIT
                                if settings.rapid_trigger
                                else 0
                            )
                        },
                    )
                self._ui_call(self._refresh_sk75_keyboard_picker)
                self._set_magnetic_status("Параметры записаны только для выбранной клавиши.", ft.Colors.GREEN_300)
            except MagneticProtocolError as exc:
                self._set_magnetic_status(str(exc), ft.Colors.ERROR)

        threading.Thread(target=worker, daemon=True, name="magnetic-key-settings").start()

    def _magnetic_read_keyboard_options(self, silent=False):
        def worker():
            try:
                response = self._query_magnetic_packet(
                    MagneticProtocol.get_keyboard_options_packet(), "magnetic_get_kboption"
                )
                options = MagneticProtocol.decode_keyboard_options(response)
                self._store_magnetic_keyboard_options(options, store_in_selected_profile=False)
                entry = self._active_device()
                if entry is not None:
                    self._seed_uninitialized_magnetic_profiles(entry)

                def update():
                    self.magnetic_rt_stab_dropdown.value = str(options.rt_stab)
                    self.magnetic_anti_accidental_switch.value = options.anti_accidental
                    if not silent:
                        self.magnetic_status.value = "RTStab и защита от случайных нажатий обновлены."
                        self.magnetic_status.color = ft.Colors.GREEN_300
                    # This worker runs independently from selected-key edits.
                    # Patch its fixed controls only; a global page update here
                    # was able to overlap an RT visibility transition.
                    for control in (
                        self.magnetic_rt_stab_dropdown,
                        self.magnetic_anti_accidental_switch,
                        self.magnetic_status if not silent else None,
                    ):
                        if control is None:
                            continue
                        try:
                            control.update()
                        except Exception:
                            pass

                self._ui_call(update)
            except MagneticProtocolError as exc:
                if not silent:
                    self._set_magnetic_status(str(exc), ft.Colors.ERROR)
                else:
                    logger.debug("keyboard option read failed: %s", exc)

        threading.Thread(target=worker, daemon=True, name="magnetic-read-options").start()

    def _magnetic_apply_keyboard_options(self):
        try:
            rt_stab = int(self.magnetic_rt_stab_dropdown.value)
            anti_accidental = bool(self.magnetic_anti_accidental_switch.value)
        except (TypeError, ValueError) as exc:
            self._set_magnetic_status(f"Проверьте RTStab: {exc}", ft.Colors.ERROR)
            return

        def worker():
            try:
                # Preserve Fn index, OS and WASD swap even when the user did
                # not read them beforehand.
                current = MagneticProtocol.decode_keyboard_options(
                    self._query_magnetic_packet(
                        MagneticProtocol.get_keyboard_options_packet(), "magnetic_get_kboption_before_set"
                    )
                )
                packet = MagneticProtocol.keyboard_options_packet(
                    KeyboardOptions(
                        fn_index=current.fn_index,
                        anti_accidental=anti_accidental,
                        rt_stab=rt_stab,
                        wasd_swap=current.wasd_swap,
                        system=current.system,
                    )
                )
                self._send_magnetic_packets([packet], "magnetic_kboption")
                self._store_magnetic_keyboard_options(
                    KeyboardOptions(
                        fn_index=current.fn_index,
                        anti_accidental=anti_accidental,
                        rt_stab=rt_stab,
                        wasd_swap=current.wasd_swap,
                        system=current.system,
                    )
                )
                if getattr(self, "_womier_cache_sync_lock", None) is not None:
                    self._queue_womier_cache_sync(
                        self._selected_magnetic_profile_index(), rt_stab=rt_stab
                    )
                self._set_magnetic_status(
                    "RTStab применён; остальные системные параметры сохранены без изменений.",
                    ft.Colors.GREEN_300,
                )
            except MagneticProtocolError as exc:
                self._set_magnetic_status(str(exc), ft.Colors.ERROR)

        threading.Thread(target=worker, daemon=True, name="magnetic-options").start()

    def _magnetic_set_snap_pair(self, first=None, second=None):
        """Write one deliberately confirmed Snap Key pair.

        ``first`` and ``second`` are accepted explicitly so the visual dialog
        can keep its draft local and a close/cancel event cannot accidentally
        become a keyboard write.
        """
        try:
            if first is None:
                first = self.snap_first_slot
            if second is None:
                second = self.snap_second_slot
            packets = MagneticProtocol.snap_pair_packets(first, second)
        except (TypeError, ValueError, MagneticProtocolError) as exc:
            self._set_magnetic_status(f"Snap Key: {exc}", ft.Colors.ERROR)
            return

        def worker():
            try:
                self._send_magnetic_packets(packets, f"snap_{first}_{second}")
                with _CONFIG_WRITE_LOCK:
                    entry = self._active_device()
                    if entry is not None:
                        modes = entry.setdefault("magnetic_key_modes", {})
                        modes[str(first)] = MagneticProtocol.MODE_SNAP
                        modes[str(second)] = MagneticProtocol.MODE_SNAP
                        live_pairs = _safe_magnetic_snap_pairs(
                            entry.get("magnetic_snap_pairs", []), modes
                        )
                        live_pairs = [
                            pair
                            for pair in live_pairs
                            if first not in pair and second not in pair
                        ]
                        live_pairs.append([first, second])
                        entry["magnetic_snap_pairs"] = live_pairs
                        profile = self._magnetic_profile_slot()
                        if profile is not None:
                            profile_modes = profile.setdefault("key_modes", {})
                            profile_modes[str(first)] = MagneticProtocol.MODE_SNAP
                            profile_modes[str(second)] = MagneticProtocol.MODE_SNAP
                            profile_pairs = _safe_magnetic_snap_pairs(
                                profile.get("snap_pairs", []), profile_modes
                            )
                            profile_pairs = [
                                pair
                                for pair in profile_pairs
                                if first not in pair and second not in pair
                            ]
                            profile_pairs.append([first, second])
                            profile["snap_pairs"] = profile_pairs
                            profile["initialized"] = True
                if entry is not None:
                    self.save_config(reload_runtime=False)
                    if getattr(self, "_womier_cache_sync_lock", None) is not None:
                        snap_settings = {
                            slot: setting
                            for slot in (first, second)
                            if (setting := self._cached_magnetic_settings(slot)) is not None
                        }
                        self._queue_womier_cache_sync(
                            self._selected_magnetic_profile_index(),
                            key_settings=snap_settings,
                            key_modes={
                                first: MagneticProtocol.MODE_SNAP,
                                second: MagneticProtocol.MODE_SNAP,
                            },
                        )
                    self._ui_call(self._refresh_sk75_keyboard_picker)
                self._set_magnetic_status("Snap Key связан только для выбранной пары.", ft.Colors.GREEN_300)
            except MagneticProtocolError as exc:
                self._set_magnetic_status(str(exc), ft.Colors.ERROR)

        threading.Thread(target=worker, daemon=True, name="magnetic-snap-set").start()

    def _magnetic_clear_all_snap_keys(self, slots=None):
        """Clear every *known* Snap mode without resetting magnetic values.

        This is intentionally called only from the explicit confirmation in
        the Snap Key dialog.  The firmware has no bulk "reset keyboard"
        command here: it receives just the mode and partner bytes for slots
        which were actually read/stored as Snap Key.
        """
        known_slots = set(self._known_snap_key_slots())
        if slots is None:
            slots = known_slots
        else:
            slots = {
                slot for slot in slots
                if isinstance(slot, int) and slot in known_slots
            }
        slots = sorted(slots)
        if not slots:
            self._set_magnetic_status("Snap Key уже не активен — очищать нечего.", ft.Colors.ON_SURFACE_VARIANT)
            self._ui_call(self._refresh_sk75_keyboard_picker)
            return
        try:
            # Restoring mode=normal must retain RT where it was enabled before
            # pairing.  The cached per-key values are the only reliable source
            # because Snap mode itself intentionally replaces the mode byte.
            rapid_trigger_slots = [
                slot
                for slot in slots
                if (settings := self._cached_magnetic_settings(slot)) is not None
                and settings.rapid_trigger
            ]
            packets = MagneticProtocol.clear_snap_slots_packets(
                slots,
                rapid_trigger_slots=rapid_trigger_slots,
            )
        except (TypeError, ValueError, MagneticProtocolError) as exc:
            self._set_magnetic_status(f"Snap Key: {exc}", ft.Colors.ERROR)
            return

        def worker():
            try:
                self._send_magnetic_packets(packets, "snap_clear_all")
                with _CONFIG_WRITE_LOCK:
                    entry = self._active_device()
                    if entry is not None:
                        self._clear_snap_modes_from_entry(entry, slots)
                        profiles = entry.get("magnetic_profiles")
                        if isinstance(profiles, dict):
                            for profile in list(profiles.values()):
                                if isinstance(profile, dict):
                                    profile["initialized"] = True
                if entry is not None:
                    self.save_config(reload_runtime=False)
                    if getattr(self, "_womier_cache_sync_lock", None) is not None:
                        normal_settings = {
                            slot: setting
                            for slot in slots
                            if (setting := self._cached_magnetic_settings(slot)) is not None
                        }
                        normal_modes = {
                            slot: MagneticProtocol.MODE_NORMAL
                            | (
                                MagneticProtocol.MODE_RAPID_TRIGGER_BIT
                                if setting.rapid_trigger
                                else 0
                            )
                            for slot, setting in normal_settings.items()
                        }
                        self._queue_womier_cache_sync(
                            self._selected_magnetic_profile_index(),
                            key_settings=normal_settings,
                            key_modes=normal_modes,
                        )
                self.snap_first_slot = None
                self.snap_second_slot = None
                self._ui_call(self._refresh_sk75_keyboard_picker)
                # The cleared amber markers are sufficient confirmation.
                # Keeping a long technical message under the controls made
                # the magnetic screen look as if it still had a pending task.
                self._set_magnetic_status("")
            except MagneticProtocolError as exc:
                self._set_magnetic_status(str(exc), ft.Colors.ERROR)

        threading.Thread(target=worker, daemon=True, name="magnetic-snap-clear-all").start()

    # ---------- Devices ----------
    # VID/usage_page всех клавиатур, настраиваемых через qmk.top.
    # PID отличается у разных моделей, VID и Page — общие.
    QMK_TOP_VID = 0x3151
    QMK_TOP_USAGE_PAGE = 0xFFFF

    def refresh_devices(self):
        self.devices = hid.enumerate()
        custom_devices = [
            d for d in self.devices
            if d['vendor_id'] == self.QMK_TOP_VID and d['usage_page'] == self.QMK_TOP_USAGE_PAGE
        ]
        seen = set()
        deduped = []
        for d in custom_devices:
            # Дедуп по тому же ключу, что использует _device_key_of (VID:PID:usage_page).
            # Раньше ключ включал ещё `usage`, и устройства с одинаковым usage_page,
            # но разными usage (часто 0x00 + 0x01 на одной клавиатуре) попадали как
            # два отдельных пункта дропдауна с ОДИНАКОВЫМ key — выглядели дубликатами.
            key = (d['vendor_id'], d['product_id'], d['usage_page'])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(d)
        custom_devices = deduped
        self.filtered_devices = custom_devices

        # Make sure every present device has a config entry BEFORE we probe
        # battery — the probe needs the saved query/parse params.
        for d in self.filtered_devices:
            self._ensure_device_entry(d)

        # Auto-detect transport by device name without an override.
        cfg_dirty = False
        for d in self.filtered_devices:
            key = self._device_key_of(d)
            entry = self.config["devices"].get(key)
            if entry is None:
                continue
            new_transport = self._detect_transport(d)
            if entry.get("transport") != new_transport:
                entry["transport"] = new_transport
                cfg_dirty = True
        if cfg_dirty:
            self.save_config()

        custom_devices = self.filtered_devices

        options = []
        for i, d in enumerate(custom_devices):
            key = self._device_key_of(d)
            saved = self.config["devices"].get(key)
            label_prefix = (saved.get("label") or self._device_label_for(d)) if saved else self._device_label_for(d)
            transport = (saved or {}).get("transport") or self._detect_transport(d)
            badge = "[WIRED]" if transport == "wired" else "[WIRELESS]"
            label_text = (
                f"{badge} {label_prefix} · VID {hex(d['vendor_id'])} · PID {hex(d['product_id'])} · Page {hex(d['usage_page'])}"
            )
            vid, pid, up = d['vendor_id'], d['product_id'], d['usage_page']
            row = ft.Row(
                [
                    ft.Text(label_text, expand=True, overflow=ft.TextOverflow.ELLIPSIS),
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            options.append(ft.dropdown.Option(key=key, text=label_text, content=row))
        self.device_dropdown.options = options

        present_keys = [self._device_key_of(d) for d in self.filtered_devices]
        active_key = self.config.get("active_device")
        target_key = self._pick_active_target(active_key, present_keys, self.config["devices"])
        self.device_dropdown.value = target_key
        if target_key and target_key != active_key:
            self._activate_device(target_key)
        elif target_key:
            entry = self.config["devices"].get(target_key)
            if entry and entry.get("keyboard_type") is None:
                self._show_setup_wizard(target_key)
        try:
            self._update_transport_icon()
        except Exception:
            pass
        self.page.update()

    def _on_device_dropdown_changed(self):
        key = self.device_dropdown.value
        if not key:
            return
        hid_dev = next(
            (d for d in self.filtered_devices if self._device_key_of(d) == key),
            None,
        )
        # Profiles are static defaults — any selected device "just works".
        # Auto-create the config entry for unknown devices and activate silently.
        if key not in self.config["devices"]:
            if hid_dev is None:
                return
            self._ensure_device_entry(hid_dev)
        if key != self.config.get("active_device"):
            self._activate_device(key)

        self.page.update()

    def open_sniffer_modal(self):
        self._sniff_auto_scroll = True

        def on_close(e):
            try:
                self.page.pop_dialog()
            except Exception:
                pass

        battery_panel = self._build_battery_test_panel()
        self._battery_test_sync_from_active()

        self._sniff_scroll_btn = ft.IconButton(
            icon=ft.Icons.ARROW_DOWNWARD_ROUNDED,
            tooltip="Прокрутить вниз (авто-скролл)",
            icon_color=ft.Colors.PRIMARY,
            on_click=self._sniff_scroll_to_bottom,
        )

        body = ft.Column(
            [
                ft.Row(
                    [self.sniff_button, self.sniff_clear_button, self.sniff_copy_button,
                     self._sniff_scroll_btn],
                    spacing=8, wrap=True,
                ),
                ft.Row(
                    [self.browser_pick_button, self.browser_path_text],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    [self.sniff_learn_switch],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                battery_panel,
                self.sniff_status,
                ft.Container(
                    content=self.sniff_log,
                    bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                    border_radius=12,
                    padding=4,
                    expand=True,
                ),
            ],
            spacing=10,
            expand=True,
        )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("HID Sniffer — qmk.top"),
            content=ft.Container(
                content=body,
                expand=True,
            ),
            actions=[
                ft.TextButton("Закрыть", on_click=on_close),
            ],
            shape=ft.RoundedRectangleBorder(radius=20),
        )
        self.page.show_dialog(dlg)
    def open_profile_dialog(self, index):
        info = self._profile_info_at(index) or {}
        current_name = self._profile_name_at(index) or f"Профиль {index + 1}"

        name_field = ft.TextField(
            label="Название профиля",
            hint_text="Например: Gaming, Typing",
            value=current_name,
            border_radius=12,
            filled=True,
        )

        entry = self._active_device()
        kb_type = entry.get("keyboard_type") if entry else None
        caps = device_capabilities(kb_type)

        polling_dropdown = None
        lighting_dropdown = None
        if DeviceCapability.POLLING_RATE in caps:
            current_pr = info.get("polling_rate")
            pr_options = [ft.dropdown.Option(key="none", text="Не менять")]
            for r in PollingRate:
                pr_options.append(ft.dropdown.Option(key=str(r.value), text=f"{r.value} Hz"))
            polling_dropdown = self._app_dropdown(
                label="Polling Rate",
                options=pr_options,
                value=str(current_pr) if current_pr and current_pr in VALID_POLLING_RATES else "none",
                border_radius=12,
                filled=True,
            )

        if DeviceCapability.LIGHTING_PROFILES in caps:
            current_lp = info.get("lighting_profile")
            lp_options = [ft.dropdown.Option(key="none", text="Не менять")]
            for i in range(LIGHTING_PROFILE_COUNT):
                lp_options.append(ft.dropdown.Option(key=str(i), text=f"Подсветка {i + 1}"))
            lighting_dropdown = self._app_dropdown(
                label="Профиль подсветки",
                options=lp_options,
                value=str(current_lp) if current_lp is not None and current_lp in VALID_LIGHTING_PROFILES else "none",
                border_radius=12,
                filled=True,
            )

        dialog_fields = [name_field]
        if polling_dropdown:
            dialog_fields.append(polling_dropdown)
        if lighting_dropdown:
            dialog_fields.append(lighting_dropdown)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Профиль {index + 1}"),
            content=ft.Container(
                content=ft.Column(
                    dialog_fields,
                    spacing=12,
                    tight=True,
                ),
                width=440,
            ),
            shape=ft.RoundedRectangleBorder(radius=24),
        )

        def on_cancel(e):
            self.page.pop_dialog()

        def on_save(e):
            new_name = name_field.value.strip()
            if not new_name:
                self._snack("Имя профиля обязательно")
                return
            if not self._rename_profile_at(index, new_name):
                self._snack("Имя занято другим профилем")
                return
            self.config["payloads"][new_name].pop("hotkey", None)
            if polling_dropdown:
                pr_val = polling_dropdown.value
                if pr_val and pr_val != "none":
                    self.config["payloads"][new_name]["polling_rate"] = int(pr_val)
                else:
                    self.config["payloads"][new_name].pop("polling_rate", None)
            if lighting_dropdown:
                lp_val = lighting_dropdown.value
                if lp_val and lp_val != "none":
                    self.config["payloads"][new_name]["lighting_profile"] = int(lp_val)
                else:
                    self.config["payloads"][new_name].pop("lighting_profile", None)
            self.save_config()
            self.update_payloads_list()
            self.update_bindings_list()
            self.page.pop_dialog()

        dlg.actions = [
            ft.TextButton("Отмена", on_click=on_cancel),
            ft.FilledButton("Сохранить", on_click=on_save),
        ]
        self.page.show_dialog(dlg)

    def update_payloads_list(self):
        self.payloads_column.controls.clear()
        items = self._profile_items()
        entry = self._active_device()
        kb_type = entry.get("keyboard_type") if entry else None
        caps = device_capabilities(kb_type)
        for index in range(self._device_profile_count()):
            name, info = items[index] if index < len(items) else (f"Профиль {index + 1}", {})
            pr = info.get("polling_rate") if DeviceCapability.POLLING_RATE in caps else None
            data = self._profile_payload_at(index)
            preview = ", ".join(hex(b) for b in data[:4]) + ("…" if len(data) > 4 else "")

            subtitle_parts = [ft.Text(preview, size=11,
                                       color=ft.Colors.ON_SURFACE_VARIANT,
                                       font_family="Consolas")]
            if pr and pr in VALID_POLLING_RATES:
                subtitle_parts.append(ft.Container(
                    content=ft.Text(f"{pr} Hz", size=10, weight=ft.FontWeight.W_500,
                                    color=ft.Colors.ON_SECONDARY_CONTAINER),
                    padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                    bgcolor=ft.Colors.SECONDARY_CONTAINER,
                    border_radius=100,
                ))

            lp = info.get("lighting_profile") if DeviceCapability.LIGHTING_PROFILES in caps else None
            if lp is not None and lp in VALID_LIGHTING_PROFILES:
                subtitle_parts.append(ft.Container(
                    content=ft.Text(f"💡 {lp + 1}", size=10, weight=ft.FontWeight.W_500,
                                    color=ft.Colors.ON_TERTIARY_CONTAINER),
                    padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                    bgcolor=ft.Colors.TERTIARY_CONTAINER,
                    border_radius=100,
                ))

            row = ft.Container(
                content=ft.Row(
                    [
                        ft.Row(
                            [
                                ft.Container(
                                    content=ft.Text(str(index + 1), size=14, weight=ft.FontWeight.W_700,
                                                    color=ft.Colors.ON_PRIMARY_CONTAINER),
                                    width=30, height=30,
                                    bgcolor=ft.Colors.PRIMARY_CONTAINER,
                                    border_radius=10,
                                    alignment=ft.Alignment.CENTER,
                                ),
                                ft.Column(
                                    [
                                        ft.Text(name, size=14, weight=ft.FontWeight.W_600),
                                        ft.Row(subtitle_parts, spacing=6),
                                    ],
                                    spacing=0,
                                    tight=True,
                                ),
                            ],
                            spacing=12,
                            expand=True,
                        ),
                        ft.Row(
                            [
                                ft.Checkbox(
                                    value=(self.default_profile_index == index),
                                    tooltip="Профиль по умолчанию (когда нет совпадений по активному окну)",
                                    on_change=lambda e, i=index: self._set_default_profile(i, e.control.value),
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.EDIT_ROUNDED,
                                    tooltip="Изменить профиль",
                                    icon_size=18,
                                    on_click=lambda e, i=index: self.open_profile_dialog(i),
                                ),
                            ],
                            spacing=4,
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                border_radius=14,
            )
            self.payloads_column.controls.append(row)
        # Profile names are also shown by the local magnetic-preset selector.
        # Refresh its option text after a rename without changing selection.
        self._refresh_magnetic_profile_dropdown()
        self.page.update()

    def _set_default_profile(self, index: int, checked: bool) -> None:
        entry = self._active_device()
        if entry is None:
            return
        entry["default_profile_index"] = index if checked else None
        self.save_config()
        self.reload_runtime_state()
        self.update_payloads_list()

    # ---------- Bindings ----------
    def open_binding_dialog(self, title, edit_idx=None):
        items = self._profile_items()
        if not items:
            self._snack("Сначала создайте хотя бы один профиль")
            return

        b_data = self.config["bindings"][edit_idx] if edit_idx is not None else None
        current_pi = b_data.get("profile_index", 0) if b_data else 0
        if not (0 <= current_pi < len(items)):
            current_pi = 0

        proc_field = ft.TextField(
            label="Процесс",
            hint_text="например, cs2.exe",
            value=b_data["process"] if b_data else "",
            border_radius=12,
            filled=True,
        )
        prof_dropdown = self._app_dropdown(
            label="Профиль",
            options=[
                ft.dropdown.Option(key=str(i), text=f"{i + 1}. {name}")
                for i, (name, _) in enumerate(items)
            ],
            value=str(current_pi),
            border_radius=12,
            filled=True,
        )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(title),
            content=ft.Container(
                content=ft.Column([proc_field, prof_dropdown], spacing=12, tight=True),
                width=440,
            ),
            shape=ft.RoundedRectangleBorder(radius=24),
        )

        def on_cancel(e):
            self.page.pop_dialog()

        def on_save(e):
            proc = proc_field.value.strip().lower()
            if not proc or prof_dropdown.value is None:
                self._snack("Процесс и профиль обязательны")
                return
            try:
                pi = int(prof_dropdown.value)
            except Exception:
                self._snack("Некорректный профиль")
                return
            new_bind = {"process": proc, "profile_index": pi}
            if edit_idx is not None:
                self.config["bindings"][edit_idx] = new_bind
            else:
                self.config["bindings"].append(new_bind)
            self.save_config()
            self.update_bindings_list()
            self.page.pop_dialog()

        dlg.actions = [
            ft.TextButton("Отмена", on_click=on_cancel),
            ft.FilledButton("Сохранить", on_click=on_save),
        ]
        self.page.show_dialog(dlg)

    def delete_binding(self, idx):
        del self.config["bindings"][idx]
        self.save_config()
        self.update_bindings_list()

    def _on_rule_toggle(self, idx: int, enabled: bool):
        bindings = self.config.get("bindings", [])
        if 0 <= idx < len(bindings):
            bindings[idx]["enabled"] = enabled
            process = bindings[idx].get("process", "?")
            self.rule_evaluator.set_enabled(process, enabled)
            logger.debug("rule toggle: process=%s enabled=%s", process, enabled)
            self.save_config()
            self.update_bindings_list()

    def _refresh_bindings_summary(self):
        """Refresh the compact binding facts shown beside the rules list."""
        bindings = [
            binding
            for binding in (self.config.get("bindings") or [])
            if isinstance(binding, dict) and str(binding.get("process", "")).strip()
        ]
        total = len(bindings)
        enabled = sum(1 for binding in bindings if binding.get("enabled", True) is not False)
        disabled = total - enabled
        try:
            profile_count = len(self._profile_items())
        except Exception:
            profile_count = 0

        self.bindings_total_value.value = str(total)
        self.bindings_enabled_value.value = str(enabled)
        self.bindings_profiles_value.value = str(profile_count)
        if not total:
            self.bindings_summary_status.value = (
                "Правил пока нет. Создайте первое — переключение начнётся автоматически."
            )
        elif not disabled:
            self.bindings_summary_status.value = (
                f"Настроено: {total}. Все привязки включены и готовы к работе."
            )
        else:
            self.bindings_summary_status.value = (
                f"Активны: {enabled}; выключены: {disabled}. Их можно включить тумблером слева."
            )

    def _binding_search_query(self) -> str:
        """Return the current bindings filter without coupling it to config."""
        field = getattr(self, "binding_search_field", None)
        return str(getattr(field, "value", "") or "").strip().casefold()

    def _sync_binding_search_clear_button(self):
        """Keep the bare in-field clear affordance visible at all times."""
        button = getattr(self, "binding_search_clear_button", None)
        if button is None:
            return
        # Clearing an empty field is intentionally harmless, so never turn the
        # icon into a dimmed/disabled control.  This also preserves its fixed
        # position at the right edge of the search field while the user types.
        button.disabled = False
        try:
            button.update()
        except Exception:
            # The initial TextField construction runs before the controls are
            # mounted.  Retaining the property is sufficient for first paint.
            pass

    def _on_binding_search_changed(self, _event=None):
        """Filter locally as the user types; no profile/rule state changes."""
        self._sync_binding_search_clear_button()
        self.update_bindings_list()

    def _clear_binding_search(self, _event=None):
        """Clear the process/profile query and immediately restore all rows."""
        field = getattr(self, "binding_search_field", None)
        if field is None:
            return
        field.value = ""
        self._sync_binding_search_clear_button()
        try:
            field.update()
        except Exception:
            pass
        self.update_bindings_list()

    def _filtered_binding_items(self):
        """Return (original_index, binding) pairs matching the visible query."""
        query = self._binding_search_query()
        result = []
        for index, binding in enumerate(self.config.get("bindings") or []):
            if not isinstance(binding, dict):
                continue
            process = str(binding.get("process", ""))
            profile_index = binding.get("profile_index", 0)
            profile_name = self._profile_name_at(profile_index) or f"Профиль {profile_index + 1}"
            if not query or query in process.casefold() or query in profile_name.casefold():
                result.append((index, binding))
        return result

    def update_bindings_list(self):
        self.bindings_column.controls.clear()
        bindings = self.config.get("bindings") or []
        matching_bindings = self._filtered_binding_items()
        if not bindings:
            self.bindings_column.controls.append(
                ft.Container(
                    content=ft.Text(
                        "Привязок пока нет. Создайте правило для автоматического переключения.",
                        color=ft.Colors.ON_SURFACE_VARIANT,
                        size=13,
                    ),
                    padding=16,
                    alignment=ft.Alignment.CENTER,
                )
            )
        elif not matching_bindings:
            query = self._binding_search_query()
            self.bindings_column.controls.append(
                ft.Container(
                    content=ft.Text(
                        f"По запросу «{query}» привязок не найдено.",
                        color=ft.Colors.ON_SURFACE_VARIANT,
                        size=13,
                    ),
                    padding=16,
                    alignment=ft.Alignment.CENTER,
                )
            )
        else:
            for i, b in matching_bindings:
                pi = b.get("profile_index", 0)
                pname = self._profile_name_at(pi) or f"Профиль {pi + 1}"
                enabled = b.get("enabled", True)

                toggle = ft.Switch(
                    value=enabled,
                    on_change=lambda e, idx=i: self._on_rule_toggle(idx, e.control.value),
                )

                row = ft.Container(
                    content=ft.Row(
                        [
                            toggle,
                            ft.Row(
                                [
                                    ft.Icon(ft.Icons.APPS_ROUNDED,
                                            color=ft.Colors.TERTIARY, size=20),
                                    ft.Text(
                                        b["process"],
                                        size=14,
                                        weight=ft.FontWeight.W_500,
                                        font_family="Consolas",
                                        width=235,
                                        no_wrap=True,
                                        overflow=ft.TextOverflow.ELLIPSIS,
                                    ),
                                    ft.Icon(ft.Icons.ARROW_FORWARD_ROUNDED, size=16,
                                            color=ft.Colors.ON_SURFACE_VARIANT),
                                    ft.Container(
                                        content=ft.Text(
                                            f"{pi + 1}. {pname}",
                                            size=12,
                                            weight=ft.FontWeight.W_500,
                                            width=150,
                                            no_wrap=True,
                                            overflow=ft.TextOverflow.ELLIPSIS,
                                        ),
                                        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                                        bgcolor=ft.Colors.PRIMARY_CONTAINER,
                                        border_radius=100,
                                    ),
                                ],
                                spacing=8,
                                tight=True,
                            ),
                            ft.Row(
                                [
                                    ft.IconButton(
                                        icon=ft.Icons.EDIT_ROUNDED,
                                        tooltip="Редактировать",
                                        icon_size=18,
                                        width=34,
                                        height=34,
                                        padding=6,
                                        on_click=lambda e, idx=i: self.open_binding_dialog("Редактировать", idx),
                                    ),
                                    ft.IconButton(
                                        icon=ft.Icons.DELETE_ROUNDED,
                                        tooltip="Удалить",
                                        icon_size=18,
                                        width=34,
                                        height=34,
                                        padding=6,
                                        icon_color=ft.Colors.ERROR,
                                        on_click=lambda e, idx=i: self.delete_binding(idx),
                                    ),
                                ],
                                spacing=2,
                                tight=True,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.START,
                        spacing=10,
                        tight=True,
                    ),
                    width=getattr(self, "bindings_list_width", 690),
                    padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                    bgcolor=ft.Colors.SURFACE_CONTAINER,
                    border_radius=14,
                    opacity=1.0 if enabled else 0.5,
                )
                self.bindings_column.controls.append(row)
        self._refresh_bindings_summary()
        self.page.update()

    # ---------- Sniffer ----------
    def _resolve_browser_path(self):
        saved = (self.config.get("settings", {}).get("browser_path") or "").strip()
        if saved and os.path.isfile(saved):
            return saved
        return self.detected_browser_path

    def _browser_label(self):
        path = self._resolve_browser_path()
        if not path:
            return "Браузер не найден — укажи путь к chrome.exe / msedge.exe вручную."
        saved = (self.config.get("settings", {}).get("browser_path") or "").strip()
        prefix = "Указан вручную" if saved and saved == path else "Найден"
        return f"{prefix}: {path}"

    def _open_browser_picker(self):
        async def _pick():
            files = await self.browser_picker.pick_files(
                dialog_title="Выбери исполняемый файл браузера (Chrome / Edge / Brave / Vivaldi)",
                allow_multiple=False,
                allowed_extensions=["exe"],
            )
            self._handle_browser_pick(files)
        self.page.run_task(_pick)

    def _handle_browser_pick(self, files):
        if not files:
            return
        path = files[0].path
        if not is_chromium_executable(path):
            self.sniff_status.value = "Файл не похож на Chromium-браузер. Жду chrome.exe / msedge.exe / brave.exe и т.п."
            self.page.update()
            return
        self.config.setdefault("settings", {})["browser_path"] = path
        self.save_config()
        self.browser_path_text.value = self._browser_label()
        self.sniff_status.value = "Браузер сохранён. Можно запускать sniff."
        self.page.update()

    def toggle_sniffer(self):
        if self.sniffer is None:
            if OFFLINE_MODE:
                self.sniff_status.value = "Sniffer disabled in offline mode. Enable it only when you need a one-time capture."
                self.page.update()
                return
            browser = self._resolve_browser_path()
            if not browser:
                self.sniff_status.value = "Браузер не выбран. Жми «Указать браузер…» и выбери chrome.exe / msedge.exe."
                self.page.update()
                self._open_browser_picker()
                return
            try:
                self.sniffer = HIDSniffer(
                    on_event=self._on_sniff_event,
                    on_status=self._on_sniff_status,
                    browser_path=browser,
                    offline_mode=False,
                )
                self._battery_captured_this_session = False
                self._battery_capture_attempts = 0
                self._battery_locked = False
                self._captured_profile_indices = set()
                self.sniffer.start()
                self.sniff_button.text = "Остановить sniff"
                self.sniff_button.icon = ft.Icons.STOP_ROUNDED
            except Exception as ex:
                self.sniffer = None
                self.sniff_status.value = f"Ошибка: {ex}"
            self.page.update()
            return
        try:
            self.sniffer.stop()
        except Exception:
            pass
        self.sniffer = None
        self.sniff_button.text = "Запустить sniff"
        self.sniff_button.icon = ft.Icons.SENSORS_ROUNDED
        self.page.update()

    def _maybe_check_updates(self):
        if not ENABLE_UPDATE_CHECK:
            self.update_check_state = _load_local_update_state()
            return
        self.update_check_state = {
            "enabled": True,
            "checked_at": None,
            "latest_version": None,
            "error": "Update check hook not wired yet.",
        }

    def _on_sniff_status(self, msg: str):
        def upd():
            self.sniff_status.value = msg
            self.page.update()
        try:
            self.page.run_thread(upd)
        except Exception:
            upd()

    def _toggle_learn_mode(self, e):
        self._sniff_learn_mode = bool(e.control.value)
        logger.debug("learn_mode toggled: %s", self._sniff_learn_mode)
        if self._sniff_learn_mode:
            self._start_battery_probe_worker()
        else:
            self._stop_battery_probe_worker()

    def _sniff_scroll_to_bottom(self, e=None):
        self._sniff_auto_scroll = True
        try:
            self.sniff_log.scroll_to(offset=-1, duration=100)
            self.page.update()
        except Exception:
            pass

    def _on_sniff_scroll(self, e: ft.OnScrollEvent):
        if e.event_type == "user":
            self._sniff_auto_scroll = False

    def _start_battery_probe_worker(self):
        entry = self._active_device()
        batt = entry.get("battery") if entry else None
        if not batt or not batt.get("response_offset"):
            logger.debug("battery probe worker not started: no battery config")
            return
        self._battery_probe_stop.clear()
        while not self._battery_probe_queue.empty():
            try:
                self._battery_probe_queue.get_nowait()
            except queue.Empty:
                break
        self._battery_probe_thread = threading.Thread(
            target=self._battery_probe_worker, daemon=True)
        self._battery_probe_thread.start()
        logger.debug("battery probe worker started")

    def _stop_battery_probe_worker(self):
        self._battery_probe_stop.set()
        while not self._battery_probe_queue.empty():
            try:
                self._battery_probe_queue.get_nowait()
            except queue.Empty:
                break
        self._battery_probe_thread = None
        logger.debug("battery probe worker stopped")

    def _battery_probe_worker(self):
        logger.debug("battery probe worker thread running")
        while not self._battery_probe_stop.is_set():
            try:
                item = self._battery_probe_queue.get(timeout=1)
            except queue.Empty:
                continue
            packet_data, result_text = item
            path = self.get_keyboard_path_safe()
            if path is None:
                logger.debug("battery probe: no device path")
                continue
            time.sleep(0.2)
            if self._battery_probe_stop.is_set():
                break
            percent = self.battery_monitor.probe_battery(packet_data, path)
            if percent is not None:
                txt = f"🔋 {percent}%"
                color = ft.Colors.GREEN_400
            else:
                txt = "—"
                color = ft.Colors.GREY_500
            logger.debug("battery probe result: packet=%s → %s",
                         [f"0x{b:02x}" for b in packet_data[:4]], txt)
            def upd(t=txt, c=color, rt=result_text):
                try:
                    rt.value = t
                    rt.color = c
                    self.page.update()
                except Exception:
                    pass
            self._ui_call(upd)
        logger.debug("battery probe worker thread exiting")

    def _append_sniff_event(self, event: object) -> dict:
        """Store one bounded event and return the detached UI snapshot."""
        snapshot = _bounded_sniff_event_snapshot(event)
        with self._sniff_events_lock:
            self.sniff_events.append(snapshot)
        return snapshot

    def _sniff_events_snapshot(self) -> list[dict]:
        """Copy a stable, bounded journal for clipboard/export consumers."""
        with self._sniff_events_lock:
            return [_bounded_sniff_event_snapshot(event) for event in self.sniff_events]

    def _on_sniff_event(self, ev: dict):
        # Do not let browser-provided nested values remain in the application
        # object or race a clipboard iteration.  All later pattern matching
        # uses the validated byte list from this detached snapshot.
        ev = self._append_sniff_event(ev)
        data = ev.get("data") or []
        if not data:
            return
        direction = (ev.get("dir") or "").upper()
        ev_type = (ev.get("type") or "").lower()

        if direction != "TX":
            return
        logger.debug("sniff TX event type=%s reportId=%s data=%s",
                      ev.get("type"), ev.get("reportId"),
                      [f"0x{b:02x}" for b in data[:8]])

        is_profile = self._matches_profile_pattern(data)
        ev_type = (ev.get("type") or "").lower()
        if is_profile:
            logger.debug("sniff: PROFILE detected reportId=%s ev_type=%s full_data=%s",
                          ev.get("reportId"), ev_type,
                          [f"0x{b:02x}" for b in data])
        is_battery = (
            ev_type == "feature"
            and not is_profile
            and self._matches_battery_pattern(data, ev_type)
        )

        # Learn mode: log EVERY TX frame with a classification tag, and
        # bypass auto-save (user must click "Сохранить" explicitly).
        if self._sniff_learn_mode:
            if is_profile:
                tag, color = "PROFILE?", ft.Colors.AMBER_400
            elif self._matches_polling_rate_pattern(data):
                tag, color = "POLL_RATE?", ft.Colors.GREEN_300
            elif self._matches_lighting_profile_pattern(data):
                tag, color = "LIGHTING?", ft.Colors.PURPLE_300
            elif ev_type == "feature":
                tag, color = "BATTERY?", ft.Colors.LIGHT_BLUE_300
            else:
                tag, color = "TX", ft.Colors.GREY_500
            self._render_sniff_row(tag, color, data, ev.get("reportId"), ev_type, ev)
            return

        if not (is_profile or is_battery):
            return

        # Ensure capture lands on an active device — if user opened sniffer
        # without picking a device first, we can't autofill anywhere.
        if self._active_device() is None:
            self._on_sniff_status("Нет активного устройства — выбери клавиатуру в списке.")
            return

        if is_battery:
            if self._battery_locked:
                return
            try:
                self.config["battery"]["query"] = list(data)
                rid = ev.get("reportId")
                if isinstance(rid, int):
                    self.config["battery"]["report_id"] = rid
                self.save_config()
            except Exception:
                pass
            self._battery_capture_attempts += 1
            self._battery_captured_this_session = True
            self._battery_locked = True
            self._on_sniff_status(
                "Battery: query захвачен. Закрой браузер, и через ≤60с (или по кнопке refresh) "
                "процент появится в шапке и в трее."
            )

        # Profiles are NOT auto-saved anymore — defaults work for everyone, and
        # users explicitly opt in via the per-row "Сохранить" button below.
        slot_idx = data[1] if (is_profile and len(data) > 1 and data[1] in (0, 1, 2, 3)) else None

        hex_str = " ".join(f"{b:02X}" for b in data[:64])
        if len(data) > 64:
            hex_str += f" …+{len(data) - 64}"
        type_ = ev.get("type") or ""
        rid = ev.get("reportId")
        idx = len(self.sniff_events)

        if is_profile:
            tag_text, tag_color = "PROFILE", ft.Colors.AMBER
        else:
            tag_text, tag_color = "BATTERY", ft.Colors.LIGHT_BLUE_ACCENT

        controls = [
            ft.Text(f"#{idx}", size=11, color=ft.Colors.ON_SURFACE_VARIANT, width=40),
            ft.Container(
                content=ft.Text(tag_text, size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK),
                bgcolor=tag_color, padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                border_radius=6,
            ),
            ft.Text(f"{type_} id={rid}", size=11, color=ft.Colors.ON_SURFACE_VARIANT, width=110),
            ft.Text(hex_str, size=10, selectable=True, font_family="Consolas",
                    no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
        ]
        if is_profile:
            label_text = f"→ слот {slot_idx + 1}" if slot_idx is not None else "→ нераспознанный слот"
            controls.append(
                ft.Container(
                    content=ft.Text(label_text, size=10, color=ft.Colors.ON_SURFACE_VARIANT, italic=True),
                    padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                )
            )
            if slot_idx is not None:
                captured = list(data)
                save_btn = ft.FilledTonalButton(
                    "Сохранить в конфиг",
                    icon=ft.Icons.SAVE_ROUNDED,
                    on_click=lambda e, i=slot_idx, p=captured: self._save_profile_payload_from_sniff(i, p),
                    style=ft.ButtonStyle(
                        shape=ft.RoundedRectangleBorder(radius=10),
                        padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                        text_style=ft.TextStyle(size=11, weight=ft.FontWeight.W_500),
                    ),
                )
                controls.append(save_btn)
        else:
            battery_chip_text = ft.Text("…", size=11, weight=ft.FontWeight.W_600, color=ft.Colors.BLACK)
            battery_chip = ft.Container(
                content=ft.Row(
                    [
                        ft.Icon(ft.Icons.BATTERY_FULL_ROUNDED, size=14, color=ft.Colors.BLACK),
                        battery_chip_text,
                    ],
                    spacing=4, tight=True,
                ),
                bgcolor=ft.Colors.LIGHT_BLUE_ACCENT,
                padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                border_radius=100,
            )
            controls.append(battery_chip)
            self._sniff_battery_chip = battery_chip_text
            threading.Thread(target=self._refresh_battery_for_sniff_chip, daemon=True).start()

        # NOTE: Task 8 will plug action buttons (e.g. "Try as profile", "Try as
        # battery") into the learn-mode row rendered by _render_sniff_row above.
        line = ft.Container(
            content=ft.Row(
                controls,
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border_radius=10,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        )
        def upd():
            self.sniff_log.controls.append(line)
            if len(self.sniff_log.controls) > 500:
                self.sniff_log.controls = self.sniff_log.controls[-500:]
            if self._sniff_auto_scroll:
                self.sniff_log.scroll_to(offset=-1, duration=100)
            self.page.update()
        try:
            self.page.run_thread(upd)
        except Exception:
            upd()

    def _render_sniff_row(self, tag, color, data, report_id, ev_type, payload):
        """Render a single sniffer log row (learn mode) with auto battery probe."""
        hex_str = " ".join(f"{b:02X}" for b in data[:64])
        if len(data) > 64:
            hex_str += f" …+{len(data) - 64}"

        extra_info = ""
        if tag == "POLL_RATE?" and len(data) >= 3:
            code_to_hz = {v: k.value for k, v in POLLING_RATE_CODES.items()}
            hz = code_to_hz.get(data[2])
            if hz:
                extra_info = f" → {hz} Hz"
        elif tag == "RX" and len(data) >= 2 and all(b == data[0] and b2 == data[1] for b, b2 in zip(data[0::2], data[1::2])):
            val = data[0] | (data[1] << 8)
            if val > 0:
                extra_info = f" → repeated {val}"

        idx = len(self.sniff_events)
        battery_result_text = ft.Text("", size=11, weight=ft.FontWeight.W_600, width=80)
        controls = [
            ft.Text(f"#{idx}", size=11, color=ft.Colors.ON_SURFACE_VARIANT, width=40),
            ft.Container(
                content=ft.Text(tag, size=10, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK),
                bgcolor=color, padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                border_radius=6,
            ),
            ft.Text(f"{ev_type} id={report_id}", size=11, color=ft.Colors.ON_SURFACE_VARIANT, width=110),
            ft.Text(hex_str + extra_info, size=10, selectable=True, font_family="Consolas",
                    no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
            battery_result_text,
        ]
        slot_btn = ft.PopupMenuButton(
            content=ft.Container(
                content=ft.Row(
                    [ft.Text("В слот"), ft.Icon(ft.Icons.ARROW_DROP_DOWN, size=16)],
                    spacing=2,
                ),
                padding=ft.Padding.symmetric(horizontal=8, vertical=4),
                border=ft.Border.all(1, ft.Colors.OUTLINE),
                border_radius=4,
            ),
            items=[
                ft.PopupMenuItem(
                    content=f"Профиль {i + 1}",
                    on_click=lambda e, idx=i, d=list(data): self._save_profile_payload_from_sniff(idx, d),
                )
                for i in range(4)
            ],
        )
        batt_btn = ft.OutlinedButton(
            "Как battery query",
            on_click=lambda e, d=list(data), rid=report_id: self._save_battery_query_from_sniff(d, rid),
        )
        controls.append(slot_btn)
        controls.append(batt_btn)
        line = ft.Container(
            content=ft.Row(
                controls,
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border_radius=10,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        )
        def upd():
            self.sniff_log.controls.append(line)
            if len(self.sniff_log.controls) > 500:
                self.sniff_log.controls = self.sniff_log.controls[-500:]
            if self._sniff_auto_scroll:
                self.sniff_log.scroll_to(offset=-1, duration=100)
            self.page.update()
        try:
            self.page.run_thread(upd)
        except Exception:
            upd()
        if (self._sniff_learn_mode
                and ev_type == "feature"
                and self._battery_probe_thread is not None
                and not self._battery_probe_stop.is_set()):
            self._battery_probe_queue.put((list(data), battery_result_text))

    @staticmethod
    def _matches_polling_rate_pattern(data: list) -> bool:
        if len(data) < 8:
            return False
        if data[0] != 0x03 or data[1] != 0x00:
            return False
        if data[2] not in range(7):
            return False
        if any(b != 0 for b in data[3:7]):
            return False
        if data[7] != (255 - sum(data[0:7])) & 0xFF:
            return False
        if any(b != 0 for b in data[8:]):
            return False
        return True

    @staticmethod
    def _matches_lighting_profile_pattern(data: list) -> bool:
        if len(data) < 9:
            return False
        if data[0] != 0x07 or data[1] != 0x0D or data[2] != 0x04 or data[3] != 0x04:
            return False
        if data[4] % 0x10 != 0 or data[4] // 0x10 >= LIGHTING_PROFILE_COUNT:
            return False
        if data[5] != 0 or data[6] != 0xC8 or data[7] != 0xC8:
            return False
        if data[8] != (511 - sum(data[0:8])) & 0xFF:
            return False
        if any(b != 0 for b in data[9:]):
            return False
        return True

    @staticmethod
    def _matches_profile_pattern(data: list) -> bool:
        if len(data) < 8:
            return False
        opcode = data[0]
        kb_info = next((v for v in KEYBOARD_TYPES.values() if v["opcode"] == opcode), None)
        if kb_info is None:
            return False
        if data[1] not in range(kb_info["profiles"]):
            return False
        if any(b != 0 for b in data[2:7]):
            return False
        expected_check = (kb_info["checksum_base"] - data[1]) & 0xFF
        if data[7] != expected_check:
            return False
        if any(b != 0 for b in data[8:]):
            return False
        return True

    @staticmethod
    def _matches_battery_pattern(data: list, ev_type: str = "") -> bool:
        # qmk.top battery-query: any TX feature report that isn't a profile frame.
        # (qmk.top's battery query opcode varies between firmwares; rather than
        # over-filtering and missing it, we just exclude the known profile opcode
        # and require it be a feature report of reasonable length.)
        if len(data) < 2:
            return False
        if data[0] == 0x04:
            return False
        return True

    def _save_profile_payload_from_sniff(self, index: int, sample_data: list):
        if self._active_device() is None:
            self._on_sniff_status("Нет активного устройства — выбери клавиатуру в списке.")
            return
        self._capture_profile_payload(index, sample_data)
        name = self._profile_name_at(index) or f"Профиль {index + 1}"
        self._on_sniff_status(f"Payload сохранён в «{name}» (слот {index + 1}).")

    # ---------- Battery test panel ----------
    @staticmethod
    def _parse_int(text, default=0, base=0):
        try:
            text = (text or "").strip()
            if not text:
                return default
            if base == 0:
                return int(text, 0)
            return int(text, base)
        except Exception:
            return default

    @staticmethod
    def _parse_float(text, default=1.0):
        try:
            return float((text or "").strip())
        except Exception:
            return default

    @staticmethod
    def _parse_optional_int(text):
        text = (text or "").strip()
        if not text or text.lower() == "none":
            return None
        try:
            return int(text, 0)
        except Exception:
            return None

    def _ensure_battery_test_fields(self):
        if getattr(self, "bt_report_id", None) is not None:
            return
        self.bt_report_id = ft.TextField(label="report_id", width=110)
        self.bt_response_length = ft.TextField(label="response_length", width=140)
        self.bt_response_offset = ft.TextField(label="response_offset", width=140)
        self.bt_response_scale = ft.TextField(label="response_scale", width=140)
        self.bt_charging_offset = ft.TextField(label="charging_offset", width=140)
        self.bt_charging_mask = ft.TextField(label="charging_mask (hex)", width=160)
        self.bt_result = ft.Text(value="", size=12, selectable=True)

    def _build_battery_test_panel(self):
        self._ensure_battery_test_fields()
        save_btn = ft.FilledTonalButton("Сохранить", on_click=self._battery_test_save)
        test_btn = ft.FilledButton("Тест", on_click=self._battery_test_run)
        inner = ft.Column(
            [
                ft.Row(
                    [self.bt_report_id, self.bt_response_length, self.bt_response_offset],
                    spacing=8, wrap=True,
                ),
                ft.Row(
                    [self.bt_response_scale, self.bt_charging_offset, self.bt_charging_mask],
                    spacing=8, wrap=True,
                ),
                ft.Row([save_btn, test_btn], spacing=8),
                self.bt_result,
            ],
            spacing=8,
            tight=True,
        )
        tile_cls = getattr(ft, "ExpansionTile", None)
        if tile_cls is not None:
            try:
                return tile_cls(
                    title=ft.Text("Battery test panel"),
                    expanded=False,
                    controls=[ft.Container(content=inner, padding=12)],
                )
            except Exception:
                pass
        return ft.Container(
            content=ft.Column(
                [ft.Text("Battery test panel", weight=ft.FontWeight.W_600), inner],
                spacing=8, tight=True,
            ),
            padding=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
            border_radius=12,
        )

    def _battery_test_sync_from_active(self):
        self._ensure_battery_test_fields()
        entry = self._active_device()
        batt = (entry or {}).get("battery") or {}
        mapping = [
            (self.bt_report_id, batt.get("report_id", 0), False),
            (self.bt_response_length, batt.get("response_length", 65), False),
            (self.bt_response_offset, batt.get("response_offset", 2), False),
            (self.bt_response_scale, batt.get("response_scale", 1), False),
            (self.bt_charging_offset,
             "" if batt.get("charging_offset") is None else batt.get("charging_offset"), False),
            (self.bt_charging_mask, hex(int(batt.get("charging_mask", 0) or 0)), True),
        ]
        for field_widget, value, is_hex in mapping:
            if is_hex:
                field_widget.value = value if isinstance(value, str) else hex(int(value or 0))
            else:
                field_widget.value = "" if value is None else str(value)
            try:
                field_widget.update()
            except Exception:
                pass

    def _battery_test_build_config(self):
        entry = self._active_device()
        existing = (entry or {}).get("battery") or {}
        return {
            "query": list(existing.get("query") or []),
            "report_id": self._parse_int(self.bt_report_id.value, 0),
            "response_length": self._parse_int(self.bt_response_length.value, 65),
            "response_offset": self._parse_int(self.bt_response_offset.value, 0),
            "response_scale": self._parse_float(self.bt_response_scale.value, 1.0),
            "charging_offset": self._parse_optional_int(self.bt_charging_offset.value),
            "charging_mask": self._parse_int(self.bt_charging_mask.value, 0),
        }

    def _battery_test_save(self, e):
        entry = self._active_device()
        if entry is None:
            self._snack("Нет активного устройства")
            return
        cfg = self._battery_test_build_config()
        batt = entry.setdefault("battery", {})
        # Preserve query as-is (we don't expose query editing here)
        cfg["query"] = list(batt.get("query") or [])
        batt.update(cfg)
        self.save_config()
        # Recreate / repoint battery monitor at the new dict
        try:
            self.battery_monitor = BatteryMonitor(
                config_battery=self.config["battery"],
                usb_lock=self.usb_lock,
                get_device_path=self.get_keyboard_path_safe,
                get_device_paths=self.get_keyboard_paths,
                on_working_path=self._cache_working_path,
                default_query=DEFAULT_BATTERY_QUERY,
            )
        except Exception:
            pass
        self._snack("Battery конфиг сохранён")

    def _battery_test_run(self, e):
        entry = self._active_device()
        if entry is None:
            self.bt_result.value = "Нет активного устройства"
            try:
                self.bt_result.update()
            except Exception:
                pass
            return
        cfg = self._battery_test_build_config()
        try:
            monitor = BatteryMonitor(
                config_battery=cfg,
                usb_lock=self.usb_lock,
                get_device_path=self.get_keyboard_path_safe,
                get_device_paths=self.get_keyboard_paths,
                on_working_path=self._cache_working_path,
                default_query=DEFAULT_BATTERY_QUERY,
            )
            monitor.read_once()
            state = monitor.state
            if state.percent is not None:
                self.bt_result.value = f"→ percent={state.percent}, charging={state.charging}"
            else:
                self.bt_result.value = "→ ошибка (см. лог)"
        except Exception as exc:
            self.bt_result.value = f"→ ошибка: {exc}"
        try:
            self.bt_result.update()
        except Exception:
            pass

    def _save_battery_query_from_sniff(self, data, report_id):
        entry = self._active_device()
        if entry is None:
            self._snack("Нет активного устройства")
            return
        batt = entry.get("battery")
        if not isinstance(batt, dict):
            batt = {
                "query": [],
                "report_id": 0,
                "response_length": 65,
                "response_offset": 2,
                "response_scale": 1,
                "charging_offset": None,
                "charging_mask": 0,
            }
            entry["battery"] = batt
        batt["query"] = list(data)
        try:
            batt["report_id"] = int(report_id) if report_id is not None else 0
        except (TypeError, ValueError):
            batt["report_id"] = 0
        self.save_config()
        if getattr(self, "battery_monitor", None) is not None:
            try:
                threading.Thread(target=self._refresh_battery_now, daemon=True).start()
            except Exception:
                pass
        self._snack("Battery query сохранён")

    def _capture_profile_payload(self, index: int, sample_data: list):
        """Save the actually-observed payload for the specific slot only.
        Preserves other slots' existing payloads — no synthesizing payload[1]=idx,
        which produced bytes the keyboard never sent and silently broke switching."""
        _pc = self._device_profile_count()
        if not (0 <= index < _pc):
            return
        items = self._profile_items()
        while len(items) < _pc:
            items.append((f"Профиль {len(items) + 1}", {"data": [], "hotkey": ""}))
        name, info = items[index]
        info = dict(info or {})
        info["data"] = list(sample_data)
        info.setdefault("hotkey", "")
        items[index] = (name, info)
        new_payloads = {n: i for n, i in items}
        self.config["payloads"] = new_payloads
        entry = self._active_device()
        if entry is not None:
            entry["payloads"] = new_payloads
        self.save_config()
        def upd():
            try:
                self.update_payloads_list()
            except Exception:
                pass
            try:
                self.update_bindings_list()
            except Exception:
                pass
            self.page.update()
        try:
            self.page.run_thread(upd)
        except Exception:
            upd()

    def _autofill_profiles(self, sample_data: list, silent: bool = False):
        if not self._matches_profile_pattern(sample_data):
            if not silent:
                self.sniff_status.value = "Не похоже на профильный payload."
                self.page.update()
            return
        _pc = self._device_profile_count()
        items = self._profile_items()
        while len(items) < _pc:
            items.append((f"Профиль {len(items) + 1}", {"data": [], "hotkey": ""}))
        new_payloads = {}
        for idx in range(_pc):
            name, info = items[idx]
            payload = list(sample_data)
            payload[1] = idx
            new_payloads[name] = {
                "data": payload,
                "hotkey": info.get("hotkey", ""),
            }
        self.config["payloads"] = new_payloads
        entry = self._active_device()
        if entry is not None:
            entry["payloads"] = new_payloads
        self.save_config()
        def upd():
            try:
                self.update_payloads_list()
            except Exception:
                pass
            try:
                self.update_bindings_list()
            except Exception:
                pass
            if not silent:
                self.sniff_status.value = "Payload загружен в 4 профиля."
                self.page.update()
            else:
                self.page.update()
        try:
            self.page.run_thread(upd)
        except Exception:
            upd()

    def clear_sniffer_log(self):
        with self._sniff_events_lock:
            self.sniff_events.clear()
        self.sniff_log.controls.clear()
        self.page.update()

    def copy_sniffer_log(self):
        events = self._sniff_events_snapshot()
        payload = {
            "outgoing": [e for e in events if e.get("dir") == "tx"],
            "incoming": [e for e in events if e.get("dir") == "rx"],
            "all": events,
        }
        text = json.dumps(payload, indent=2, ensure_ascii=False)

        async def copy():
            try:
                await self.clipboard.set(text)
                self.sniff_status.value = f"Скопировано {len(events)} событий в буфер."
            except Exception as exc:
                logger.exception("sniffer copy failed")
                self.sniff_status.value = f"Не удалось скопировать журнал: {exc}"
            self.page.update()

        self.page.run_task(copy)

    # ---------- Service control ----------
    def toggle_service(self):
        if self.is_running:
            self.is_running = False
            self._stop_auto_profile_switching()
            try:
                keyboard.unhook_all()
            except Exception:
                pass
            self._set_status(False)
        else:
            if not self.config.get("active_device"):
                self._snack("Выберите HID-устройство")
                return
            self.is_running = True
            self.current_binding = None
            self.save_config()
            self.reload_runtime_state()
            self._set_status(True)
            self.worker_thread = threading.Thread(target=self.background_task, daemon=True)
            self.worker_thread.start()

    def _set_keyboard_recovery_busy(self, busy):
        """Keep the header recovery action single-flight and visibly honest."""
        button = getattr(self, "keyboard_recovery_button", None)
        if button is None:
            return
        try:
            button.disabled = bool(busy)
            button.update()
        except Exception:
            pass

    def _recover_keyboard_connection(self):
        """Safely leave diagnostic modes and reconnect the selected keyboard.

        This is intentionally not a factory reset.  The only direct firmware
        write is Womier's already-verified, idempotent ``0x1E/0`` calibration
        stop command.  It does not clear profiles, lighting, magnetic values,
        bindings, or local configuration.
        """
        recovery_lock = getattr(self, "_keyboard_recovery_lock", None)
        if recovery_lock is None:
            recovery_lock = threading.Lock()
            self._keyboard_recovery_lock = recovery_lock
        if not recovery_lock.acquire(blocking=False):
            self._snack("Восстановление клавиатуры уже выполняется.")
            return
        self._set_keyboard_recovery_busy(True)

        def worker():
            error = None
            calibration = None
            tester = getattr(self, "_magnetic_travel_tester", None)
            try:
                # Both public stop paths only signal their own workers; their
                # workers retain ownership of the matching protocol stop
                # command.  This keeps their teardown idempotent.
                self._stop_magnetic_travel_tester(reset_ui=False)
                calibration = self._stop_magnetic_calibration(reset_ui=False)
                if calibration is not None:
                    self._wait_for_magnetic_calibration_cleanup(
                        calibration, timeout=1.5
                    )
                reader = getattr(tester, "reader_thread", None)
                if reader is not None and reader is not threading.current_thread():
                    try:
                        reader.join(timeout=0.5)
                    except Exception:
                        pass

                # Do not let queued profile/slider activity resurrect the
                # exact state the user is trying to recover from.
                self._stop_magnetic_profile_switching()
                self._cancel_pending_magnetic_writes()

                entry = self._active_device()
                if entry is not None and entry.get("keyboard_type") == "magnetic":
                    # Confirmed against the official driver: this exits a
                    # stuck calibration mode and is safe when none is active.
                    self._send_magnetic_packets(
                        [MagneticProtocol.calibration_stop_packet()],
                        "keyboard_recovery_stop_calibration",
                    )
                # A feature interface can change after diagnostic mode.  Drop
                # only the transient path cache, never configuration data.
                self._working_hid_path.clear()
            except Exception as exc:
                logger.debug("keyboard connection recovery failed", exc_info=True)
                error = str(exc)
            finally:
                try:
                    recovery_lock.release()
                except RuntimeError:
                    pass

            def finish():
                try:
                    self.refresh_devices()
                    self._refresh_battery_for_tray()
                except Exception:
                    logger.debug("keyboard recovery refresh failed", exc_info=True)
                self._set_keyboard_recovery_busy(False)
                if error:
                    self._snack(f"Связь обновлена, но команда остановки вернула ошибку: {error}")
                else:
                    self._snack("Связь с клавиатурой восстановлена. Настройки не изменены.")

            self._ui_call(finish)

        threading.Thread(
            target=worker,
            daemon=True,
            name="keyboard-connection-recovery",
        ).start()

    def _set_status(self, running):
        if running:
            self.toggle_button.icon = ft.Icons.STOP_ROUNDED
            self.toggle_button.tooltip = "Остановить службу"
            self.toggle_button.style = self._service_button_style(running=True)
        else:
            self.toggle_button.icon = ft.Icons.PLAY_ARROW_ROUNDED
            self.toggle_button.tooltip = "Запустить службу"
            self.toggle_button.style = self._service_button_style(running=False)
        self.page.update()

    # ---------- HID ----------
    def get_keyboard_path(self):
        """Возвращает первый path активного устройства (для совместимости —
        BatteryMonitor использует именно эту сигнатуру). Предпочитает кэшированный
        рабочий path, если он всё ещё перечисляется."""
        paths = self.get_keyboard_paths()
        return paths[0] if paths else None

    def get_keyboard_paths(self):
        """Все HID-пути активного устройства с подходящим usage_page.
        Кэшированный «рабочий» path выносится в начало списка."""
        dev = self.config.get("device")
        if not dev:
            return []
        vid = dev["vid"]
        pid = dev["pid"]
        usage_page = dev["usage_page"]
        paths = []
        for d in hid.enumerate(vid, pid):
            if d['usage_page'] == usage_page:
                paths.append(d['path'])
        cache_key = self._device_key(vid, pid, usage_page)
        cached = self._working_hid_path.get(cache_key)
        if cached and cached in paths:
            paths.remove(cached)
            paths.insert(0, cached)
        elif cached:
            self._working_hid_path.pop(cache_key, None)
        return paths

    def _magnetic_travel_input_paths(self):
        """Return only SK75's dedicated live-travel input endpoint.

        The normal magnetic control packets use the vendor feature endpoint
        (usage 2).  Womier sends simulation-test measurements on a separate
        vendor input endpoint (usage 1), so deliberately never fall back to a
        normal keyboard interface here.
        """
        dev = self.config.get("device") or {}
        if not dev:
            return []
        try:
            vid, pid = int(dev["vid"]), int(dev["pid"])
        except (KeyError, TypeError, ValueError):
            return []
        paths = []
        for endpoint in hid.enumerate(vid, pid):
            if endpoint.get("usage_page") == 0xFFFF and endpoint.get("usage") == 1:
                path = endpoint.get("path")
                if path:
                    paths.append(path)
        return paths

    def get_keyboard_path_safe(self):
        """Like get_keyboard_path but returns None if device isn't configured."""
        if not self.config.get("device"):
            return None
        return self.get_keyboard_path()

    def _cache_working_path(self, path):
        """Called by BatteryMonitor when it finds a working HID path."""
        dev = self.config.get("device") or {}
        cache_key = self._device_key(dev.get("vid", 0), dev.get("pid", 0), dev.get("usage_page", 0))
        self._working_hid_path[cache_key] = path
        logger.debug("cached working HID path from battery: %s", path)

    def _diagnose_hid_endpoints(self):
        dev = self.config.get("device") or {}
        if not dev:
            return
        vid, pid = dev.get("vid", 0), dev.get("pid", 0)
        vendor_paths = []
        for d in hid.enumerate(vid, pid):
            if d["usage_page"] == 0xFFFF:
                vendor_paths.append({
                    "path": d["path"],
                    "usage": d["usage"],
                    "interface": d.get("interface_number", -1),
                })
        safe_query_data = [0xF7] + [0x00] * 63
        test_sizes = [8, 16, 32, 33, 64]
        logger.debug("=== HID endpoint diagnostic ===")
        for ep in vendor_paths:
            path = ep["path"]
            logger.debug("--- probing path=%s usage=0x%04x interface=%d ---",
                         path, ep["usage"], ep["interface"])
            for data_size in test_sizes:
                report = [0x00] + safe_query_data[:data_size]
                try:
                    device = hid.device()
                    device.open_path(path)
                    device.set_nonblocking(1)
                    rc = device.send_feature_report(report)
                    response = None
                    if rc is not None and rc > 0:
                        try:
                            response = device.get_feature_report(0, len(report))
                        except Exception:
                            pass
                    device.close()
                    logger.debug("  size=%d(+1) rc=%s response=%s",
                                 data_size, rc,
                                 [f"0x{b:02x}" for b in response[:16]] if response else None)
                except Exception as exc:
                    logger.debug("  size=%d(+1) EXCEPTION: %s", data_size, exc)
                    try:
                        device.close()
                    except Exception:
                        pass
        logger.debug("=== end HID endpoint diagnostic ===")

    def _battery_is_available_for_active_device(self):
        """Avoid querying a wired-only device for a battery it cannot expose."""
        entry = self._active_device()
        if entry is None:
            return True
        if entry.get("keyboard_type") is None:
            return False
        # The active SK75 is identified as wired.  Its all-zero reply is not a
        # charge measurement, so show an unknown battery state rather than
        # eventually accepting a repeated fake 0% response.
        return entry.get("transport") != "wired"

    def _publish_battery_unavailable(self):
        state = BatteryState()
        self._update_tray_indicator(state)
        self.publish_battery_to_ui(state)

    def _update_tray_indicator(self, state: BatteryState):
        """Render a wired SK75 as USB instead of an unknown battery.

        Battery state is intentionally unavailable for the wired SK75, but
        that does not mean that the tray has no connection information.  Keep
        the connection type separate from the battery query so a real
        wireless battery reading continues to show its charge.
        """
        tray = getattr(self, "tray", None)
        if tray is None:
            return
        entry = self._active_device()
        transport = entry.get("transport") if isinstance(entry, dict) else None
        try:
            tray.update_battery(state, transport=transport)
        except TypeError:
            # Preserve compatibility with a third-party/custom tray object
            # that still implements the historical one-argument method.
            tray.update_battery(state)

    def battery_poll_loop(self):
        logger.info("battery poll loop started (60s interval)")
        print("[Battery] Поток опроса батареи запущен (каждые 60 сек).")
        while self.app_alive:
            try:
                if not self._battery_is_available_for_active_device():
                    logger.debug("battery poll skipped: unavailable for active device")
                    self._publish_battery_unavailable()
                else:
                    self.battery_monitor.read_once()
                    state = self.battery_monitor.state
                    logger.debug("battery poll: percent=%s charging=%s stale=%s",
                                 state.percent, state.charging, state.is_stale)
                    self._update_tray_indicator(state)
                    self.publish_battery_to_ui(state)
            except Exception as e:
                print(f"[Battery] Ошибка цикла опроса: {e}")
            for _ in range(60):
                if not self.app_alive:
                    return
                time.sleep(1)

    def publish_battery_to_ui(self, state: BatteryState):
        def do():
            if state.is_stale or state.percent is None:
                self.battery_chip_icon.name = ft.Icons.BATTERY_UNKNOWN
                self.battery_chip_icon.color = ft.Colors.ON_SURFACE_VARIANT
                self.battery_chip_text.value = "—"
            else:
                if state.percent >= 50:
                    self.battery_chip_icon.name = ft.Icons.BATTERY_FULL_ROUNDED
                    self.battery_chip_icon.color = ft.Colors.TERTIARY
                elif state.percent >= 20:
                    self.battery_chip_icon.name = ft.Icons.BATTERY_3_BAR_ROUNDED
                    self.battery_chip_icon.color = ft.Colors.SECONDARY
                else:
                    self.battery_chip_icon.name = ft.Icons.BATTERY_ALERT_ROUNDED
                    self.battery_chip_icon.color = ft.Colors.ERROR
                suffix = " ⚡" if state.charging else ""
                self.battery_chip_text.value = f"{state.percent}%{suffix}"
            try:
                self.page.update()
            except Exception:
                pass
        self._ui_call(do)

    def _refresh_battery_for_tray(self):
        """Read battery and update tray icon immediately (non-blocking thread)."""
        def _do():
            try:
                if not self._battery_is_available_for_active_device():
                    self._publish_battery_unavailable()
                    return
                self.battery_monitor.read_once()
                state = self.battery_monitor.state
                self._update_tray_indicator(state)
                self.publish_battery_to_ui(state)
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()

    def _refresh_battery_now(self):
        if not self._battery_is_available_for_active_device():
            self._publish_battery_unavailable()
            return
        self.battery_monitor.read_once()
        state = self.battery_monitor.state
        self._update_tray_indicator(state)
        self.publish_battery_to_ui(state)

    def _update_transport_icon(self):
        """Sync header transport icon with active device's transport field."""
        icon = getattr(self, "transport_icon", None)
        if icon is None:
            return
        entry = self._active_device()
        transport = entry.get("transport") if entry else None
        if transport == "wired":
            icon.icon = ft.Icons.USB
            icon.color = ft.Colors.BLUE_400
            icon.tooltip = "Проводное подключение"
            icon.visible = True
        elif transport == "wireless":
            icon.icon = ft.Icons.WIFI_TETHERING_ROUNDED
            icon.color = ft.Colors.GREEN_400
            icon.tooltip = "Беспроводное подключение"
            icon.visible = True
        else:
            icon.visible = False
        try:
            icon.update()
        except Exception:
            pass

    def _on_transport_override_change(self, e):
        return

    def _show_setup_wizard(self, device_key: str):
        if not device_key:
            return
        entry = self.config["devices"].get(device_key)
        if not entry:
            return
        if entry.get("keyboard_type") in KEYBOARD_TYPES:
            return
        logger.info("Setup wizard opened for device %s (%s)", device_key, entry.get("label", ""))

        label = entry.get("label") or "Unknown Device"
        vid = entry.get("vid", 0)
        pid = entry.get("pid", 0)

        confirm_checkbox = ft.Checkbox(
            label="Я подтверждаю, что это магнитная клавиатура Womier SK75 TMR",
            value=False,
        )

        save_btn = ft.ElevatedButton("Продолжить", disabled=True)

        def _update_save_state(_=None):
            save_btn.disabled = not bool(confirm_checkbox.value)
            try:
                self.page.update()
            except Exception:
                pass

        confirm_checkbox.on_change = lambda e: _update_save_state()

        def _on_save(_):
            # This SK75 TMR companion deliberately supports the magnetic
            # protocol only.  Keeping the assignment explicit behind a
            # confirmation avoids sending any HID command until the owner has
            # acknowledged that this is the matching keyboard.
            kb_type = "magnetic"
            logger.info("Magnetic SK75 setup confirmed for device %s", device_key)
            try:
                self.page.pop_dialog()
            except Exception:
                pass
            self._set_keyboard_type(vid, pid, entry.get("usage_page", 0), kb_type)

        def _on_cancel(_):
            logger.info("Setup wizard cancelled for device %s", device_key)
            try:
                self.page.pop_dialog()
            except Exception:
                pass
            if self.config.get("active_device") == device_key:
                prev_keys = [k for k in self.config["devices"]
                             if k != device_key and self.config["devices"][k].get("keyboard_type") in KEYBOARD_TYPES]
                new_active = prev_keys[0] if prev_keys else None
                if new_active:
                    self._activate_device(new_active)
                    self.device_dropdown.value = new_active
                else:
                    self.config["active_device"] = None
                    self._ensure_active_device_aliases()
                    self.device_dropdown.value = None
                try:
                    self.page.update()
                except Exception:
                    pass

        save_btn.on_click = _on_save

        warning_col = ft.Column([
            ft.Row([
                ft.Icon(ft.Icons.KEYBOARD_ROUNDED, color=ft.Colors.PRIMARY, size=24),
                ft.Text("ТОЛЬКО МАГНИТНЫЕ СВИТЧИ", weight=ft.FontWeight.BOLD, color=ft.Colors.PRIMARY, size=16),
            ], spacing=8),
            ft.Text(
                "QMK.Top Manager for SK75 TMR работает только с магнитными "
                "свитчами Womier SK75 TMR. После подтверждения для этого "
                "устройства будет безопасно включён магнитный режим.",
                size=13,
                color=ft.Colors.ON_SURFACE,
            ),
        ], spacing=6)

        warning_block = ft.Container(
            content=warning_col,
            bgcolor=ft.Colors.SURFACE_CONTAINER,
            border=ft.Border(
                ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT),
                ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT),
                ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT),
                ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT),
            ),
            border_radius=12,
            padding=ft.Padding(left=16, top=12, right=16, bottom=12),
        )

        content = ft.Column([
            ft.Text(f"Устройство: {label}", size=14, weight=ft.FontWeight.W_500),
            ft.Text(f"VID: 0x{vid:04X}   PID: 0x{pid:04X}", size=12,
                    color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Divider(height=16, color=ft.Colors.TRANSPARENT),
            warning_block,
            ft.Divider(height=8, color=ft.Colors.TRANSPARENT),
            confirm_checkbox,
        ], spacing=4, tight=True, width=400)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("⌨ Настройка клавиатуры", size=20, weight=ft.FontWeight.BOLD),
            content=content,
            actions=[
                ft.TextButton("Отмена", on_click=_on_cancel),
                save_btn,
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            shape=ft.RoundedRectangleBorder(radius=20),
        )

        self.page.show_dialog(dlg)

    def _set_keyboard_type(self, vid, pid, usage_page, kb_type):
        if kb_type not in ("magnetic", "mechanical"):
            return
        key = self._device_key(vid, pid, usage_page)
        entry = self.config["devices"].get(key)
        if entry is None:
            hid_dev = next(
                (d for d in getattr(self, "filtered_devices", [])
                 if d['vendor_id'] == vid and d['product_id'] == pid and d['usage_page'] == usage_page),
                None,
            )
            if hid_dev is None:
                return
            entry = self._empty_device_entry(vid, pid, usage_page, label=self._device_label_for(hid_dev))
            self.config["devices"][key] = entry
            self._normalize_device_entry(entry)
        entry["keyboard_type"] = kb_type
        transport = entry.get("transport")
        if kb_type == "mechanical":
            default_cooldown = 2000 if transport == "wireless" else 1000
        else:
            default_cooldown = 250 if transport == "wireless" else 100
        entry["cooldown_ms"] = default_cooldown
        self._normalize_device_entry(entry)

        # A first public launch starts with an empty app-owned config.  Once
        # the user has explicitly confirmed the connected SK75, safely import
        # only the official Womier magnetic cache (read-only local LevelDB)
        # before creating our own first save.  This never imports another
        # QMK.Top Manager's names, bindings, RGB choices or profiles, and it
        # never writes a HID command.  The later startup reads still make the
        # physical keyboard the source of truth for live settings.
        if kb_type == "magnetic" and key == self.config.get("active_device"):
            try:
                self.config, womier_report = self._import_womier_magnetic_profiles_data(
                    self.config, automatic=True
                )
                if womier_report is not None:
                    logger.info(
                        "first-device Womier import: %s profile(s)",
                        womier_report.get("profiles_imported", 0),
                    )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                # The official driver may be absent, locked or use an unknown
                # cache version.  That must not prevent a clean hardware-only
                # setup from completing.
                logger.info("official Womier cache skipped during setup: %s", exc)
        self.save_config()
        if key == self.config.get("active_device"):
            self._ensure_active_device_aliases()
            was_running = self.is_running
            if was_running:
                self.is_running = False
                self._stop_auto_profile_switching()
                try:
                    keyboard.unhook_all()
                except Exception:
                    pass
            try:
                self.update_payloads_list()
            except Exception:
                pass
            try:
                self.update_bindings_list()
            except Exception:
                pass
            if was_running:
                self.is_running = True
                self.reload_runtime_state()
                self._set_status(True)
                if not self.worker_thread or not self.worker_thread.is_alive():
                    self.worker_thread = threading.Thread(target=self.background_task, daemon=True)
                    self.worker_thread.start()
        self.refresh_devices()
        try:
            self._update_transport_icon()
        except Exception:
            pass

        if kb_type == "magnetic" and key == self.config.get("active_device"):
            def _hydrate_new_magnetic_device():
                # Let the confirmation dialog and first UI patch settle; all
                # calls below are read-only and retain their existing HID
                # failure handling.  This avoids showing release defaults as
                # the user's actual Womier/keyboard state on first use.
                time.sleep(0.25)
                self._read_magnetic_matrix(silent=True)
                self._magnetic_read_keyboard_options(silent=True)
                self._read_lighting_settings_from_keyboard(silent=True)

            threading.Thread(
                target=_hydrate_new_magnetic_device,
                daemon=True,
                name="first-sk75-hardware-hydrate",
            ).start()

    def _refresh_battery_for_sniff_chip(self):
        self._refresh_battery_now()
        state = self.battery_monitor.state
        chip = getattr(self, "_sniff_battery_chip", None)
        if chip is None:
            return
        if state.is_stale or state.percent is None:
            txt = "—"
        else:
            suffix = " ⚡" if state.charging else ""
            txt = f"{state.percent}%{suffix}"
        def upd():
            try:
                chip.value = txt
                self.page.update()
            except Exception:
                pass
        try:
            self.page.run_thread(upd)
        except Exception:
            upd()

    def _send_hid_payload(self, payload_data, label="payload"):
        """Send a single HID feature report to all matching paths. Returns first successful path or None.
        Caller must hold self.usb_lock."""
        full_report = [0x00] + payload_data
        logger.debug("_send_hid_payload [%s] report[:%d]=%s",
                      label, min(len(full_report), 16),
                      [f"0x{b:02x}" for b in full_report[:16]])
        paths = self.get_keyboard_paths()
        if not paths:
            logger.warning("_send_hid_payload [%s]: no HID paths found", label)
            return None
        dev = self.config.get("device") or {}
        cache_key = self._device_key(dev.get("vid", 0), dev.get("pid", 0), dev.get("usage_page", 0))
        sent_path = None
        last_err = None
        for path in paths:
            try:
                device = hid.device()
                device.open_path(path)
                device.set_nonblocking(1)
                rc = device.send_feature_report(full_report)
                logger.debug("_send_hid_payload [%s] path=%s rc=%s", label, path, rc)
                if rc is not None and rc > 0:
                    try:
                        read_back = device.get_feature_report(0, min(len(full_report), 65))
                        logger.debug("_send_hid_payload [%s] read_back=%s", label,
                                     [f"0x{b:02x}" for b in read_back[:16]] if read_back else None)
                    except Exception as rb_err:
                        logger.debug("_send_hid_payload [%s] read_back failed: %s", label, rb_err)
                device.close()
            except Exception as e:
                last_err = e
                try:
                    device.close()
                except Exception:
                    pass
                continue
            if rc is None or rc > 0:
                sent_path = sent_path or path
                if rc is not None and rc > 0:
                    break
        if sent_path:
            self._working_hid_path[cache_key] = sent_path
        else:
            logger.warning("_send_hid_payload [%s] FAILED on all paths, last_err=%s", label, last_err)
        return sent_path

    def apply_payload(
        self,
        profile_name,
        payload_data,
        *,
        should_continue=None,
        suppress_input=True,
        automatic=False,
    ):
        """Apply one ordinary keyboard profile without overlapping HID work.

        ``should_continue`` is used only by foreground automation.  It lets a
        newer Alt+Tab target cancel an old transaction between firmware
        stages, while direct callers retain the former fully synchronous
        behaviour.  Automatic switches deliberately set ``suppress_input``
        false and send only the primary profile feature report: an Alt+Tab
        must never install a global Windows keyboard hook, wait for auxiliary
        stages, or queue a large per-key magnetic transaction.
        """
        def can_continue():
            if should_continue is None:
                return True
            try:
                return bool(should_continue())
            except Exception:
                logger.debug("profile transaction cancellation check failed", exc_info=True)
                return False

        def wait_for_stage(stage):
            """Wait in small cancellable slices instead of blocking a stale worker."""
            seconds = _stage_delay_ms(entry, stage) / 1000.0
            if seconds <= 0:
                return can_continue()
            deadline = time.monotonic() + seconds
            sleeper = threading.Event()
            while True:
                if not can_continue():
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return can_continue()
                # 20 ms is short enough that a quick Alt+Tab releases the HID
                # lock promptly, while retaining the original total stage
                # delay for a stable direct/manual transaction.
                sleeper.wait(min(0.020, remaining))

        if not can_continue():
            return False
        entry = self._active_device()
        if entry and entry.get("keyboard_type") is None:
            logger.warning("HID write blocked: device %s has no keyboard_type configured",
                           self.config.get("active_device"))
            self._ui_call(lambda: self._show_setup_wizard(self.config.get("active_device")))
            return False
        logger.debug("apply_payload profile=%s payload[:%d]=%s",
                      profile_name, min(len(payload_data), 16),
                      [f"0x{b:02x}" for b in payload_data[:16]])

        kb_type = entry.get("keyboard_type") if entry else None
        caps = device_capabilities(kb_type)
        logger.debug("apply_payload: keyboard_type=%s caps=%s", kb_type, caps)

        cooldown_ms = _resolved_cooldown_ms(entry)
        hook = None
        if cooldown_ms > 0 and suppress_input:
            _release_all_keys()
            hook = _suppress_keyboard_start()
            logger.debug("apply_payload: keyboard suppressed (transaction-bound, device %s)",
                         self.config.get("active_device"))

        sent_path = None
        refresh_battery = bool(entry and entry.get("battery", {}).get("query"))
        try:
            with self.usb_lock:
                if not can_continue():
                    return False
                # 1. Profile switch first — keymap is the primary operation
                sent_path = self._send_hid_payload(payload_data, label=f"profile_{profile_name}")
                if sent_path is None:
                    logger.error("profile switch FAILED for %s", profile_name)
                    print("[Ошибка USB] Не удалось отправить HID пакет ни в один интерфейс.")
                    return False

                # A foreground switch must be one short profile report.  The
                # firmware stage pause is useful for an explicit manual apply,
                # but keeping the HID lock during it turns rapid Alt+Tab into
                # visible input loss on an SK75.
                if not automatic and not wait_for_stage("profile"):
                    return False

                info = self._profile_info_at_by_name(profile_name)

                # 2. Polling rate (capability-gated)
                if (
                    not automatic
                    and can_continue()
                    and DeviceCapability.POLLING_RATE in caps
                    and info
                ):
                    pr = info.get("polling_rate")
                    if pr and pr in VALID_POLLING_RATES:
                        logger.debug("apply_payload: sending polling rate %d Hz", pr)
                        pr_path = self._send_hid_payload(
                            _polling_rate_payload(PollingRate(pr)),
                            label=f"polling_rate_{pr}Hz")
                        if pr_path:
                            if not wait_for_stage("polling"):
                                return False
                        else:
                            logger.warning("apply_payload: polling rate send FAILED")

                # 3. Lighting profile after profile stabilizes (capability-gated)
                if (
                    not automatic
                    and can_continue()
                    and DeviceCapability.LIGHTING_PROFILES in caps
                    and info
                ):
                    lp = info.get("lighting_profile")
                    if lp is not None and lp in VALID_LIGHTING_PROFILES:
                        logger.debug("apply_payload: sending lighting profile %d after profile switch", lp + 1)
                        lp_path = self._send_hid_payload(
                            _lighting_profile_payload(lp),
                            label=f"lighting_profile_{lp + 1}")
                        if lp_path:
                            if not wait_for_stage("lighting"):
                                return False
                        else:
                            logger.warning("apply_payload: lighting profile send FAILED")
                elif not automatic and DeviceCapability.LIGHTING_PROFILES not in caps:
                    logger.debug("apply_payload: lighting subsystem DISABLED for %s", kb_type)
        finally:
            if hook is not None:
                try:
                    keyboard.unhook(hook)
                except Exception:
                    pass
                logger.debug("apply_payload: keyboard suppression removed after transaction")

        if sent_path is None or not can_continue():
            return False

        # BatteryMonitor owns the same non-reentrant USB lock.  Start its
        # asynchronous refresh only after releasing this profile transaction;
        # doing it while holding the lock leaves a waiting battery thread and
        # needlessly prolongs the next feature-report operation.
        if refresh_battery and not automatic:
            self._refresh_battery_for_tray()

        # The normal SK75 profile packet does not contain magnetic settings.
        # Pair its index with the app-side magnetic preset only after releasing
        # ``usb_lock`` above; the preset worker needs that same lock for its
        # small batch of per-key packets.  Previously this omission meant a
        # process rule could visibly select profile 2/3/4 while Magnetic Lab
        # (and the keyboard) retained profile 1's thresholds.
        if kb_type == "magnetic":
            profile_index = self._profile_index_by_name(profile_name)
            if profile_index is not None:
                if automatic or should_continue is not None:
                    self._select_magnetic_preset_for_keyboard_profile(
                        profile_index,
                        automatic=automatic,
                        should_continue=should_continue,
                    )
                else:
                    # Keep the public/direct caller shape compatible with
                    # existing integrations; only automation needs the
                    # cancellable, delayed magnetic path.
                    self._select_magnetic_preset_for_keyboard_profile(profile_index)

        if not can_continue():
            return False

        self.current_binding = profile_name
        print(f"[Авто] Успешно применен профиль: {profile_name} (path={sent_path!r})")
        return True

    def background_task(self):
        logger.info("background window scanner started")
        print("[DEBUG] Фоновый сканер окон запущен...")
        while self.is_running:
            try:
                hwnd = win32gui.GetForegroundWindow()
                if hwnd:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    try:
                        active_process = psutil.Process(pid).name().lower()
                    except psutil.AccessDenied:
                        print(f"[ОШИБКА ДОСТУПА] Процесс защищен (PID {pid}). Нужны права Администратора!")
                        time.sleep(2)
                        continue
                    except Exception:
                        continue

                    if active_process != self.last_active_window:
                        print(f"[DEBUG] Активное окно: '{active_process}'")
                        self.last_active_window = active_process

                        target_pi = self.rule_evaluator.match(active_process)
                        if target_pi is not None:
                            logger.debug("rule matched: process=%s → profile_index=%d", active_process, target_pi)
                        else:
                            disabled = self.rule_evaluator.is_disabled_match(active_process)
                            if disabled:
                                logger.debug("rule SKIPPED (disabled): process=%s → profile_index=%d",
                                             active_process, disabled.profile_index)
                            if self.default_profile_index is not None:
                                target_pi = self.default_profile_index
                        if target_pi is not None:
                            entry = self._active_device()
                            if entry and entry.get("keyboard_type") is None:
                                self._request_auto_profile_switch(None)
                                continue
                            name = self._profile_name_at(target_pi)
                            payload = self._profile_payload_at(target_pi)
                            if name and payload is not None:
                                # Do not sleep or write HID from the polling
                                # loop.  The coordinator owns one cancellable
                                # latest-wins request, so several Alt+Tabs
                                # cannot build a queue of profile changes or
                                # keyboard-suppression hooks.
                                self._request_auto_profile_switch(
                                    name,
                                    payload,
                                    process_name=active_process,
                                    entry=entry,
                                )
                            else:
                                self._request_auto_profile_switch(None)
                        else:
                            # Moving to an unbound process must cancel a
                            # delayed rule rather than letting the old app's
                            # profile arrive after focus has already changed.
                            self._request_auto_profile_switch(None)
            except Exception as e:
                print(f"[GLOBAL ERROR] Сбой в цикле сканирования: {e}")
            time.sleep(PROFILE_WINDOW_POLL_INTERVAL_SEC)

    # ---------- Tray callbacks (run on pystray thread) ----------
    def _ui_call(self, fn, *, allow_shutdown=False):
        """Schedule a UI mutation on Flet's page event loop.

        ``Page.run_thread()`` runs a synchronous callable in a worker thread.
        That is useful for blocking work, but is the opposite of what tray,
        HID and polling callbacks need: Flet controls may only be changed by
        the page event loop.  Flet 0.85's ``run_task()`` deliberately accepts
        an *async* function, so wrap the existing synchronous callbacks in a
        tiny coroutine instead of calling them directly from their source
        thread.

        There is intentionally no direct-call fallback here.  If the page is
        already closing, mutating controls from a pystray/background thread is
        less safe than dropping that final cosmetic update.
        """
        # Native window shutdown does not cancel every already-queued Flet
        # coroutine.  A late battery/HID/tray callback must not touch a stale
        # control tree after Quit has begun; it is cosmetic at that point and
        # can safely be dropped.  Missing ``app_alive`` deliberately means
        # "alive" for the lightweight detached test managers.
        if not allow_shutdown and not getattr(self, "app_alive", True):
            return None

        async def dispatch():
            if not allow_shutdown and not getattr(self, "app_alive", True):
                return None
            try:
                result = fn()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # A stale callback is normal while the window is being
                # hidden/closed.  Keep it from taking down Flet's event loop.
                logger.debug("scheduled UI callback failed", exc_info=True)

        try:
            return self.page.run_task(dispatch)
        except Exception:
            logger.debug("could not schedule UI callback", exc_info=True)
            return None

    def _show_window(self):
        if not getattr(self, "app_alive", True):
            return
        try:
            self.page.window.skip_task_bar = False
            self.page.window.visible = True
            try:
                self.page.window.minimized = False
            except Exception:
                pass
            self.page.update()
            try:
                self.page.run_task(self.page.window.to_front)
            except Exception:
                pass
        except Exception as exc:
            print(f"[Window] show failed: {exc}")
        if self.tray:
            self.tray.set_window_visible(True)

    def _hide_window(self):
        if not getattr(self, "app_alive", True):
            return
        # Diagnostic magnetic dialogs own reversible firmware modes while they
        # are open.  A hidden window must not leave either reader running.
        try:
            self._stop_magnetic_travel_tester(reset_ui=False)
        except Exception:
            pass
        try:
            self._stop_magnetic_calibration(reset_ui=False)
        except Exception:
            pass
        try:
            self._stop_magnetic_profile_switching()
            # Hiding is not a preset/device change.  Preserve the final
            # debounced slider value instead of cancelling it just because
            # the user immediately opens the official Womier driver.
            self._flush_pending_magnetic_writes()
            # Values already accepted by HID are committed before the window
            # disappears.  A just-started final HID worker will schedule one
            # additional quiet save for its own newer value.
            self._flush_magnetic_persistence()
        except Exception:
            pass
        try:
            self.page.window.visible = False
            self.page.window.skip_task_bar = True
            self.page.update()
        except Exception as exc:
            print(f"[Window] hide failed: {exc}")
        if self.tray:
            self.tray.set_window_visible(False)

    def _tray_show_window(self):
        self._ui_call(self._show_window)

    def _tray_hide_window(self):
        self._ui_call(self._hide_window)

    def _tray_toggle_window(self):
        def do():
            visible = bool(getattr(self.page.window, "visible", True))
            if visible:
                self._hide_window()
            else:
                self._show_window()
        self._ui_call(do)

    def _tray_quit(self):
        self.app_alive = False
        self.is_running = False
        self._stop_auto_profile_switching()
        calibration_session = None
        try:
            self._stop_magnetic_travel_tester(reset_ui=False)
        except Exception:
            pass
        try:
            calibration_session = self._stop_magnetic_calibration(reset_ui=False)
        except Exception:
            pass
        flush_thread = None
        try:
            self._stop_magnetic_profile_switching()
            # Unlike profile/device switching, quitting should persist the
            # latest visible magnetic values.  The bounded wait below happens
            # off the UI thread and gives the final HID worker time to finish.
            flush_thread = self._flush_pending_magnetic_writes()
        except Exception:
            pass
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            self.tray.stop()
        except Exception:
            pass

        def _shutdown_window_and_exit():
            try:
                self.page.window.prevent_close = False
            except Exception:
                pass
            try:
                self.page.run_task(self.page.window.destroy)
            except Exception:
                try:
                    self.page.run_task(self.page.window.close)
                except Exception:
                    pass
            try:
                self.page.update()
            except Exception:
                pass
            threading.Thread(target=lambda: (time.sleep(0.25), os._exit(0)), daemon=True).start()

        def _shutdown_after_magnetic_flush():
            # The calibration worker, not this UI callback, owns Womier's
            # single 0x1E/0 stop packet.  Before an explicit application exit
            # give that worker a short, bounded chance to finish so ``os._exit``
            # cannot cut it off in the ordinary tray-quit path.
            if calibration_session is not None:
                try:
                    cleaned = self._wait_for_magnetic_calibration_cleanup(
                        calibration_session, timeout=1.5
                    )
                    if not cleaned:
                        logger.warning("calibration cleanup did not finish before quit timeout")
                except Exception:
                    logger.debug("calibration cleanup wait failed during quit", exc_info=True)
            # A normal final key transaction is a handful of short feature
            # reports.  Do not make closing the app feel stuck if USB is busy
            # with a profile batch: the wait is deliberately bounded.
            if flush_thread is not None:
                try:
                    flush_thread.join(timeout=1.25)
                except Exception:
                    pass
            try:
                self._wait_for_pending_magnetic_writes(timeout=0.25)
            except Exception:
                pass
            try:
                # A final HID worker may have just queued its configuration
                # durability timer.  Commit it before the explicit cache
                # drain / process exit rather than letting the daemon timer
                # be cut short by ``os._exit``.
                self._flush_magnetic_persistence()
            except Exception:
                logger.exception("final magnetic persistence flush failed")
            try:
                # A final HID flush can have queued its Womier mirror only a
                # moment ago.  Drain it before the process exits instead of
                # letting a daemon timer be cut off by ``os._exit``.
                if getattr(self, "_womier_cache_sync_lock", None) is not None:
                    self._flush_womier_cache_sync()
            except Exception:
                logger.exception("final Womier cache sync failed")
            # This is the one intentional UI action after ``app_alive`` was
            # cleared: it tears down the native shell.  All ordinary late
            # worker/tray callbacks remain rejected by ``_ui_call``.
            self._ui_call(_shutdown_window_and_exit, allow_shutdown=True)

        threading.Thread(
            target=_shutdown_after_magnetic_flush,
            daemon=True,
            name="magnetic-quit-drain",
        ).start()

    def _handle_window_event(self, e):
        if not getattr(self, "app_alive", True):
            return
        evt_type = getattr(e, "type", None)
        evt_value = getattr(evt_type, "value", evt_type)
        evt_str = str(evt_value) if evt_value is not None else getattr(e, "data", "")
        if evt_str in ("close", "WindowEventType.CLOSE") or evt_type == getattr(ft, "WindowEventType", type("x", (), {})).CLOSE:
            self._hide_window()

    # ---------- Utilities ----------
    def _snack(self, text):
        """Show compact in-app feedback without a bright modal-looking panel."""
        message = str(text or "")
        toast = ft.SnackBar(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.INFO_OUTLINE_ROUNDED, size=17, color=ft.Colors.PRIMARY),
                    ft.Container(
                        content=ft.Text(
                            message,
                            size=12,
                            color=ft.Colors.ON_SURFACE,
                            max_lines=2,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        width=350,
                    ),
                ],
                spacing=8,
                tight=True,
            ),
            behavior=ft.SnackBarBehavior.FLOATING,
            dismiss_direction=ft.DismissDirection.DOWN,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            duration=2400,
            width=420,
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            elevation=8,
            shape=ft.RoundedRectangleBorder(radius=14),
        )
        try:
            self.page.show_dialog(toast)
        except Exception:
            # Notifications must never disrupt a copy/import action when a
            # page is closing or a portable runtime has no active overlay.
            logger.debug("could not show in-app toast", exc_info=True)


FORCE_VISIBLE_LAUNCH = False


def main(page: ft.Page):
    QMKManager(page, force_visible=FORCE_VISIBLE_LAUNCH)


def _should_start_minimized() -> bool:
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return bool(data.get("settings", {}).get("start_minimized", False))
    except Exception:
        pass
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--startup", action="store_true",
                        help="Launched by Windows autostart")
    parser.add_argument(
        "--show",
        action="store_true",
        help="Open visibly once without changing the tray-start setting",
    )
    args = parser.parse_args()
    FORCE_VISIBLE_LAUNCH = bool(args.show)

    if not acquire_single_instance():
        bring_existing_to_front()
        sys.exit(0)

    os.chdir(paths.app_dir)

    is_startup = args.startup

    if is_startup:
        config_data = {}
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
        except Exception:
            pass
        delay = config_data.get("settings", {}).get("startup_delay_sec", 5)
        if delay > 0:
            time.sleep(delay)

    if is_startup or (_should_start_minimized() and not args.show):
        ft.run(main, view=ft.AppView.FLET_APP_HIDDEN)
    else:
        ft.run(main)
