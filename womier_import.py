"""Import and closed-driver cache synchronization for Womier magnetic settings.

The official Womier desktop driver keeps the per-profile magnetic settings in
Chromium's Local Storage LevelDB rather than exposing all profiles through the
keyboard HID read command.  Import helpers are read-only.  The narrowly scoped
sync helper only appends a native LevelDB WriteBatch after a successful HID
write, with the official driver closed, its exclusive LOCK acquired, a backup,
an fsync, and a parse/CRC verification.  It never starts the official driver
and never sends HID packets.

Only the small, stable subset of LevelDB needed by Chromium Local Storage is
implemented here.  Keeping it in Python avoids a native LevelDB dependency in
the packaged driver.  Malformed, locked, or concurrently modified databases
simply result in no import candidate rather than partial settings.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import struct
import threading
from typing import Iterable, Iterator, Mapping
import uuid

from magnetic import (
    KeyMagneticSettings,
    MagneticProtocol,
    MagneticProtocolError,
    SK75_KEY_BY_HID,
    SK75_KEY_BY_SLOT,
)


def _windows_user_directory(variable: str, fallback_leaf: str) -> Path:
    """Use Windows' per-user directory variables, with a portable fallback."""
    raw = os.environ.get(variable, "").strip()
    if raw:
        return Path(raw)
    return Path.home() / "AppData" / fallback_leaf


def _configured_path(variable: str) -> Path | None:
    raw = os.environ.get(variable, "").strip()
    return Path(raw).expanduser() if raw else None


def _first_existing_or_default(candidates: Iterable[Path]) -> Path:
    """Choose a known installer location without recursively scanning disks."""
    candidate_list = tuple(candidates)
    for candidate in candidate_list:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return candidate_list[0]


_ROAMING_APP_DATA = _windows_user_directory("APPDATA", "Roaming")
_LOCAL_APP_DATA = _windows_user_directory("LOCALAPPDATA", "Local")
WOMIER_DRIVER_LEVELDB = (
    _configured_path("QMK_WOMIER_LEVELDB")
    or _ROAMING_APP_DATA / "WOMIER Driver" / "Local Storage" / "leveldb"
)
WOMIER_STORAGE_KEY_PREFIX = "DeviceTest_02_"
WOMIER_DRIVER_EXE = _first_existing_or_default(
    (
        _configured_path("QMK_WOMIER_DRIVER_EXE")
        or _LOCAL_APP_DATA / "Programs" / "WOMIER Driver" / "WOMIER Driver.exe",
        _ROAMING_APP_DATA / "WOMIER Driver" / "WOMIER Driver.exe",
        (
            Path(os.environ["ProgramFiles"]) / "WOMIER Driver" / "WOMIER Driver.exe"
            if os.environ.get("ProgramFiles")
            else _LOCAL_APP_DATA / ".missing-program-files-womier.exe"
        ),
        (
            Path(os.environ["ProgramFiles(x86)"]) / "WOMIER Driver" / "WOMIER Driver.exe"
            if os.environ.get("ProgramFiles(x86)")
            else _LOCAL_APP_DATA / ".missing-program-files-x86-womier.exe"
        ),
    )
)
# The stock installation starts a small HID helper beside the Electron
# executable, while newer packages install a standalone helper under Roaming.
# Either process can still be alive while a user has handed the keyboard back
# to the official application.  Treating that state as closed is needlessly
# risky for the Chromium cache mirror: deferring is harmless, whereas a cache
# write that races an official helper can be overwritten on its next flush.
WOMIER_IOT_DRIVER_EXE = (
    WOMIER_DRIVER_EXE.parent / "resources" / "app" / "iot_driver.exe"
)
WOMIER_IOT_DRIVER_V210_EXE = (
    _configured_path("QMK_WOMIER_IOT_DRIVER_EXE")
    or _ROAMING_APP_DATA / "WOMIER Driver" / "iot_driver_v210.exe"
)
WOMIER_CACHE_OWNER_PROCESS_TARGETS = (
    ("womier driver.exe", WOMIER_DRIVER_EXE),
    ("iot_driver.exe", WOMIER_IOT_DRIVER_EXE),
    ("iot_driver_v210.exe", WOMIER_IOT_DRIVER_V210_EXE),
)

_LOGGER = logging.getLogger(__name__)
_WOMIER_CACHE_SYNC_LOCK = threading.RLock()

# The official SK75 record is normally only a few hundred kilobytes.  These
# caps leave a very generous safety margin for future firmware data, while
# preventing a damaged or replaced Chromium cache from making the desktop app
# allocate arbitrary amounts of memory during startup or a background sync.
# They are deliberately local to the on-disk Womier cache; portable app
# configuration has its own, separate import limit in ``app_flet.py``.
_MAX_LEVELDB_FILE_BYTES = 32 * 1024 * 1024
_MAX_LEVELDB_FILES = 512
_MAX_LEVELDB_TOTAL_BYTES = 96 * 1024 * 1024
_MAX_LEVELDB_LOGICAL_RECORD_BYTES = 16 * 1024 * 1024
_MAX_CHROMIUM_JSON_VALUE_BYTES = 16 * 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024

# Cache synchronization can happen repeatedly while a user fine-tunes keys.
# Keep a small, bounded recovery history instead of allowing a new full
# LevelDB copy for every successful quiet-period write to grow forever.
# Keep this application's recovery data distinct from a generic/upstream
# manager that may share the official Womier LevelDB.  Only backups marked by
# this exact public SK75 TMR build are considered for retention or recovery.
_WOMIER_BACKUP_DIR_NAME = "qmk-top-manager-for-sk75-tmr-womier-backups"
_WOMIER_BACKUP_MARKER_NAME = ".qmk-top-manager-for-sk75-tmr-womier-backup.json"
_WOMIER_BACKUP_FORMAT = "qmk-top-manager-for-sk75-tmr-womier-cache-backup"
_WOMIER_BACKUP_VERSION = 1
_MAX_WOMIER_BACKUP_COUNT = 8
_MAX_WOMIER_BACKUP_TOTAL_BYTES = 256 * 1024 * 1024
_WOMIER_BACKUP_DIRECTORY_PATTERN = re.compile(
    r"^\d{8}-\d{6}-\d{6}-[0-9a-f]{8}$", re.IGNORECASE
)


class WomierImportError(ValueError):
    """The official driver's on-disk cache could not be read safely."""


class WomierCacheSyncError(RuntimeError):
    """The official Womier cache could not be safely updated."""


class WomierCacheSyncDeferred(WomierCacheSyncError):
    """The official driver currently owns its cache, so syncing must wait."""


@dataclass(frozen=True)
class WomierCacheSyncResult:
    """Outcome of one closed-driver Womier cache synchronization."""

    synced: bool
    deferred: bool
    detail: str
    storage_key: str | None = None
    changed_values: int = 0
    backup_dir: Path | None = None


@dataclass(frozen=True)
class WomierMagneticImport:
    """A converted four-profile Womier magnetic cache.

    ``profiles`` deliberately uses the application's portable JSON shape so
    the caller can assign it directly to ``entry['magnetic_profiles']``.
    """

    storage_key: str
    profiles: dict[str, dict]
    imported_profile_count: int


