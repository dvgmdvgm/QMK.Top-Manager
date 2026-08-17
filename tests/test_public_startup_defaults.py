"""Public-release startup defaults must be safe for a fresh SK75 install."""

import json
import os
import sys
from pathlib import Path


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app_flet as app_module
from app_flet import QMKManager
from autostart import APP_ID


def _load_config_at(path, monkeypatch):
    monkeypatch.setattr(app_module, "CONFIG_FILE", path)
    manager = QMKManager.__new__(QMKManager)
    return manager.load_config(include_external_migrations=False)


def test_fresh_public_configuration_does_not_start_profile_service(tmp_path, monkeypatch):
    """A new public data folder must not start the removed automatic service."""
    config_path = tmp_path / "profiles_config.json"

    config = _load_config_at(config_path, monkeypatch)

    assert config["settings"]["autostart_service"] is False
    assert json.loads(config_path.read_text(encoding="utf-8"))["settings"]["autostart_service"] is False


def test_existing_explicit_profile_service_choice_is_preserved(tmp_path, monkeypatch):
    """Changing the fresh default must never silently disable an old choice."""
    config_path = tmp_path / "profiles_config.json"
    config_path.write_text(
        json.dumps({"settings": {"autostart_service": True}}),
        encoding="utf-8",
    )

    config = _load_config_at(config_path, monkeypatch)

    assert config["settings"]["autostart_service"] is True
    assert json.loads(config_path.read_text(encoding="utf-8"))["settings"]["autostart_service"] is True


def test_taskbar_identity_matches_the_public_sk75_application_identity():
    """The public EXE must not group itself with the old generic manager."""
    source_text = (Path(__file__).resolve().parents[1] / "app_flet.py").read_text(
        encoding="utf-8"
    )

    assert APP_ID == "QMK.TopManager.SK75TMR"
    assert "SetCurrentProcessExplicitAppUserModelID(APP_ID)" in source_text
    assert 'SetCurrentProcessExplicitAppUserModelID("QMK.Top.Manager.1")' not in source_text
