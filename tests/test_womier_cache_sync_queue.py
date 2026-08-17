"""Application-side scheduling coverage for official Womier cache mirroring."""

from __future__ import annotations

from types import SimpleNamespace

import app_flet as app_module
from app_flet import QMKManager
from magnetic import KeyMagneticSettings


class _ManualTimer:
    """A non-threaded Timer substitute: tests explicitly drain queued work."""

    def __init__(self, _delay, target):
        self.delay = _delay
        self.target = target
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def is_alive(self):
        return self.started and not self.cancelled


def _manager():
    manager = QMKManager.__new__(QMKManager)
    manager.app_alive = True
    manager._ensure_womier_cache_sync_state()
    return manager


def _persistable_manager():
    manager = _manager()
    manager.is_running = False
    manager.config = {
        "active_device": "sk75",
        "devices": {"sk75": {"womier_cache_sync_pending": {}}},
    }
    manager.save_config = lambda **_kwargs: None
    return manager


def test_successful_hid_deltas_are_coalesced_before_one_womier_sync(monkeypatch):
    manager = _manager()
    calls = []
    monkeypatch.setattr(app_module.threading, "Timer", _ManualTimer)
    monkeypatch.setattr(
        app_module,
        "sync_womier_magnetic_cache",
        lambda profile, settings, **kwargs: calls.append((profile, settings, kwargs))
        or SimpleNamespace(synced=True, deferred=False, detail="ok"),
    )

    manager._queue_womier_cache_sync(
        1, key_settings={8: "older"}, key_modes={8: 0x80}
    )
    manager._queue_womier_cache_sync(
        1, key_settings={8: "newer", 9: "other"}, rt_stab=50
    )
    manager._drain_womier_cache_sync()

    assert calls == [
        (
            1,
            {"8": "newer", "9": "other"},
            {"key_modes": {"8": 0x80}, "rt_stab": 50},
        )
    ]


def test_open_official_driver_is_requeued_without_replaying_old_values(monkeypatch):
    manager = _manager()
    calls = []
    outcomes = [
        SimpleNamespace(synced=False, deferred=True, detail="driver open"),
        SimpleNamespace(synced=True, deferred=False, detail="ok"),
    ]
    monkeypatch.setattr(app_module.threading, "Timer", _ManualTimer)

    def sync(profile, settings, **kwargs):
        calls.append((profile, dict(settings), dict(kwargs)))
        return outcomes.pop(0)

    monkeypatch.setattr(app_module, "sync_womier_magnetic_cache", sync)

    manager._queue_womier_cache_sync(0, key_settings={8: "first"})
    manager._drain_womier_cache_sync()
    # A newer HID-success value arrives while Womier is still open.  It must
    # win over the value that was deferred from the older drain.
    manager._queue_womier_cache_sync(0, key_settings={8: "newer"})
    manager._drain_womier_cache_sync()

    assert calls == [
        (0, {"8": "first"}, {"key_modes": None, "rt_stab": None}),
        (0, {"8": "newer"}, {"key_modes": None, "rt_stab": None}),
    ]
    assert manager._womier_cache_sync_pending == {}


def test_transient_womier_cache_error_is_retained_for_later_retry(monkeypatch):
    """A failed official-cache mirror must never discard HID-success values."""
    manager = _persistable_manager()
    monkeypatch.setattr(app_module.threading, "Timer", _ManualTimer)
    calls = []
    accepted = KeyMagneticSettings(0.45, True, 0.12, 0.56, 0.11, 0.22)

    def sync(profile, settings, **kwargs):
        calls.append((profile, dict(settings), dict(kwargs)))
        if len(calls) == 1:
            raise app_module.WomierCacheSyncError("temporary cache lock")
        return SimpleNamespace(synced=True, deferred=False, detail="ok")

    monkeypatch.setattr(app_module, "sync_womier_magnetic_cache", sync)
    manager._queue_womier_cache_sync(0, key_settings={8: accepted})
    manager._drain_womier_cache_sync()

    pending = manager._womier_cache_sync_pending
    assert pending[("sk75", 0)]["key_settings"]["8"]["actuation"] == 0.45
    assert manager._womier_cache_sync_timer.delay == app_module.WOMIER_CACHE_SYNC_ERROR_RETRY_SEC
    assert manager.config["devices"]["sk75"]["womier_cache_sync_pending"]["0"]["key_settings"]["8"]["actuation"] == 0.45

    manager._drain_womier_cache_sync()

    assert [call[1]["8"]["actuation"] for call in calls] == [0.45, 0.45]
    assert manager._womier_cache_sync_pending == {}


def test_deferred_delta_is_persisted_and_replayed_after_restart(monkeypatch):
    manager = _persistable_manager()
    monkeypatch.setattr(app_module.threading, "Timer", _ManualTimer)
    monkeypatch.setattr(
        app_module,
        "sync_womier_magnetic_cache",
        lambda *_args, **_kwargs: SimpleNamespace(
            synced=False, deferred=True, detail="driver open"
        ),
    )
    settings = KeyMagneticSettings(0.45, True, 0.12, 0.56, 0.11, 0.22)

    manager._queue_womier_cache_sync(0, key_settings={8: settings}, rt_stab=50)
    manager._drain_womier_cache_sync()
    persisted = manager.config["devices"]["sk75"]["womier_cache_sync_pending"]
    assert persisted["0"]["key_settings"]["8"]["actuation"] == 0.45
    assert persisted["0"]["rt_stab"] == 50

    restored = _persistable_manager()
    restored.config = manager.config
    replayed = []
    monkeypatch.setattr(
        app_module,
        "sync_womier_magnetic_cache",
        lambda profile, values, **kwargs: replayed.append((profile, values, kwargs))
        or SimpleNamespace(synced=True, deferred=False, detail="ok"),
    )
    restored._restore_persisted_womier_cache_sync()
    restored._drain_womier_cache_sync()

    assert replayed[0][0] == 0
    assert replayed[0][1]["8"]["rapid_release"] == 0.56
    assert replayed[0][2]["rt_stab"] == 50
    assert "womier_cache_sync_pending" not in restored.config["devices"]["sk75"]