def _strict_womier_json_loads(text: str) -> object:
    """Decode one cache JSON value without Python-only number extensions.

    Chromium's ``JSON.parse`` rejects ``NaN`` and ``Infinity``.  Python's
    default decoder accepts them, which meant a damaged cache could be read
    here and then written back in a form the official driver cannot parse.
    Keep the cache boundary strict and reject duplicate object keys too: they
    make a local-storage value ambiguous in exactly the same way as a copied
    app configuration.
    """

    def reject_constant(value: str):
        raise ValueError(f"недопустимое JSON-значение Womier: {value}")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("неконечное число в JSON Womier")
        return parsed

    def object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("повторяющийся ключ в JSON Womier")
            result[key] = value
        return result

    return json.loads(
        text,
        parse_constant=reject_constant,
        parse_float=finite_float,
        object_pairs_hook=object_without_duplicates,
    )


def _checked_leveldb_file_size(path: Path, stat_result=None) -> int:
    """Return a stable-enough file size without following cache symlinks."""
    try:
        # Cache files are expected to be ordinary files.  Refusing a symlink
        # prevents a corrupt user cache from tricking the background writer
        # into reading or appending to an unrelated file.
        if path.is_symlink() or not path.is_file():
            raise WomierImportError("некорректный файл Local Storage Womier")
        stat_result = stat_result if stat_result is not None else path.stat()
    except OSError as exc:
        raise WomierImportError("не удалось проверить файл Local Storage Womier") from exc
    size = int(stat_result.st_size)
    if size < 0 or size > _MAX_LEVELDB_FILE_BYTES:
        raise WomierImportError("файл Local Storage Womier слишком большой")
    return size


def _leveldb_paths(directory: Path, *, strict: bool) -> list[Path]:
    """List bounded ordinary table/WAL files for a safe cache operation."""
    try:
        paths = sorted(directory.glob("*.ldb")) + sorted(directory.glob("*.log"))
    except OSError as exc:
        if strict:
            raise WomierCacheSyncError("не удалось открыть Local Storage Womier") from exc
        return []
    if len(paths) > _MAX_LEVELDB_FILES:
        if strict:
            raise WomierCacheSyncError("в Local Storage Womier слишком много файлов")
        return []
    total = 0
    checked: list[Path] = []
    for path in paths:
        try:
            total += _checked_leveldb_file_size(path)
        except WomierImportError as exc:
            if strict:
                raise WomierCacheSyncError("не удалось безопасно прочитать Local Storage Womier") from exc
            # A concurrent Chromium compaction or an unrelated/reparse file
            # should not make the read-only importer fail as a whole.
            continue
        if total > _MAX_LEVELDB_TOTAL_BYTES:
            if strict:
                raise WomierCacheSyncError("Local Storage Womier слишком большой")
            return []
        checked.append(path)
    return checked


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        # LevelDB varint64 has one bit left at shift 63.  Accepting a larger
        # final byte can manufacture arbitrarily large Python integers which
        # later become fake block lengths or sequence numbers.
        if shift == 63 and byte > 1:
            raise WomierImportError("varint Local Storage выходит за пределы 64 бит")
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise WomierImportError("повреждён varint в Local Storage Womier")


def _snappy_uncompress(data: bytes) -> bytes:
    """Decode the raw Snappy block format used by LevelDB table blocks."""
    expected_length, pos = _read_varint(data, 0)
    if expected_length > _MAX_LEVELDB_LOGICAL_RECORD_BYTES:
        raise WomierImportError("распакованный блок Local Storage слишком большой")
    result = bytearray()
    while pos < len(data) and len(result) < expected_length:
        tag = data[pos]
        pos += 1
        kind = tag & 0x03
        if kind == 0:
            length = tag >> 2
            if length < 60:
                length += 1
            else:
                count = length - 59
                if pos + count > len(data):
                    raise WomierImportError("повреждён Snappy literal")
                length = int.from_bytes(data[pos:pos + count], "little") + 1
                pos += count
            end = pos + length
            if end > len(data):
                raise WomierImportError("обрезан Snappy literal")
            if length > expected_length - len(result):
                raise WomierImportError("Snappy literal выходит за границы блока")
            result.extend(data[pos:end])
            pos = end
            continue

        if kind == 1:
            if pos >= len(data):
                raise WomierImportError("обрезана Snappy copy-1 ссылка")
            length = ((tag >> 2) & 0x07) + 4
            offset = ((tag & 0xE0) << 3) | data[pos]
            pos += 1
        elif kind == 2:
            if pos + 2 > len(data):
                raise WomierImportError("обрезана Snappy copy-2 ссылка")
            length = (tag >> 2) + 1
            offset = int.from_bytes(data[pos:pos + 2], "little")
            pos += 2
        else:
            if pos + 4 > len(data):
                raise WomierImportError("обрезана Snappy copy-4 ссылка")
            length = (tag >> 2) + 1
            offset = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
        if offset <= 0 or offset > len(result):
            raise WomierImportError("некорректная Snappy ссылка")
        if length > expected_length - len(result):
            raise WomierImportError("Snappy ссылка выходит за границы блока")
        # Preserve Snappy's overlapping-copy semantics without appending one
        # byte at a time.  Apart from being faster for a normal cache this
        # avoids a CPU spike when a damaged block advertises a very long copy.
        while length:
            chunk = result[-offset:]
            copy_length = min(length, len(chunk))
            result.extend(chunk[:copy_length])
            length -= copy_length

    if len(result) != expected_length:
        raise WomierImportError("неполный Snappy блок")
    return bytes(result)


def _decode_leveldb_block(raw: bytes) -> list[tuple[bytes, bytes]]:
    """Return the key/value entries of an uncompressed LevelDB block."""
    if len(raw) < 4:
        raise WomierImportError("короткий блок Local Storage")
    restart_count = struct.unpack_from("<I", raw, len(raw) - 4)[0]
    restart_offset = len(raw) - 4 - restart_count * 4
    if restart_offset < 0:
        raise WomierImportError("повреждён блок Local Storage")
    pos = 0
    previous = b""
    entries: list[tuple[bytes, bytes]] = []
    while pos < restart_offset:
        shared, pos = _read_varint(raw, pos)
        non_shared, pos = _read_varint(raw, pos)
        value_len, pos = _read_varint(raw, pos)
        end_key = pos + non_shared
        end_value = end_key + value_len
        if shared > len(previous) or end_value > restart_offset:
            raise WomierImportError("повреждённая запись Local Storage")
        key = previous[:shared] + raw[pos:end_key]
        value = raw[end_key:end_value]
        entries.append((key, value))
        previous = key
        pos = end_value
    return entries


def _read_block(file_data: bytes, offset: int, size: int) -> bytes:
    end = offset + size
    if offset < 0 or size < 0 or end + 5 > len(file_data):
        raise WomierImportError("блок Local Storage выходит за границы файла")
    payload = file_data[offset:end]
    compression = file_data[end]
    # The final four bytes are CRC32C.  The import is read-only, and Chromium
    # can rewrite an SST while we are inspecting it, so bounds + decode checks
    # are more useful here than rejecting a perfectly readable old block whose
    # file changed between stat() calls.
    if compression == 0:
        return payload
    if compression == 1:
        return _snappy_uncompress(payload)
    raise WomierImportError("неизвестное сжатие Local Storage")


def _read_block_handle(data: bytes, offset: int) -> tuple[tuple[int, int], int]:
    block_offset, offset = _read_varint(data, offset)
    size, offset = _read_varint(data, offset)
    return (block_offset, size), offset


