"""Safety coverage for the explicit stock-Womier close control."""
import os
import sys

import psutil

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app_flet as app_module
from app_flet import QMKManager


class _FakeProcess:
    def __init__(self, pid, name, executable, *, created_at=10.0):
        self.pid = pid
        self.info = {
            "pid": pid,
            "name": name,
            "exe": str(executable),
            "create_time": created_at,
        }
        self._alive = True
        self.terminate_calls = 0
        self.kill_calls = 0

    def _ensure_alive(self):
        if not self._alive:
            raise psutil.NoSuchProcess(self.pid)

    def name(self):
        self._ensure_alive()
        return self.info["name"]

    def exe(self):
        self._ensure_alive()
        return self.info["exe"]

    def create_time(self):
        self._ensure_alive()
        return self.info["create_time"]

    def terminate(self):
        self._ensure_alive()
        self.terminate_calls += 1
        self._alive = False

    def kill(self):
        self._ensure_alive()
        self.kill_calls += 1
        self._alive = False


class _FakeControl:
    """Tiny mounted-control stand-in for action busy-state tests."""

    def __init__(self):
        self.disabled = False
        self.color = None

    def update(self):
        pass


def test_stock_womier_close_targets_only_exact_name_and_installed_path(monkeypatch, tmp_path):
    install = tmp_path / "WOMIER Driver"
    driver_path = install / "WOMIER Driver.exe"
    iot_path = install / "resources" / "app" / "iot_driver.exe"
    monkeypatch.setattr(
        app_module,
        "WOMIER_DRIVER_PROCESS_TARGETS",
        (
            ("Womier Driver", "womier driver.exe", driver_path),
            ("Womier iot_driver", "iot_driver.exe", iot_path),
        ),
    )
    driver = _FakeProcess(401, "WOMIER Driver.exe", driver_path)
    iot = _FakeProcess(402, "iot_driver.exe", iot_path)
    same_name_elsewhere = _FakeProcess(
        403, "WOMIER Driver.exe", tmp_path / "elsewhere" / "WOMIER Driver.exe"
    )
    same_path_wrong_name = _FakeProcess(404, "not-womier.exe", driver_path)
    monkeypatch.setattr(
        app_module.psutil,
        "process_iter",
        lambda _attrs: iter((driver, iot, same_name_elsewhere, same_path_wrong_name)),
    )

    result = app_module._close_exact_womier_driver_processes(graceful_timeout=0)

    assert [match.pid for match in result.found] == [401, 402]
    assert [match.pid for match in result.closed] == [401, 402]
    assert result.remaining == ()
    assert result.skipped == ()
    assert result.errors == ()
    assert driver.terminate_calls == 1
    assert iot.terminate_calls == 1
    assert driver.kill_calls == 0
    assert iot.kill_calls == 0
    assert same_name_elsewhere.terminate_calls == 0
    assert same_name_elsewhere.kill_calls == 0
    assert same_path_wrong_name.terminate_calls == 0
    assert same_path_wrong_name.kill_calls == 0


def test_stock_womier_close_rechecks_identity_before_force_kill(monkeypatch, tmp_path):
    driver_path = tmp_path / "WOMIER Driver.exe"
    monkeypatch.setattr(
        app_module,
        "WOMIER_DRIVER_PROCESS_TARGETS",
        (("Womier Driver", "womier driver.exe", driver_path),),
    )

    class ReusedAfterTerminate(_FakeProcess):
        def terminate(self):
            self._ensure_alive()
            self.terminate_calls += 1
            # Simulate a PID reused by an unrelated same-name process at a
            # different path before the force-kill phase.  It must be skipped.
            self.info["exe"] = str(tmp_path / "unrelated" / "WOMIER Driver.exe")

    process = ReusedAfterTerminate(501, "WOMIER Driver.exe", driver_path)
    monkeypatch.setattr(app_module.psutil, "process_iter", lambda _attrs: iter((process,)))

    result = app_module._close_exact_womier_driver_processes(graceful_timeout=0)

    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert [match.pid for match in result.skipped] == [501]
    assert result.closed == ()


