import importlib
import sys
import inspect
from types import SimpleNamespace

import autostart
from app_flet import QMKManager


def test_second_launch_raises_current_sk75_window(monkeypatch):
    """A current descriptive title must not leave the one instance hidden."""
    restored = []
    foreground = []
    titles = {
        1: "QMK.Top Manager",
        2: "QMK.Top Manager for SK75 TMR",
        3: "Unrelated application",
    }
    fake_gui = SimpleNamespace(
        IsWindowVisible=lambda _hwnd: True,
        GetWindowText=lambda hwnd: titles[hwnd],
        ShowWindow=lambda hwnd, command: restored.append((hwnd, command)),
        SetForegroundWindow=lambda hwnd: foreground.append(hwnd),
        EnumWindows=lambda callback, data: [callback(hwnd, data) for hwnd in titles],
    )
    fake_con = SimpleNamespace(SW_SHOW=5, SW_RESTORE=9)
    monkeypatch.setitem(sys.modules, "win32gui", fake_gui)
    monkeypatch.setitem(sys.modules, "win32con", fake_con)

    autostart.bring_existing_to_front()

    assert restored == [(2, 5), (2, 9)]
    assert foreground == [2]


def test_public_paths_are_per_user_and_never_next_to_the_executable(tmp_path, monkeypatch):
    """Fresh public releases must not inherit the publisher's local config."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.delenv(autostart.DATA_DIRECTORY_OVERRIDE_ENV, raising=False)
    # A generic-manager override must not leak an old user's personal state
    # into a fresh SK75 TMR public release.
    monkeypatch.setenv("QMK_TOP_MANAGER_DATA_DIR", str(tmp_path / "old-manager"))

    module = importlib.reload(autostart)
    try:
        assert module.paths.data_dir == tmp_path / "local" / "QMK.Top Manager for SK75 TMR"
        assert module.paths.config_path == module.paths.data_dir / "profiles_config.json"
        assert module.paths.config_path.parent != module.paths.app_dir
        assert module.paths.log_path == module.paths.data_dir / "qmk_top_manager_for_sk75_tmr.log"
        assert module.paths.startup_shortcut.name == "QMK.Top Manager for SK75 TMR.lnk"
    finally:
        # Restore module-level paths for the rest of the test process.
        monkeypatch.undo()
        importlib.reload(module)


def test_sk75_specific_data_override_is_opt_in_and_isolated(tmp_path, monkeypatch):
    """Only the explicitly named SK75 override can replace LocalAppData."""
    override = tmp_path / "portable-sk75-state"
    monkeypatch.setenv(autostart.DATA_DIRECTORY_OVERRIDE_ENV, str(override))
    monkeypatch.setenv("QMK_TOP_MANAGER_DATA_DIR", str(tmp_path / "generic-state"))

    module = importlib.reload(autostart)
    try:
        assert module.paths.data_dir == override.resolve()
        assert module.paths.config_path == override.resolve() / module.CONFIG_FILE_NAME
        assert module.paths.log_path == override.resolve() / module.LOG_FILE_NAME
    finally:
        monkeypatch.undo()
        importlib.reload(module)


def test_explicit_visible_launch_overrides_start_minimized_shell_setup():
    """``app_flet.py --show`` must not create another hidden Flet window."""
    source = inspect.getsource(QMKManager._build_page)
    assert "and not self.force_visible" in source