def _table_entries(path: Path) -> Iterator[tuple[int, bytes, bytes | None]]:
    """Yield ``(sequence, user_key, value)`` from one LevelDB .ldb file."""
    before = path.stat()
    _checked_leveldb_file_size(path, before)
    data = path.read_bytes()
    after = path.stat()
    _checked_leveldb_file_size(path, after)
    # Do not import a table observed during a write.  The next run can read the
    # stable one; silently skipping it is safer than accepting a partial cache.
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return
    if len(data) < 48:
        return
    # A LevelDB footer is exactly 48 bytes.  The index handle starts after the
    # variable-length metaindex handle.
    footer = data[-48:]
    try:
        _, after_meta = _read_block_handle(footer, 0)
        index_handle, _ = _read_block_handle(footer, after_meta)
        index_block = _read_block(data, *index_handle)
        index_entries = _decode_leveldb_block(index_block)
    except (OSError, WomierImportError):
        return
    for _index_key, encoded_handle in index_entries:
        try:
            handle, _ = _read_block_handle(encoded_handle, 0)
            data_block = _read_block(data, *handle)
            entries = _decode_leveldb_block(data_block)
        except WomierImportError:
            continue
        for internal_key, value in entries:
            if len(internal_key) < 8:
                continue
            tag = int.from_bytes(internal_key[-8:], "little")
            sequence = tag >> 8
            value_type = tag & 0xFF
            # 1 is value and 0 is deletion in LevelDB's internal key format.
            yield sequence, internal_key[:-8], value if value_type == 1 else None


def _logical_wal_records(
    data: bytes, *, verify_checksums: bool = False, strict: bool = False
) -> Iterator[bytes]:
    """Yield complete LevelDB WAL logical records (including fragmented ones).

    The read-only importer deliberately tolerates a torn tail while Chromium
    is active.  The closed-driver writer uses ``strict=True`` instead: a
    successful post-write parse must be acceptable to native LevelDB too, and
    a new batch must never be appended after a torn record that recovery could
    ignore.
    """
    block_size = 32 * 1024
    fragments = bytearray()
    for base in range(0, len(data), block_size):
        block = data[base:base + block_size]
        pos = 0
        while pos < len(block):
            if pos + _LEVELDB_LOG_HEADER_SIZE > len(block):
                # Native LevelDB permits only an all-zero tail shorter than a
                # physical header (padding after the final record in a block).
                if strict and any(block[pos:]):
                    raise WomierImportError("обрезанный журнал Local Storage Womier")
                break
            # header: crc32c (ignored), uint16 payload length, record type
            length = int.from_bytes(block[pos + 4:pos + 6], "little")
            record_type = block[pos + 6]
            end = pos + _LEVELDB_LOG_HEADER_SIZE + length
            if length == 0 and record_type == 0:
                if strict and any(block[pos:]):
                    raise WomierImportError("некорректное заполнение журнала Local Storage Womier")
                break
            if end > len(block):
                if strict:
                    raise WomierImportError("обрезанная запись журнала Local Storage Womier")
                break
            payload = block[pos + 7:end]
            if verify_checksums:
                expected = int.from_bytes(block[pos:pos + 4], "little")
                actual = _masked_crc32c(_crc32c(bytes((record_type,)) + payload))
                if expected != actual:
                    raise WomierImportError("контрольная сумма журнала Local Storage не совпала")
            pos = end
            if record_type == 1:  # full
                if strict and fragments:
                    raise WomierImportError("оборванная составная запись Local Storage Womier")
                yield payload
                fragments.clear()
            elif record_type == 2:  # first
                if strict and fragments:
                    raise WomierImportError("оборванная составная запись Local Storage Womier")
                fragments = bytearray(payload)
            elif record_type == 3 and fragments:  # middle
                if len(fragments) + len(payload) > _MAX_LEVELDB_LOGICAL_RECORD_BYTES:
                    raise WomierImportError("запись журнала Local Storage слишком большая")
                fragments.extend(payload)
            elif record_type == 4 and fragments:  # last
                if len(fragments) + len(payload) > _MAX_LEVELDB_LOGICAL_RECORD_BYTES:
                    raise WomierImportError("запись журнала Local Storage слишком большая")
                fragments.extend(payload)
                yield bytes(fragments)
                fragments.clear()
            elif strict:
                raise WomierImportError("некорректная составная запись Local Storage Womier")
    if strict and fragments:
        raise WomierImportError("оборванная составная запись Local Storage Womier")


def _wal_entries(
    path: Path, *, verify_checksums: bool = False, strict: bool = False
) -> Iterator[tuple[int, bytes, bytes | None]]:
    """Yield puts/deletions from the small active Chromium LevelDB WAL."""
    before = path.stat()
    _checked_leveldb_file_size(path, before)
    data = path.read_bytes()
    after = path.stat()
    _checked_leveldb_file_size(path, after)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return
    for record in _logical_wal_records(
        data, verify_checksums=verify_checksums, strict=strict
    ):
        if len(record) < 12:
            if strict:
                raise WomierImportError("короткий WriteBatch Local Storage Womier")
            continue
        sequence = int.from_bytes(record[:8], "little")
        count = int.from_bytes(record[8:12], "little")
        pos = 12
        for index in range(count):
            if pos >= len(record):
                if strict:
                    raise WomierImportError("обрезанный WriteBatch Local Storage Womier")
                break
            op = record[pos]
            pos += 1
            try:
                key_len, pos = _read_varint(record, pos)
            except WomierImportError:
                if strict:
                    raise
                break
            end_key = pos + key_len
            if end_key > len(record):
                if strict:
                    raise WomierImportError("обрезанный ключ WriteBatch Local Storage Womier")
                break
            key = record[pos:end_key]
            pos = end_key
            if op == 0:
                yield sequence + index, key, None
                continue
            if op != 1:
                if strict:
                    raise WomierImportError("неизвестная операция WriteBatch Local Storage Womier")
                break
            try:
                value_len, pos = _read_varint(record, pos)
            except WomierImportError:
                if strict:
                    raise
                break
            end_value = pos + value_len
            if end_value > len(record):
                if strict:
                    raise WomierImportError("обрезанное значение WriteBatch Local Storage Womier")
                break
            yield sequence + index, key, record[pos:end_value]
            pos = end_value
        if strict and pos != len(record):
            raise WomierImportError("лишние байты WriteBatch Local Storage Womier")


def _storage_key_name(key: bytes) -> str | None:
    """Recover Chromium's UTF-8 localStorage key from its origin prefix."""
    marker = WOMIER_STORAGE_KEY_PREFIX.encode("utf-8")
    location = key.find(marker)
    if location < 0:
        return None
    raw = key[location:]
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return name if name.startswith(WOMIER_STORAGE_KEY_PREFIX) else None


def _decode_chromium_string(value: bytes) -> str | None:
    """Decode the string payload Chromium stores for DOM localStorage."""
    if not value or len(value) > _MAX_CHROMIUM_JSON_VALUE_BYTES:
        return None
    # Chromium prefixes values with a one-byte type marker.  Retain the raw
    # form too because older Womier builds wrote direct strings.
    candidates = (value, value[1:])
    for candidate in candidates:
        if not candidate:
            continue
        if candidate.startswith((b"{", b"[")):
            try:
                return candidate.decode("utf-8")
            except UnicodeDecodeError:
                pass
        # Values containing non-ASCII JSON are normally UTF-16LE.
        if candidate.startswith((b"{\x00", b"[\x00")):
            try:
                return candidate.decode("utf-16le")
            except UnicodeDecodeError:
                pass
    return None