def test_stock_womier_close_accepts_only_the_exact_roaming_v210_helper(monkeypatch, tmp_path):
    roaming = tmp_path / "AppData" / "Roaming" / "WOMIER Driver"
    helper_path = roaming / "iot_driver_v210.exe"
    monkeypatch.setattr(
        app_module,
        "WOMIER_DRIVER_PROCESS_TARGETS",
        (("Womier iot_driver_v210", "iot_driver_v210.exe", helper_path),),
    )
    exact = _FakeProcess(601, "iot_driver_v210.exe", helper_path)
    same_name_elsewhere = _FakeProcess(
        602, "iot_driver_v210.exe", tmp_path / "elsewhere" / "iot_driver_v210.exe"
    )
    monkeypatch.setattr(
        app_module.psutil,
        "process_iter",
        lambda _attrs: iter((exact, same_name_elsewhere)),
    )

    result = app_module._close_exact_womier_driver_processes(graceful_timeout=0)

    assert [match.pid for match in result.closed] == [601]
    assert exact.terminate_calls == 1
    assert same_name_elsewhere.terminate_calls == 0


def test_womier_close_ui_is_confirmed_and_above_travel_check_in_navigation():
    source = __import__("inspect").getsource(QMKManager._build_ui)
    assert "self.womier_driver_open_nav_button" in source
    assert "self.womier_driver_actions_row" in source
    assert 'self.womier_driver_open_label = ft.Text(\n            "Открыть"' in source
    assert 'self.womier_driver_close_label = ft.Text(\n            "Закрыть"' in source
    assert "self._confirm_close_womier_driver_processes()" in source
    rail = source.index("self.womier_driver_actions_row,")
    tester = source.index("self.magnetic_tester_nav_button,", rail)
    assert rail < tester
    assert "magnetic_calibration_nav_button" not in source

    confirm_source = __import__("inspect").getsource(
        QMKManager._confirm_close_womier_driver_processes
    )
    assert "Закрыть процессы" in confirm_source
    assert "точным именем и путём" in confirm_source
    assert "_close_womier_driver_processes_after_confirmation" in confirm_source


def test_womier_driver_action_labels_dim_with_their_busy_tiles():
    manager = QMKManager.__new__(QMKManager)
    manager.womier_driver_open_nav_button = _FakeControl()
    manager.womier_driver_open_label = _FakeControl()
    manager.womier_driver_close_nav_button = _FakeControl()
    manager.womier_driver_close_label = _FakeControl()

    manager._set_womier_driver_open_busy(True)
    manager._set_womier_driver_close_busy(True)

    assert manager.womier_driver_open_nav_button.disabled is True
    assert manager.womier_driver_open_label.color == app_module.ft.Colors.ON_SURFACE_VARIANT
    assert manager.womier_driver_close_nav_button.disabled is True
    assert manager.womier_driver_close_label.color == app_module.ft.Colors.ON_SURFACE_VARIANT

    manager._set_womier_driver_open_busy(False)
    manager._set_womier_driver_close_busy(False)

    assert manager.womier_driver_open_nav_button.disabled is False
    assert manager.womier_driver_open_label.color == app_module.ft.Colors.PRIMARY
    assert manager.womier_driver_close_nav_button.disabled is False
    assert manager.womier_driver_close_label.color == app_module.ft.Colors.ERROR


def test_womier_open_starts_only_the_canonical_installed_executable(monkeypatch, tmp_path):
    executable = tmp_path / "WOMIER Driver.exe"
    executable.touch()
    calls = []
    monkeypatch.setattr(app_module, "WOMIER_DRIVER_EXE", executable)
    monkeypatch.setattr(
        app_module.os,
        "startfile",
        lambda value: calls.append(value),
        raising=False,
    )

    opened, message = app_module._launch_exact_womier_driver()

    assert opened is True
    assert "открыт" in message
    assert calls == [str(executable)]


def test_womier_open_refuses_when_the_canonical_executable_is_missing(monkeypatch, tmp_path):
    missing = tmp_path / "WOMIER Driver.exe"
    monkeypatch.setattr(app_module, "WOMIER_DRIVER_EXE", missing)

    opened, message = app_module._launch_exact_womier_driver()

    assert opened is False
    assert "не найден" in message
