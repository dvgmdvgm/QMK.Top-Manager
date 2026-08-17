"""Regression coverage for the closed-driver official Womier cache sync."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

import womier_import as womier
from magnetic import KeyMagneticSettings, SK75_KEYS


def _write_log_record(path: Path, payload: bytes) -> None:
    with path.open("wb") as handle:
        womier._append_leveldb_logical_record(handle, payload)


def _storage_fixture(tmp_path: Path) -> tuple[Path, int, bytes]:
    """Build a tiny but native-formatted Chromium LevelDB directory."""
    db_dir = tmp_path / "Local Storage" / "leveldb"
    db_dir.mkdir(parents=True)
    (db_dir / "CURRENT").write_text("MANIFEST-000001\n", encoding="ascii")
    manifest = bytes((2,)) + womier._encode_varint(7) + bytes((4,)) + womier._encode_varint(1)
    _write_log_record(db_dir / "MANIFEST-000001", manifest)

    keys = [key for key in SK75_KEYS if key.hid is not None][:12]
    assert len(keys) == 12
    modes = [
        {
            "mode": "normal",
            "option": "normal",
            "original": key.hid,
            "index": 0,
            "travel": 1.2,
            "liftTravel": 2.8,
            "fire": True,
            "firePressTravel": 0.3,
            "fireLiftTravel": 0.3,
            "topDeadZoneTravel": 0.0,
            "deadZoneTravel": 0.3,
            "轴体": "玄磁轴",
        }
        for key in keys
    ]
    # Make the rewrite larger than one WAL block, proving the fragmentation
    # branch used for Womier's real ~230 KB JSON value.
    data = {
        "configs": [],
        "fnConfigs": [],
        "padding": "x" * 18_000,
        "磁轴": [
            {"profile": profile, "modes": [dict(item) for item in modes]}
            for profile in range(4)
        ],
    }
    raw_key = b"_file://\x00\x01DeviceTest_02_2518"
    payload = womier._build_leveldb_write_batch(
        1, [(raw_key, womier._encode_womier_json_value(data))]
    )
    _write_log_record(db_dir / "000007.log", payload)
    return db_dir, keys[0].slot, raw_key


def test_closed_driver_sync_updates_only_selected_womier_profile_and_syncs_normal_deactivation(tmp_path, monkeypatch):
    db_dir, slot, raw_key = _storage_fixture(tmp_path)
    monkeypatch.setattr(womier, "is_womier_driver_running", lambda: False)

    result = womier.sync_womier_magnetic_cache(
        2,
        {
            slot: KeyMagneticSettings(
                actuation=0.45,
                rapid_trigger=True,
                rapid_press=0.12,
                rapid_release=0.56,
                lower_dead_zone=0.11,
                upper_dead_zone=0.22,
                deactivation=1.67,
            )
        },
        key_modes={slot: 0x80},
        rt_stab=50,
        leveldb_dir=db_dir,
    )

    assert result.synced is True
    assert result.deferred is False
    assert result.changed_values == 1
    assert result.backup_dir is not None and result.backup_dir.is_dir()
    assert (result.backup_dir / "000007.log").is_file()
    assert (result.backup_dir / womier._WOMIER_BACKUP_MARKER_NAME).is_file()

    records = womier.read_womier_storage(db_dir)
    payload = records["DeviceTest_02_2518"]
    profiles = {profile["profile"]: profile for profile in payload["磁轴"]}
    target = next(item for item in profiles[2]["modes"] if item["original"] == 41)
    untouched = next(item for item in profiles[1]["modes"] if item["original"] == 41)
    assert target["travel"] == 0.45
    assert target["liftTravel"] == 1.67
    assert target["firePressTravel"] == 0.12
    assert target["fireLiftTravel"] == 0.56
    assert target["deadZoneTravel"] == 0.11
    assert target["topDeadZoneTravel"] == 0.22
    assert untouched["travel"] == 1.2

    latest = womier._read_latest_leveldb_entries(db_dir)
    prefix = raw_key[: -len(b"DeviceTest_02_2518")]
    assert latest[prefix + b"2518_RTStab_value"][1] == b"\x0150"
    assert latest[prefix + b"2518_RTStab_open"][1] == b"\x01true"


def test_sync_defers_without_touching_leveldb_when_official_driver_is_running(tmp_path, monkeypatch):
    db_dir, slot, _raw_key = _storage_fixture(tmp_path)
    before = (db_dir / "000007.log").read_bytes()
    monkeypatch.setattr(womier, "is_womier_driver_running", lambda: True)

    result = womier.sync_womier_magnetic_cache(
        0,
        {
            slot: {
                "actuation": 0.5,
                "rapid_trigger": True,
                "rapid_press": 0.2,
                "rapid_release": 0.2,
                "lower_dead_zone": 0.0,
                "upper_dead_zone": 0.0,
            }
        },
        leveldb_dir=db_dir,
    )

    assert result.synced is False
    assert result.deferred is True
    assert (db_dir / "000007.log").read_bytes() == before
    assert not (db_dir.parent / "qmk-top-manager-for-sk75-tmr-womier-backups").exists()


def test_known_iot_helper_is_treated_as_a_womier_cache_owner(monkeypatch):
    """Do not append to the cache while either stock helper is still alive."""
    helper = SimpleNamespace(
        info={
            "name": "iot_driver_v210.exe",
            "exe": str(womier.WOMIER_IOT_DRIVER_V210_EXE),
            "cmdline": [str(womier.WOMIER_IOT_DRIVER_V210_EXE)],
        }
    )
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: iter((helper,)))

    assert womier.is_womier_driver_running() is True


def test_manifest_fixture_is_a_real_write_batch_and_reopens_through_read_only_import(tmp_path):
    db_dir, _slot, _raw_key = _storage_fixture(tmp_path)
    assert womier._leveldb_manifest_state(db_dir) == (7, 1)
    loaded = womier.read_womier_storage(db_dir)
    assert list(loaded) == ["DeviceTest_02_2518"]
    assert json.loads(json.dumps(loaded))["DeviceTest_02_2518"]["磁轴"][0]["profile"] == 0


def test_crc32c_matches_leveldb_reference_vector():
    # Castagnoli CRC32C for this vector is published by RFC 3720 and is the
    # checksum native LevelDB validates before it accepts a WAL fragment.
    assert womier._crc32c(b"123456789") == 0xE3069283
    assert womier._masked_crc32c(0xE3069283) == 0xC78AB0E5


def test_options_only_sync_does_not_rewrite_unchanged_magnetic_json(tmp_path, monkeypatch):
    db_dir, _slot, raw_key = _storage_fixture(tmp_path)
    monkeypatch.setattr(womier, "is_womier_driver_running", lambda: False)
    before = womier._read_latest_leveldb_entries(db_dir)[raw_key][1]

    result = womier.sync_womier_magnetic_cache(
        0, {}, rt_stab=25, leveldb_dir=db_dir
    )

    assert result.synced is True
    assert result.changed_values == 0
    latest = womier._read_latest_leveldb_entries(db_dir)
    assert latest[raw_key][1] == before
    prefix = raw_key[: -len(b"DeviceTest_02_2518")]
    assert latest[prefix + b"2518_RTStab_value"][1] == b"\x0125"


def test_partial_wal_append_is_rolled_back_before_lock_release(tmp_path, monkeypatch):
    db_dir, slot, _raw_key = _storage_fixture(tmp_path)
    monkeypatch.setattr(womier, "is_womier_driver_running", lambda: False)
    log_path = db_dir / "000007.log"
    before = log_path.read_bytes()

    def interrupted_append(handle, _payload):
        handle.write(b"incomplete")
        raise OSError("simulated disk interruption")

    monkeypatch.setattr(womier, "_append_leveldb_logical_record", interrupted_append)
    with pytest.raises(womier.WomierCacheSyncError):
        womier.sync_womier_magnetic_cache(
            0,
            {
                slot: KeyMagneticSettings(
                    actuation=0.50,
                    rapid_trigger=True,
                    rapid_press=0.20,
                    rapid_release=0.20,
                    lower_dead_zone=0.0,
                    upper_dead_zone=0.0,
                )
            },
            leveldb_dir=db_dir,
        )
    assert log_path.read_bytes() == before


def test_failed_post_write_verification_is_rolled_back(tmp_path, monkeypatch):
    db_dir, slot, _raw_key = _storage_fixture(tmp_path)
    monkeypatch.setattr(womier, "is_womier_driver_running", lambda: False)
    log_path = db_dir / "000007.log"
    before = log_path.read_bytes()
    original_reader = womier._read_latest_leveldb_entries
    calls = 0

    def fail_only_after_append(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise womier.WomierCacheSyncError("simulated verification failure")
        return original_reader(path)

    monkeypatch.setattr(womier, "_read_latest_leveldb_entries", fail_only_after_append)
    with pytest.raises(womier.WomierCacheSyncError, match="verification failure"):
        womier.sync_womier_magnetic_cache(
            0,
            {
                slot: KeyMagneticSettings(
                    actuation=0.50,
                    rapid_trigger=True,
                    rapid_press=0.20,
                    rapid_release=0.20,
                    lower_dead_zone=0.0,
                    upper_dead_zone=0.0,
                )
            },
            leveldb_dir=db_dir,
        )
    assert log_path.read_bytes() == before


def test_backup_retention_prunes_only_new_manager_owned_backups(tmp_path, monkeypatch):
    """Future cache syncs stay bounded without deleting old user history."""
    parent = tmp_path / "qmk-top-manager-for-sk75-tmr-womier-backups"
    parent.mkdir()

    def managed(name: str, stamp: int) -> Path:
        directory = parent / name
        directory.mkdir()
        (directory / womier._WOMIER_BACKUP_MARKER_NAME).write_text(
            json.dumps(
                {
                    "format": womier._WOMIER_BACKUP_FORMAT,
                    "version": womier._WOMIER_BACKUP_VERSION,
                }
            ),
            encoding="utf-8",
        )
        (directory / "000007.log").write_bytes(b"x" * 32)
        os.utime(directory, ns=(stamp, stamp))
        return directory

    old = managed("20260817-120000-000001-deadbeef", 10)
    middle = managed("20260817-120000-000002-deadbeef", 20)
    newest = managed("20260817-120000-000003-deadbeef", 30)
    # An unmarked old backup may have been made by an older release.  It is
    # never touched by automatic retention.
    legacy = parent / "20260817-120000-000004-deadbeef"
    legacy.mkdir()
    (legacy / "000007.log").write_bytes(b"legacy")

    monkeypatch.setattr(womier, "_MAX_WOMIER_BACKUP_COUNT", 2)
    monkeypatch.setattr(womier, "_MAX_WOMIER_BACKUP_TOTAL_BYTES", 1_000)
    womier._prune_managed_womier_backups(parent, keep=newest)

    assert not old.exists()
    assert middle.exists()
    assert newest.exists()
    assert legacy.exists()