def read_womier_storage(leveldb_dir: Path | str = WOMIER_DRIVER_LEVELDB) -> dict[str, object]:
    """Return parseable official-driver `DeviceTest_02_*` JSON records.

    The latest LevelDB sequence always wins.  No exception is raised for a
    missing/locked database: a normal first-time use of this driver should not
    turn a missing official driver into an application error.
    """
    try:
        directory = Path(leveldb_dir)
    except (TypeError, ValueError, OSError):
        return {}
    if not directory.is_dir():
        return {}
    latest: dict[str, tuple[int, bytes | None]] = {}
    for path in _leveldb_paths(directory, strict=False):
        try:
            entries = _table_entries(path) if path.suffix == ".ldb" else _wal_entries(path)
            for sequence, raw_key, value in entries:
                key = _storage_key_name(raw_key)
                if key is None:
                    continue
                prior = latest.get(key)
                if prior is None or sequence > prior[0]:
                    latest[key] = (sequence, value)
        except (OSError, WomierImportError):
            continue
    decoded: dict[str, object] = {}
    for key, (_sequence, raw_value) in latest.items():
        if raw_value is None:
            continue
        text = _decode_chromium_string(raw_value)
        if text is None:
            continue
        try:
            value = _strict_womier_json_loads(text)
        except (TypeError, ValueError, RecursionError):
            continue
        if isinstance(value, dict):
            decoded[key] = value
    return decoded


def womier_storage_fingerprint(
    leveldb_dir: Path | str = WOMIER_DRIVER_LEVELDB,
) -> str | None:
    """Return a cheap read-only marker for one official-driver cache snapshot."""
    try:
        directory = Path(leveldb_dir)
    except (TypeError, ValueError, OSError):
        return None
    if not directory.is_dir():
        return None
    try:
        paths = _leveldb_paths(directory, strict=False)
        parts = [
            f"{path.name}:{path.stat().st_mtime_ns}:{path.stat().st_size}"
            for path in paths
        ]
    except (OSError, WomierImportError):
        return None
    return "|".join(parts) if parts else None


