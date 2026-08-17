"""Regression checks for the public configuration-transfer documentation."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_readme_documents_selectable_cfg_categories_and_new_data_directory():
    """Public docs must match the selective CFG dialog, not a full backup."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "%LOCALAPPDATA%\\QMK.Top Manager for SK75 TMR\\profiles_config.json" in readme
    assert "старая папка `%LOCALAPPDATA%\\QMK.Top Manager` не используется" in readme
    assert "legacy `%LOCALAPPDATA%\\QMK.Top Manager` folder is not used" in readme

    for label in (
        "Перенос CFG",
        "Профили",
        "Lighting Lab",
        "Magnetic Lab",
        "Привязки к процессам",
        "CFG transfer",
        "Profiles",
        "Process bindings",
    ):
        assert label in readme

    for outdated_navigation_claim in (
        "Tab переключает разделы",
        "Tab cycles through sections",
        "Use Tab to switch sections",
    ):
        assert outdated_navigation_claim not in readme
