"""Windows integration helpers used by QMK.Top Manager for SK75 TMR.

The upstream repository imports this module but does not include it.  Keeping
the integration small and explicit lets the app run both from source and from
a PyInstaller executable.
"""
from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "QMK.Top Manager for SK75 TMR"
# This is already SK75-specific and deliberately remains stable between
# public releases.  That prevents an old and a newly renamed EXE from opening
# together and competing for the same keyboard.  The *data* directory below
# is what gives the public app clean state distinct from a generic manager.
APP_ID = "QMK.TopManager.SK75TMR"
APP_DATA_DIRECTORY_NAME = APP_NAME
DATA_DIRECTORY_OVERRIDE_ENV = "QMK_TOP_MANAGER_FOR_SK75_TMR_DATA_DIR"
CONFIG_FILE_NAME = "profiles_config.json"
LOG_FILE_NAME = "qmk_top_manager_for_sk75_tmr.log"
_MUTEX_HANDLE = None


@dataclass(frozen=True)
class AppPaths:
    app_dir: Path
    data_dir: Path
    config_path: Path
    log_path: Path
    startup_shortcut: Path


def _application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _startup_directory() -> Path:
    app_data = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    return app_data / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _user_data_directory() -> Path:
    """Return the writable per-user state directory for source and EXE runs.

    A release EXE can live in ``Program Files``, on a read-only Downloads
    directory, or beside the original manager.  Keeping mutable JSON beside
    the executable used to make first run depend on the publisher's local
    ``profiles_config.json`` and could fail with ``Access denied``.  Public
    builds therefore always use an isolated LocalAppData directory.

    ``QMK_TOP_MANAGER_FOR_SK75_TMR_DATA_DIR`` is intentionally an opt-in
    override for portable/testing use.  It is not set by the application and
    never points at another manager's installation by default.  The former,
    generic override name is deliberately not read: a public SK75 TMR build
    must start with separate, clean state even beside an older manager.
    """
    override = os.environ.get(DATA_DIRECTORY_OVERRIDE_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / APP_DATA_DIRECTORY_NAME


_app_dir = _application_directory()
_data_dir = _user_data_directory()
paths = AppPaths(
    app_dir=_app_dir,
    data_dir=_data_dir,
    config_path=_data_dir / CONFIG_FILE_NAME,
    log_path=_data_dir / LOG_FILE_NAME,
    startup_shortcut=_startup_directory() / f"{APP_NAME}.lnk",
)


def _startup_target() -> tuple[str, str]:
    if getattr(sys, "frozen", False):
        return sys.executable, "--startup"
    entrypoint = paths.app_dir / "app_flet.py"
    return sys.executable, f'"{entrypoint}" --startup'


def autostart_enabled() -> bool:
    return paths.startup_shortcut.is_file()


def set_autostart(enabled: bool) -> None:
    """Create or remove the current-app Startup shortcut."""
    shortcut_path = paths.startup_shortcut
    if not enabled:
        try:
            shortcut_path.unlink(missing_ok=True)
        except OSError:
            pass
        return

    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    target, arguments = _startup_target()
    try:
        from win32com.client import Dispatch

        shell = Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(str(shortcut_path))
        shortcut.TargetPath = target
        shortcut.Arguments = arguments
        shortcut.WorkingDirectory = str(paths.app_dir)
        shortcut.Description = APP_NAME
        shortcut.save()
    except Exception as exc:
        raise RuntimeError(f"Unable to configure Windows autostart: {exc}") from exc


def acquire_single_instance() -> bool:
    """Acquire a per-user Windows mutex and retain the handle for process life."""
    global _MUTEX_HANDLE
    if _MUTEX_HANDLE is not None:
        return True
    _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(
        None, False, f"Local\\{APP_ID}"
    )
    if not _MUTEX_HANDLE:
        return True
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def bring_existing_to_front() -> None:
    """Best-effort activation of the already running desktop window."""
    try:
        import win32con
        import win32gui

        def activate(hwnd, _):
            # Match only this public SK75 companion.  The original generic
            # manager may still be installed, but bringing *it* forward after
            # a second launch of this EXE would be confusing and would hide
            # the new application's own window.
            title = win32gui.GetWindowText(hwnd)
            # A tray-started Flet shell is deliberately hidden.  EnumWindows
            # still returns it, and ``SW_RESTORE`` is precisely the native
            # operation that makes it visible again, so do not reject that
            # expected hidden state here.
            if title == APP_NAME or title.startswith(APP_NAME):
                # ``SW_RESTORE`` only un-minimises; Flet's tray mode can make
                # the native window fully hidden instead.  Show it first, then
                # restore in case it was also minimised.
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)

        win32gui.EnumWindows(activate, None)
    except Exception:
        pass
