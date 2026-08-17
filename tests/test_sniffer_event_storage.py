"""Bounded, thread-safe storage tests for the opt-in browser sniffer."""

from __future__ import annotations

from collections import deque
import json
import threading

from app_flet import (
    QMKManager,
    SNIFF_EVENT_DATA_LIMIT,
    SNIFF_EVENT_LIMIT,
    SNIFF_EVENT_TEXT_LIMIT,
    _bounded_sniff_event_snapshot,
)


def _manager_with_sniff_storage() -> QMKManager:
    manager = QMKManager.__new__(QMKManager)
    manager._sniff_events_lock = threading.RLock()
    manager.sniff_events = deque(maxlen=SNIFF_EVENT_LIMIT)
    return manager


def test_sniff_event_snapshot_detaches_and_limits_browser_controlled_values():
    raw_data = list(range(SNIFF_EVENT_DATA_LIMIT + 40))
    event = {
        "dir": "tx" * 100,
        "type": "feature" * 100,
        "reportId": 7,
        "data": raw_data,
        "ts": 123.4,
        "nested": {"untrusted": ["not retained"]},
    }

    snapshot = _bounded_sniff_event_snapshot(event)
    raw_data[0] = 99

    assert snapshot["dir"] == ("tx" * 100)[:SNIFF_EVENT_TEXT_LIMIT]
    assert snapshot["type"] == ("feature" * 100)[:SNIFF_EVENT_TEXT_LIMIT]
    assert snapshot["data"] == list(range(SNIFF_EVENT_DATA_LIMIT))
    assert snapshot["data_truncated"] is True
    assert "nested" not in snapshot


def test_sniff_event_journal_is_bounded_and_snapshot_is_json_safe():
    manager = _manager_with_sniff_storage()
    for value in range(SNIFF_EVENT_LIMIT + 7):
        manager._append_sniff_event(
            {
                "dir": "tx" if value % 2 else "rx",
                "type": "feature",
                "reportId": value % 256,
                "data": [value % 256],
            }
        )

    snapshot = manager._sniff_events_snapshot()

    assert len(snapshot) == SNIFF_EVENT_LIMIT
    assert snapshot[0]["data"] == [7]
    assert snapshot[-1]["data"] == [(SNIFF_EVENT_LIMIT + 6) % 256]
    assert json.loads(json.dumps(snapshot)) == snapshot


def test_sniff_event_snapshot_drops_malformed_values_without_throwing():
    snapshot = _bounded_sniff_event_snapshot(
        {
            "dir": 99,
            "type": object(),
            "reportId": -1,
            "data": [0, True, -1, 256, "4", 7],
            "ts": float("inf"),
        }
    )

    assert snapshot["dir"] == ""
    assert snapshot["type"] == ""
    assert snapshot["reportId"] is None
    assert snapshot["data"] == [0, 7]
    # The snapshot itself stays safe for strict JSON serialization.
    assert json.loads(json.dumps(snapshot, allow_nan=False)) == snapshot