def _as_float(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _clamp(value: object, minimum: float, maximum: float, default: float) -> float:
    return round(max(minimum, min(maximum, _as_float(value, default))), 3)


def _convert_womier_modes(
    modes: object,
) -> tuple[dict[str, dict], dict[str, int], dict[str, bool], int]:
    """Convert one official `磁轴` profile into our portable per-slot data."""
    key_settings: dict[str, dict] = {}
    key_modes: dict[str, int] = {}
    rt_separate: dict[str, bool] = {}
    if not isinstance(modes, list):
        return key_settings, key_modes, rt_separate, 0
    for item in modes:
        if not isinstance(item, dict):
            continue
        try:
            hid = int(item.get("original"))
        except (TypeError, ValueError, OverflowError):
            continue
        key = SK75_KEY_BY_HID.get(hid)
        if key is None:
            continue
        # Womier has two different lift-travel values: ``liftTravel`` is the
        # ordinary deactivation point in normal mode, while
        # ``fireLiftTravel`` belongs to Rapid Trigger.  Preserve both instead
        # of treating the RT value as a normal-mode release point.
        # The current SK75 Womier UI permits 0.10..3.30 mm actuation and
        # 0.01..2.00 mm RT values.  Keep the import aligned with the UI rather
        # than retaining an old protocol-only 3.50 mm value that cannot be
        # selected after opening the stock driver.
        actuation = _clamp(
            item.get("travel"),
            MagneticProtocol.OFFICIAL_SK75_ACTUATION_MIN_MM,
            MagneticProtocol.OFFICIAL_SK75_ACTUATION_MAX_MM,
            1.20,
        )
        deactivation = _clamp(
            item.get("liftTravel", item.get("travel")),
            MagneticProtocol.OFFICIAL_SK75_ACTUATION_MIN_MM,
            MagneticProtocol.OFFICIAL_SK75_ACTUATION_MAX_MM,
            actuation,
        )
        rapid_trigger = bool(item.get("fire", False))
        rapid_press = _clamp(
            item.get("firePressTravel"),
            MagneticProtocol.OFFICIAL_SK75_RAPID_MIN_MM,
            MagneticProtocol.OFFICIAL_SK75_RAPID_MAX_MM,
            0.30,
        )
        rapid_release = _clamp(
            item.get("fireLiftTravel", item.get("firePressTravel")),
            MagneticProtocol.OFFICIAL_SK75_RAPID_MIN_MM,
            MagneticProtocol.OFFICIAL_SK75_RAPID_MAX_MM,
            rapid_press,
        )
        lower_zone = _clamp(item.get("deadZoneTravel"), 0.0, 1.0, 0.30)
        upper_zone = _clamp(item.get("topDeadZoneTravel"), 0.0, 1.0, 0.0)
        key_settings[str(key.slot)] = {
            "actuation": actuation,
            "rapid_trigger": rapid_trigger,
            "rapid_press": rapid_press,
            "rapid_release": rapid_release,
            "lower_dead_zone": lower_zone,
            "upper_dead_zone": upper_zone,
            "deactivation": deactivation,
        }
        rt_separate[str(key.slot)] = rapid_trigger and rapid_press != rapid_release
        option = str(item.get("option", "normal"))
        mode = {
            "normal": MagneticProtocol.MODE_NORMAL,
            "snap": MagneticProtocol.MODE_SNAP,
            "dks": 2,
            "mt": 3,
            "tgl_hold": 4,
            "tgl_dots": 5,
        }.get(option, MagneticProtocol.MODE_NORMAL)
        if rapid_trigger:
            mode |= MagneticProtocol.MODE_RAPID_TRIGGER_BIT
        key_modes[str(key.slot)] = mode
    return key_settings, key_modes, rt_separate, len(key_settings)


def decode_womier_magnetic_profiles(storage_key: str, data: object) -> WomierMagneticImport | None:
    """Convert one `DeviceTest_02_*` record into four app-side presets."""
    if not isinstance(data, dict):
        return None
    raw_profiles = data.get("磁轴")
    if not isinstance(raw_profiles, list):
        return None
    profiles: dict[str, dict] = {}
    imported = 0
    for raw_profile in raw_profiles:
        if not isinstance(raw_profile, dict):
            continue
        try:
            index = int(raw_profile.get("profile"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not 0 <= index < 4:
            continue
        settings, modes, rt_separate, count = _convert_womier_modes(raw_profile.get("modes"))
        # A candidate must match actual SK75 keys.  This prevents importing a
        # stale cache from another Womier keyboard that happens to share the
        # generic DeviceTest prefix.
        if count < 12:
            continue
        profiles[str(index)] = {
            "key_settings": settings,
            "key_modes": modes,
            "keyboard_options": {},
            "rt_separate": rt_separate,
            "initialized": True,
        }
        imported += 1
    if not profiles:
        return None
    return WomierMagneticImport(storage_key, profiles, imported)


def find_womier_magnetic_import(
    leveldb_dir: Path | str = WOMIER_DRIVER_LEVELDB,
) -> WomierMagneticImport | None:
    """Find the strongest SK75 candidate in Womier Driver's cache.

    The official app encodes a model-specific id in the localStorage key, but
    it is not the USB PID and can change between driver releases.  Selecting
    the record by recognised SK75 magnetic key count is safer than guessing a
    key suffix.
    """
    best: WomierMagneticImport | None = None
    for storage_key, data in read_womier_storage(leveldb_dir).items():
        candidate = decode_womier_magnetic_profiles(storage_key, data)
        if candidate is None:
            continue
        if best is None or candidate.imported_profile_count > best.imported_profile_count:
            best = candidate
    return best


# ---------------------------------------------------------------------------
# Closed-driver cache synchronization
# ---------------------------------------------------------------------------
#
# The Womier Electron application keeps its own magnetic-profile cache in the
# Chromium Local Storage LevelDB.  It deliberately trusts that cache on the
# next launch instead of fetching every value from the keyboard again.  HID
# writes from this application are therefore invisible to Womier until its
# cache is updated too.
#
# Chromium/LevelDB has no public cross-process API for this old driver build.
# Writing an SST table or replacing a LevelDB directory would be unsafe.  A
# complete WriteBatch in the *active WAL*, however, is the native recovery
# format understood by LevelDB itself.  The small writer below only uses that
# format under an exclusive LOCK, with an on-disk backup and post-write parse
# verification.  It never writes while the official Electron process owns the
# database.

_LEVELDB_LOG_BLOCK_SIZE = 32 * 1024
_LEVELDB_LOG_HEADER_SIZE = 7
_LEVELDB_LOG_FULL = 1
_LEVELDB_LOG_FIRST = 2
_LEVELDB_LOG_MIDDLE = 3
_LEVELDB_LOG_LAST = 4


def _crc32c_table() -> tuple[int, ...]:
    """Create the reflected Castagnoli table used by LevelDB log headers."""
    values: list[int] = []
    polynomial = 0x82F63B78
    for index in range(256):
        value = index
        for _ in range(8):
            value = (value >> 1) ^ polynomial if value & 1 else value >> 1
        values.append(value & 0xFFFFFFFF)
    return tuple(values)


_CRC32C_TABLE = _crc32c_table()


def _crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for value in data:
        crc = _CRC32C_TABLE[(crc ^ value) & 0xFF] ^ (crc >> 8)
    return (~crc) & 0xFFFFFFFF


def _masked_crc32c(value: int) -> int:
    """LevelDB's on-disk CRC mask, not a generic checksum transform."""
    value &= 0xFFFFFFFF
    return (((value >> 15) | (value << 17)) + 0xA282EAD8) & 0xFFFFFFFF


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise WomierCacheSyncError("отрицательный varint для Local Storage")
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _append_leveldb_logical_record(handle, payload: bytes) -> None:
    """Append one complete logical LevelDB WAL record to an open binary file."""
    if not payload:
        raise WomierCacheSyncError("пустой WriteBatch нельзя записать в Local Storage")
    remaining = memoryview(payload)
    is_first = True
    while remaining:
        block_offset = handle.tell() % _LEVELDB_LOG_BLOCK_SIZE
        block_left = _LEVELDB_LOG_BLOCK_SIZE - block_offset
        if block_left < _LEVELDB_LOG_HEADER_SIZE:
            handle.write(b"\x00" * block_left)
            block_left = _LEVELDB_LOG_BLOCK_SIZE
        fragment_length = min(len(remaining), block_left - _LEVELDB_LOG_HEADER_SIZE)
        fragment = remaining[:fragment_length].tobytes()
        is_last = fragment_length == len(remaining)
        if is_first and is_last:
            record_type = _LEVELDB_LOG_FULL
        elif is_first:
            record_type = _LEVELDB_LOG_FIRST
        elif is_last:
            record_type = _LEVELDB_LOG_LAST
        else:
            record_type = _LEVELDB_LOG_MIDDLE
        checksum = _masked_crc32c(_crc32c(bytes((record_type,)) + fragment))
        header = struct.pack("<IHB", checksum, len(fragment), record_type)
        handle.write(header)
        handle.write(fragment)
        remaining = remaining[fragment_length:]
        is_first = False


def _build_leveldb_write_batch(
    sequence: int, writes: Iterable[tuple[bytes, bytes]]
) -> bytes:
    """Encode a native LevelDB WriteBatch containing only put operations."""
    records = list(writes)
    if sequence < 0 or not records:
        raise WomierCacheSyncError("некорректный WriteBatch Local Storage")
    result = bytearray(struct.pack("<QI", sequence, len(records)))
    for key, value in records:
        if not isinstance(key, bytes) or not key or not isinstance(value, bytes):
            raise WomierCacheSyncError("некорректная запись Local Storage")
        result.append(1)  # kTypeValue
        result.extend(_encode_varint(len(key)))
        result.extend(key)
        result.extend(_encode_varint(len(value)))
        result.extend(value)
    return bytes(result)


def _read_latest_leveldb_entries(directory: Path) -> dict[bytes, tuple[int, bytes | None]]:
    """Read raw latest entries, including primitive Local Storage values."""
    if not directory.is_dir():
        raise WomierCacheSyncError("Local Storage Womier не найден")
    latest: dict[bytes, tuple[int, bytes | None]] = {}
    for path in _leveldb_paths(directory, strict=True):
        try:
            entries = (
                _table_entries(path)
                if path.suffix == ".ldb"
                else _wal_entries(path, verify_checksums=True, strict=True)
            )
            for sequence, raw_key, value in entries:
                prior = latest.get(raw_key)
                if prior is None or sequence > prior[0]:
                    latest[raw_key] = (sequence, value)
        except (OSError, WomierImportError):
            # A malformed historical SST is not a safe place to write into.
            raise WomierCacheSyncError("не удалось безопасно прочитать Local Storage Womier")
    return latest


def _skip_manifest_length_prefixed(data: bytes, offset: int) -> int:
    size, offset = _read_varint(data, offset)
    end = offset + size
    if end > len(data):
        raise WomierCacheSyncError("повреждён MANIFEST Local Storage Womier")
    return end


def _leveldb_manifest_state(directory: Path) -> tuple[int, int]:
    """Return (active WAL number, last sequence) from Chromium's MANIFEST."""
    current_path = directory / "CURRENT"
    try:
        if current_path.is_symlink() or not current_path.is_file():
            raise WomierCacheSyncError("CURRENT Local Storage Womier повреждён")
        current_data = current_path.read_bytes()
        if len(current_data) > 1024:
            raise WomierCacheSyncError("CURRENT Local Storage Womier повреждён")
        manifest_name = current_data.decode("ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise WomierCacheSyncError("не удалось прочитать CURRENT Local Storage Womier") from exc
    if (
        not manifest_name
        or Path(manifest_name).name != manifest_name
        or not manifest_name.startswith("MANIFEST-")
        or not manifest_name[len("MANIFEST-"):].isdigit()
    ):
        raise WomierCacheSyncError("CURRENT Local Storage Womier повреждён")
    manifest_path = directory / manifest_name
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise WomierCacheSyncError("MANIFEST Local Storage Womier повреждён")
        if _checked_leveldb_file_size(manifest_path) > _MAX_MANIFEST_BYTES:
            raise WomierCacheSyncError("MANIFEST Local Storage Womier слишком большой")
        records = list(
            _logical_wal_records(
                manifest_path.read_bytes(), verify_checksums=True, strict=True
            )
        )
    except (OSError, WomierImportError) as exc:
        raise WomierCacheSyncError("не удалось прочитать MANIFEST Local Storage Womier") from exc

    log_number: int | None = None
    last_sequence: int | None = None
    try:
        for record in records:
            position = 0
            while position < len(record):
                tag, position = _read_varint(record, position)
                if tag in (2, 3, 4, 9):
                    value, position = _read_varint(record, position)
                    if tag == 2:
                        log_number = value
                    elif tag == 4:
                        last_sequence = value
                elif tag == 1:  # comparator name
                    position = _skip_manifest_length_prefixed(record, position)
                elif tag == 5:  # compact pointer
                    _level, position = _read_varint(record, position)
                    position = _skip_manifest_length_prefixed(record, position)
                elif tag == 6:  # deleted file
                    _level, position = _read_varint(record, position)
                    _file_number, position = _read_varint(record, position)
                elif tag == 7:  # new file
                    _level, position = _read_varint(record, position)
                    _file_number, position = _read_varint(record, position)
                    _file_size, position = _read_varint(record, position)
                    position = _skip_manifest_length_prefixed(record, position)
                    position = _skip_manifest_length_prefixed(record, position)
                else:
                    raise WomierCacheSyncError("неизвестная запись MANIFEST Local Storage Womier")
    except WomierImportError as exc:
        raise WomierCacheSyncError("повреждён MANIFEST Local Storage Womier") from exc

    if log_number is None or last_sequence is None:
        raise WomierCacheSyncError("MANIFEST Local Storage Womier не содержит активный журнал")
    return log_number, last_sequence


def is_womier_driver_running() -> bool:
    """Return True while a known official process can own Womier state.

    Cache writes deliberately prefer a safe false positive to an unsafe false
    negative.  The process names alone are retained for compatibility with an
    installed driver relocated by the vendor, and canonical known paths make
    the check work even when a helper is hosted with an unusual visible name.
    This function only decides whether to *defer* a cache update; it never
    performs a destructive process action.
    """
    try:
        import psutil
    except ImportError:
        # The subsequent exclusive LevelDB lock is still authoritative.
        return False
    expected_names = {
        name.casefold() for name, _path in WOMIER_CACHE_OWNER_PROCESS_TARGETS
    }
    expected_paths = {
        os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path)))).casefold()
        for _name, path in WOMIER_CACHE_OWNER_PROCESS_TARGETS
    }
    try:
        processes = psutil.process_iter(("name", "exe", "cmdline"))
        for process in processes:
            try:
                info = process.info
                name = str(info.get("name") or "").casefold()
                executable = str(info.get("exe") or "").casefold()
                command_line = " ".join(info.get("cmdline") or ()).casefold()
            except (psutil.Error, OSError):
                continue
            if name in expected_names:
                return True
            try:
                executable_path = (
                    os.path.normcase(os.path.realpath(os.path.abspath(executable))).casefold()
                    if executable
                    else ""
                )
            except (OSError, TypeError, ValueError):
                executable_path = ""
            if executable_path and executable_path in expected_paths:
                return True
            if any(path and path in command_line for path in expected_paths):
                return True
    except (psutil.Error, OSError):
        return False
    return False


