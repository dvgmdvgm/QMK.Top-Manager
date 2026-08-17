"""Regression checks for the public, clean SK75 release artifact."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_pyinstaller_spec_bundles_icon_but_not_user_state():
    """A GitHub release must not accidentally ship the developer's config."""
    spec = (ROOT / "QMK.Top Manager.spec").read_text(encoding="utf-8")

    assert (ROOT / "assets" / "qmk-top-manager-keyboard.ico").is_file()
    assert "assets/qmk-top-manager-keyboard.ico" in spec
    assert "name='QMK.Top Manager for SK75 TMR'" in spec
    assert "profiles_config.json" not in spec
    assert "qmk_top_manager.log" not in spec


def test_release_paths_do_not_embed_the_developers_machine_layout():
    """Only environment-backed per-user locations may be used at runtime."""
    runtime_source = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in ("autostart.py", "app_flet.py", "womier_import.py")
    )

    assert "C:\\QMK.Top.Manager" not in runtime_source
    assert "C:\\Users\\Titan" not in runtime_source
    assert "QMK_TOP_MANAGER_FOR_SK75_TMR_DATA_DIR" in runtime_source
    assert "QMK_TOP_MANAGER_DATA_DIR" not in runtime_source
    assert "QMK_WOMIER_DRIVER_EXE" in runtime_source


def test_public_artifact_uses_the_same_sk75_tmr_identity_everywhere():
    """The release filename, metadata and app data namespace stay aligned."""
    build_script = (ROOT / "build_release.ps1").read_text(encoding="utf-8")
    version_info = (ROOT / "version_info.txt").read_text(encoding="utf-8")
    autostart_source = (ROOT / "autostart.py").read_text(encoding="utf-8")

    identity = "QMK.Top Manager for SK75 TMR"
    assert identity in build_script
    assert f"{identity}.exe" in build_script
    assert identity in version_info
    assert "APP_DATA_DIRECTORY_NAME = APP_NAME" in autostart_source
    assert "QMK.TopManager.SK75TMR" in autostart_source


def test_public_build_script_emits_a_checksum_for_github_release_uploads():
    """A release asset needs a reproducible integrity value beside the EXE."""
    script = (ROOT / "build_release.ps1").read_text(encoding="utf-8")

    assert "Get-FileHash" in script
    assert ".exe.sha256" in script
