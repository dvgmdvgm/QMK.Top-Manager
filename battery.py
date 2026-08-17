import logging
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# A number of keyboards echo an all-zero feature report on a HID interface
# which is not responsible for the battery.  Treating that first reply as a
# real empty battery made the UI jump to 0% at random.  A real 0% reading is
# still supported, but it has to repeat once before it is published.
_ZERO_CONFIRMATION_READS = 2


@dataclass
class BatteryState:
    percent: Optional[int] = None
    charging: bool = False
    updated_at: datetime = field(default_factory=datetime.now)
    is_stale: bool = True


class BatteryMonitor:
    def __init__(
        self,
        config_battery: dict,
        usb_lock: Lock,
        get_device_path: Callable[[], Optional[str]],
        get_device_paths: Optional[Callable[[], list]] = None,
        on_working_path: Optional[Callable[[bytes], None]] = None,
        hid_device_factory: Optional[Callable[[], object]] = None,
        default_query: Optional[list[int]] = None,
    ):
        self._config = config_battery
        self._usb_lock = usb_lock
        self._get_path = get_device_path
        self._get_paths = get_device_paths
        self._on_working_path = on_working_path
        self._default_query = list(default_query or [])
        if hid_device_factory is None:
            import hid
            hid_device_factory = hid.device
        self._make_device = hid_device_factory
        self._state = BatteryState()
        self._zero_read_streak = 0

    @property
    def state(self) -> BatteryState:
        return self._state

    def read_once(self) -> None:
        paths = self._get_paths() if self._get_paths else []
        if not paths:
            path = self._get_path()
            paths = [path] if path else []
        if not paths:
            self._mark_failure("device path unavailable")
            return
        report_id = self._config.get("report_id", 0)
        query = [report_id] + list(self._config.get("query") or self._default_query)
        if len(query) < 2:
            self._mark_failure("no battery query configured")
            return
        response_length = self._config.get("response_length", 32)
        logger.debug("battery read_once %d path(s) to try", len(paths))
        # Do not return at the first parseable answer.  Some SK75 interfaces
        # answer every feature request with zeroes, while another interface
        # holds the actual battery value.  Prefer any non-zero answer found
        # during this polling pass and keep a zero candidate as a fallback.
        zero_candidate = None
        with self._usb_lock:
            for path in paths:
                try:
                    device = self._make_device()
                    device.open_path(path)
                    logger.debug("battery send_feature_report path=%s query=%s",
                                 path, [f"0x{b:02x}" for b in query])
                    device.send_feature_report(query)
                    response = device.get_feature_report(report_id, response_length)
                    logger.debug("battery get_feature_report response=%s",
                                 [f"0x{b:02x}" for b in response[:16]])
                    device.close()
                except Exception as exc:
                    logger.debug("battery read HID error on path=%s: %s", path, exc)
                    try:
                        device.close()
                    except Exception:
                        pass
                    continue
                try:
                    offset = self._config["response_offset"]
                    scale = self._config.get("response_scale", 1)
                    raw = response[offset]
                    scaled_percent = int(raw * scale)
                    percent = max(0, min(100, scaled_percent))

                    charging = False
                    ch_offset = self._config.get("charging_offset")
                    ch_mask = self._config.get("charging_mask", 0)
                    if ch_offset is not None and ch_mask:
                        charging = bool(response[ch_offset] & ch_mask)

                    logger.debug("battery parsed: raw=%d percent=%d charging=%s path=%s",
                                 raw, percent, charging, path)
                    candidate = BatteryState(
                        percent=percent,
                        charging=charging,
                        updated_at=datetime.now(),
                        is_stale=False,
                    )

                    # Only a literal zero byte is considered suspicious.
                    # Keep the old clamping behaviour for an unusual custom
                    # scale (for example a negative scale in a diagnostic
                    # profile), so those values remain deterministic.
                    if raw == 0 and scaled_percent == 0:
                        if zero_candidate is None:
                            zero_candidate = (candidate, path)
                        continue

                    self._publish_state(candidate, path)
                    self._zero_read_streak = 0
                    return
                except (IndexError, KeyError, TypeError) as exc:
                    logger.debug("battery parse error on path=%s: %s", path, exc)
                    continue

        if zero_candidate is not None:
            candidate, path = zero_candidate
            self._zero_read_streak += 1
            if self._zero_read_streak >= _ZERO_CONFIRMATION_READS:
                logger.debug("battery zero accepted after %d matching reads", self._zero_read_streak)
                self._publish_state(candidate, path)
                self._zero_read_streak = 0
            else:
                logger.debug("battery zero deferred pending confirmation")
                self._mark_failure("unconfirmed zero battery response")
            return

        self._zero_read_streak = 0
        self._mark_failure(f"all {len(paths)} paths failed")

    def _publish_state(self, state: BatteryState, path) -> None:
        """Publish a validated state and remember the matching HID interface."""
        self._state = state
        if self._on_working_path:
            self._on_working_path(path)

    def probe_battery(self, packet_data: list, path: str) -> Optional[int]:
        """Try packet_data as a battery query. Returns percent (0-100) or None."""
        report_id = self._config.get("report_id", 0)
        response_length = self._config.get("response_length", 32)
        offset = self._config.get("response_offset")
        scale = self._config.get("response_scale", 1)
        if offset is None:
            return None
        query = [report_id] + list(packet_data)
        logger.debug("battery probe query=%s path=%s", [f"0x{b:02x}" for b in query], path)
        with self._usb_lock:
            try:
                device = self._make_device()
                device.open_path(path)
                device.set_nonblocking(1)
                device.send_feature_report(query)
                response = device.get_feature_report(report_id, response_length)
                device.close()
            except Exception as exc:
                logger.debug("battery probe HID error: %s", exc)
                try:
                    device.close()
                except Exception:
                    pass
                return None
        try:
            raw = response[offset]
            percent = int(raw * scale)
            logger.debug("battery probe response=%s raw=%d percent=%d",
                         [f"0x{b:02x}" for b in response[:16]], raw, percent)
            if 0 <= percent <= 100:
                return percent
            return None
        except (IndexError, TypeError):
            return None

    def _mark_failure(self, reason: str) -> None:
        logger.debug("battery read failed: %s", reason)
        self._state = BatteryState(
            percent=None,
            charging=False,
            updated_at=datetime.now(),
            is_stale=True,
        )