@contextmanager
def _exclusive_leveldb_lock(directory: Path) -> Iterator[None]:
    """Acquire the same exclusive `LOCK` protection expected by LevelDB."""
    lock_path = directory / "LOCK"
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE

        class Overlapped(ctypes.Structure):
            _fields_ = (
                ("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            )

        handle = create_file(
            str(lock_path),
            0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            0,  # no sharing: also blocks a just-starting Electron process
            None,
            4,  # OPEN_ALWAYS
            0x80,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle is None or handle == invalid:
            raise WomierCacheSyncDeferred(
                "официальный Womier Driver использует свой Local Storage"
            )
        overlapped = Overlapped()
        locked = False
        try:
            # LOCKFILE_FAIL_IMMEDIATELY | LOCKFILE_EXCLUSIVE_LOCK.
            if not kernel32.LockFileEx(handle, 0x00000001 | 0x00000002, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped)):
                raise WomierCacheSyncDeferred(
                    "официальный Womier Driver использует свой Local Storage"
                )
            locked = True
            yield
        finally:
            if locked:
                kernel32.UnlockFileEx(handle, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(overlapped))
            kernel32.CloseHandle(handle)
        return

    # The product is Windows-only, but keeping this branch makes the parser and
    # regression tests portable for contributors.
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - platform safeguard
        raise WomierCacheSyncError("эта платформа не поддерживает блокировку Local Storage") from exc
    with lock_path.open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WomierCacheSyncDeferred(
                "официальный Womier Driver использует свой Local Storage"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _womier_backup_parent(directory: Path) -> Path:
    """Return the dedicated manager-owned recovery directory."""
    return directory.parent / _WOMIER_BACKUP_DIR_NAME


def _backup_directory_size(directory: Path) -> int:
    """Count one flat manager backup without following links or subtrees."""
    total = 0
    try:
        for item in directory.iterdir():
            if item.is_symlink() or not item.is_file():
                continue
            try:
                total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def _managed_womier_backups(backup_parent: Path) -> list[tuple[Path, int, int]]:
    """List only backups created by this version of the manager.

    Earlier builds did not create a marker.  They may contain a user's only
    recovery copy, so retention must never delete them silently; it starts
    only with explicitly marked future backups.
    """
    result: list[tuple[Path, int, int]] = []
    try:
        candidates = list(backup_parent.iterdir())
    except OSError:
        return result
    for candidate in candidates:
        try:
            if (
                candidate.is_symlink()
                or not candidate.is_dir()
                or not _WOMIER_BACKUP_DIRECTORY_PATTERN.fullmatch(candidate.name)
            ):
                continue
            marker = candidate / _WOMIER_BACKUP_MARKER_NAME
            if marker.is_symlink() or not marker.is_file():
                continue
            metadata = _strict_womier_json_loads(marker.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                continue
            if (
                metadata.get("format") != _WOMIER_BACKUP_FORMAT
                or metadata.get("version") != _WOMIER_BACKUP_VERSION
            ):
                continue
            stat = candidate.stat()
        except (OSError, ValueError, TypeError, RecursionError):
            continue
        result.append((candidate, _backup_directory_size(candidate), stat.st_mtime_ns))
    return result


def _prune_managed_womier_backups(backup_parent: Path, *, keep: Path | None = None) -> None:
    """Bound future manager backups after a successfully verified cache write.

    ``keep`` is the just-created backup and is never removed in this run.  We
    intentionally do *not* touch unmarked historical directories: removing
    the user's existing recovery history requires an explicit UI choice.
    """
    backups = _managed_womier_backups(backup_parent)
    total = sum(size for _path, size, _mtime in backups)
    # Oldest first.  Keep new data whenever possible; if a single new backup
    # exceeds the cap it remains as the one recovery point for that sync.
    for path, size, _mtime in sorted(backups, key=lambda item: item[2]):
        if path == keep:
            continue
        if len(backups) <= _MAX_WOMIER_BACKUP_COUNT and total <= _MAX_WOMIER_BACKUP_TOTAL_BYTES:
            break
        try:
            shutil.rmtree(path)
        except OSError:
            _LOGGER.warning("could not prune Womier recovery backup %s", path)
            continue
        total -= size
        backups = [item for item in backups if item[0] != path]


def _create_womier_backup(directory: Path) -> Path:
    """Copy the compact LevelDB state before one append-only cache update."""
    backup_parent = _womier_backup_parent(directory)
    backup_dir = backup_parent / (
        f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{uuid.uuid4().hex[:8]}"
    )
    backup_dir.mkdir(parents=True, exist_ok=False)
    try:
        for item in directory.iterdir():
            if item.is_symlink() or not item.is_file() or item.name == "LOCK":
                continue
            if (
                item.suffix in {".ldb", ".log"}
                or item.name in {"CURRENT", "LOG", "LOG.old"}
                or item.name.startswith("MANIFEST-")
            ):
                shutil.copy2(item, backup_dir / item.name)
        # The marker is the authority for automatic retention.  It is written
        # only after the complete backup copy succeeds, so an interrupted
        # historical/partial directory will remain untouched by cleanup.
        (backup_dir / _WOMIER_BACKUP_MARKER_NAME).write_text(
            json.dumps(
                {
                    "format": _WOMIER_BACKUP_FORMAT,
                    "version": _WOMIER_BACKUP_VERSION,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        try:
            shutil.rmtree(backup_dir)
        except OSError:
            pass
        raise WomierCacheSyncError("не удалось создать резервную копию Local Storage Womier") from exc
    return backup_dir


def _decode_raw_womier_json(value: bytes | None) -> dict | None:
    if value is None:
        return None
    text = _decode_chromium_string(value)
    if text is None:
        return None
    try:
        data = _strict_womier_json_loads(text)
    except (TypeError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _find_raw_womier_magnetic_cache(
    latest: Mapping[bytes, tuple[int, bytes | None]],
) -> tuple[str, bytes, dict] | None:
    """Find the actual raw storage key and the strongest SK75 cache object."""
    best: tuple[str, bytes, dict, int] | None = None
    for raw_key, (_sequence, raw_value) in latest.items():
        storage_key = _storage_key_name(raw_key)
        if storage_key is None:
            continue
        data = _decode_raw_womier_json(raw_value)
        candidate = decode_womier_magnetic_profiles(storage_key, data)
        if candidate is None or data is None:
            continue
        score = candidate.imported_profile_count
        if best is None or score > best[3]:
            best = (storage_key, raw_key, data, score)
    if best is None:
        return None
    return best[0], best[1], best[2]


def _coerce_key_magnetic_settings(value: object) -> KeyMagneticSettings:
    """Validate both dataclass settings and JSON-shaped app preset values."""
    if isinstance(value, KeyMagneticSettings):
        return MagneticProtocol.clamp_key_settings_to_official_bounds(value)
    getter = value.get if isinstance(value, Mapping) else lambda name, default=None: getattr(value, name, default)
    try:
        return MagneticProtocol.clamp_key_settings_to_official_bounds(
            KeyMagneticSettings(
                actuation=float(getter("actuation")),
                rapid_trigger=bool(getter("rapid_trigger")),
                rapid_press=float(getter("rapid_press")),
                rapid_release=float(getter("rapid_release")),
                lower_dead_zone=float(getter("lower_dead_zone")),
                upper_dead_zone=float(getter("upper_dead_zone")),
                deactivation=float(getter("deactivation", getter("actuation"))),
            )
        )
    except (TypeError, ValueError, MagneticProtocolError) as exc:
        raise WomierCacheSyncError("некорректные магнитные значения для синхронизации") from exc


def _round_womier_mm(value: float) -> float:
    # Womier's JSON represents its 0.01 mm firmware units as ordinary numbers.
    return round(float(value), 2)


def _merge_womier_profile_values(
    data: dict,
    profile_index: int,
    key_settings: Mapping[object, object],
    key_modes: Mapping[object, object] | None,
) -> int:
    """Merge app-side values into one official profile without touching extras."""
    raw_profiles = data.get("磁轴")
    if not isinstance(raw_profiles, list):
        raise WomierCacheSyncError("в кэше Womier нет магнитных профилей")
    target_profile: dict | None = None
    for candidate in raw_profiles:
        if not isinstance(candidate, dict):
            continue
        try:
            if int(candidate.get("profile")) == profile_index:
                target_profile = candidate
                break
        except (TypeError, ValueError, OverflowError):
            continue
    if target_profile is None:
        raise WomierCacheSyncError(f"в кэше Womier отсутствует профиль {profile_index + 1}")
    modes = target_profile.get("modes")
    if not isinstance(modes, list):
        raise WomierCacheSyncError("профиль Womier содержит некорректные магнитные клавиши")

    changed = 0
    for raw_slot, raw_settings in key_settings.items():
        try:
            slot = int(raw_slot)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WomierCacheSyncError("некорректный слот магнитной клавиши") from exc
        key = SK75_KEY_BY_SLOT.get(slot)
        # Fn is a layer key, not a magnetic HID key in Womier's `modes` array.
        if key is None or key.hid is None:
            continue
        settings = _coerce_key_magnetic_settings(raw_settings)
        matching: list[dict] = []
        for item in modes:
            if not isinstance(item, dict):
                continue
            try:
                original = int(item.get("original", -1))
            except (TypeError, ValueError, OverflowError):
                continue
            if original == key.hid:
                matching.append(item)
        if len(matching) != 1:
            raise WomierCacheSyncError(
                f"не удалось однозначно найти клавишу {key.label} в кэше Womier"
            )
        item = matching[0]
        # `liftTravel` is Womier's ordinary-release setting.  It is independent
        # of `fireLiftTravel` and must therefore follow the app's normal-mode
        # deactivation point even while Rapid Trigger is disabled.
        update = {
            "travel": _round_womier_mm(settings.actuation),
            "liftTravel": _round_womier_mm(settings.deactivation),
            "fire": bool(settings.rapid_trigger),
            "firePressTravel": _round_womier_mm(settings.rapid_press),
            "fireLiftTravel": _round_womier_mm(settings.rapid_release),
            "deadZoneTravel": _round_womier_mm(settings.lower_dead_zone),
            "topDeadZoneTravel": _round_womier_mm(settings.upper_dead_zone),
        }
        if key_modes is not None:
            raw_mode = key_modes.get(str(slot), key_modes.get(slot))
            if raw_mode is not None:
                try:
                    mode = int(raw_mode)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise WomierCacheSyncError("некорректный режим магнитной клавиши") from exc
                option = mode & 0x7F
                # Never convert a foreign advanced mode (DKS/MT/TGL) by guess.
                if option == MagneticProtocol.MODE_NORMAL:
                    update["option"] = "normal"
                    update["mode"] = "normal"
                elif option == MagneticProtocol.MODE_SNAP:
                    update["option"] = "snap"
                    update["mode"] = "normal"
                update["fire"] = bool(mode & MagneticProtocol.MODE_RAPID_TRIGGER_BIT)
        if any(item.get(name) != value for name, value in update.items()):
            item.update(update)
            changed += 1
    return changed


def _encode_womier_json_value(data: dict) -> bytes:
    try:
        text = json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise WomierCacheSyncError("не удалось сериализовать кэш Womier") from exc
    # Chromium stores file:// Local Storage JSON as a type byte + UTF-16LE.
    encoded = text.encode("utf-16le")
    if len(encoded) > _MAX_CHROMIUM_JSON_VALUE_BYTES:
        raise WomierCacheSyncError("кэш Womier слишком большой для безопасной синхронизации")
    return b"\x00" + encoded


def _raw_key_prefix(raw_device_key: bytes, storage_key: str) -> bytes:
    suffix = storage_key.encode("utf-8")
    if not raw_device_key.endswith(suffix):
        raise WomierCacheSyncError("не удалось определить origin Local Storage Womier")
    return raw_device_key[: -len(suffix)]


def _restore_log_size(log_path: Path, size: int) -> None:
    """Undo an incomplete append while our exclusive lock is still held."""
    try:
        with log_path.open("r+b") as handle:
            handle.truncate(size)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        _LOGGER.exception("could not roll back partial Womier LevelDB WAL append")


def sync_womier_magnetic_cache(
    profile_index: int,
    key_settings: Mapping[object, object] | None = None,
    *,
    key_modes: Mapping[object, object] | None = None,
    rt_stab: int | None = None,
    leveldb_dir: Path | str = WOMIER_DRIVER_LEVELDB,
) -> WomierCacheSyncResult:
    """Synchronize values *already written successfully through HID* to Womier.

    This is intentionally a closed-driver operation.  If the official Womier
    Driver is open, its renderer owns an in-memory copy and can overwrite a
    direct cache edit.  In that case a deferred result is returned and no file
    is touched.  Callers should surface that short status but must not treat it
    as a keyboard-write failure.
    """
    try:
        if isinstance(profile_index, bool):
            raise ValueError
        profile_index = int(profile_index)
    except (TypeError, ValueError, OverflowError):
        return WomierCacheSyncResult(False, False, "Некорректный профиль Womier.")
    if not 0 <= profile_index < 4:
        return WomierCacheSyncResult(False, False, "Профиль Womier должен быть от 1 до 4.")
    if key_settings is None:
        key_settings = {}
    if not isinstance(key_settings, Mapping):
        return WomierCacheSyncResult(False, False, "Нет магнитных значений для синхронизации.")
    if not key_settings and rt_stab is None:
        return WomierCacheSyncResult(False, False, "Нет магнитных значений для синхронизации.")

    try:
        directory = Path(leveldb_dir)
    except (TypeError, ValueError, OSError) as exc:
        raise WomierCacheSyncError("некорректный путь Local Storage Womier") from exc
    if directory.is_symlink():
        # This function is a writer.  A reparse/symlinked root can otherwise
        # move the active WAL and its backup outside the official driver's
        # Local Storage folder between our validation and append.
        raise WomierCacheSyncError("Local Storage Womier не должен быть ссылкой")
    if is_womier_driver_running():
        return WomierCacheSyncResult(
            False,
            True,
            "Официальный Womier Driver открыт: его кэш будет синхронизирован после закрытия драйвера.",
        )

    with _WOMIER_CACHE_SYNC_LOCK:
        # Check again after an earlier local sync has finished: the user may
        # have launched Womier while this call waited for the in-process lock.
        if is_womier_driver_running():
            return WomierCacheSyncResult(
                False,
                True,
                "Официальный Womier Driver открыт: его кэш будет синхронизирован после закрытия драйвера.",
            )
        try:
            with _exclusive_leveldb_lock(directory):
                latest = _read_latest_leveldb_entries(directory)
                located = _find_raw_womier_magnetic_cache(latest)
                if located is None:
                    raise WomierCacheSyncError("кэш SK75 в официальном Womier Driver не найден")
                storage_key, raw_device_key, data = located
                changed = _merge_womier_profile_values(
                    data, profile_index, key_settings, key_modes
                )
                writes: list[tuple[bytes, bytes]] = []
                if changed:
                    writes.append((raw_device_key, _encode_womier_json_value(data)))
                if rt_stab is not None:
                    try:
                        rt_stab = int(rt_stab)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise WomierCacheSyncError("некорректный RTStab для Womier") from exc
                    if rt_stab not in (0, 25, 50, 75, 100, 125):
                        raise WomierCacheSyncError("некорректный RTStab для Womier")
                    device_id = storage_key.rsplit("_", 1)[-1]
                    prefix = _raw_key_prefix(raw_device_key, storage_key)
                    writes.extend(
                        (
                            (prefix + f"{device_id}_RTStab_value".encode("utf-8"), b"\x01" + str(rt_stab).encode("ascii")),
                            (
                                prefix + f"{device_id}_RTStab_open".encode("utf-8"),
                                b"\x01" + (b"true" if rt_stab else b"false"),
                            ),
                        )
                    )
                if not writes:
                    return WomierCacheSyncResult(
                        True,
                        False,
                        "Кэш Womier уже содержит выбранные магнитные значения.",
                        storage_key=storage_key,
                    )

                log_number, manifest_sequence = _leveldb_manifest_state(directory)
                latest_sequence = max((sequence for sequence, _value in latest.values()), default=0)
                sequence = max(manifest_sequence, latest_sequence) + 1
                log_path = directory / f"{log_number:06d}.log"
                # A missing active log is a recovery state that Chromium must
                # resolve itself.  Do not create a guessed WAL from Python:
                # the manifest can then be stale relative to a just-finished
                # compaction, and declining is fully recoverable.
                if log_path.is_symlink() or not log_path.is_file():
                    raise WomierCacheSyncError("активный журнал Local Storage Womier не найден")
                try:
                    _checked_leveldb_file_size(log_path)
                except WomierImportError as exc:
                    raise WomierCacheSyncError(
                        "активный журнал Local Storage Womier повреждён"
                    ) from exc
                backup_dir = _create_womier_backup(directory)
                payload = _build_leveldb_write_batch(sequence, writes)
                old_size: int | None = None
                try:
                    with log_path.open("a+b") as handle:
                        handle.seek(0, os.SEEK_END)
                        old_size = handle.tell()
                        _append_leveldb_logical_record(handle, payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                except Exception as exc:
                    # Never guess a rollback length.  If opening/seeking the
                    # WAL itself failed, no append is known to have happened;
                    # truncating it to zero would be worse than leaving it
                    # untouched.  A partially appended record is rolled back
                    # only after its exact prior EOF was observed.
                    if old_size is not None:
                        _restore_log_size(log_path, old_size)
                    if isinstance(exc, WomierCacheSyncError):
                        raise
                    raise WomierCacheSyncError("не удалось записать журнал Local Storage Womier") from exc

                # Verify the exact raw values through the existing read-only
                # LevelDB parser before releasing LOCK.  If this fails, undo
                # the append while no Womier process can race us.
                try:
                    verified = _read_latest_leveldb_entries(directory)
                    for raw_key, expected in writes:
                        actual = verified.get(raw_key)
                        if actual is None or actual[1] != expected:
                            raise WomierCacheSyncError(
                                "не удалось проверить запись Local Storage Womier"
                            )
                except Exception:
                    # A post-append parser/CRC failure is just as unsafe as a
                    # failed write: remove exactly the bytes we appended while
                    # our exclusive LevelDB lock is still held.
                    if old_size is not None:
                        _restore_log_size(log_path, old_size)
                    raise
        except WomierCacheSyncDeferred as exc:
            return WomierCacheSyncResult(
                False,
                True,
                "Официальный Womier Driver открыт: его кэш будет синхронизирован после закрытия драйвера.",
            )

    # Prune only after the WAL append and post-write parser verification have
    # both succeeded.  A failed sync keeps every existing recovery point.
    if backup_dir is not None:
        try:
            _prune_managed_womier_backups(
                _womier_backup_parent(directory), keep=backup_dir
            )
        except Exception:
            _LOGGER.warning("could not prune managed Womier recovery backups", exc_info=True)

    return WomierCacheSyncResult(
        True,
        False,
        "Значения синхронизированы с кэшем официального Womier Driver.",
        storage_key=storage_key,
        changed_values=changed,
        backup_dir=backup_dir,
    )
