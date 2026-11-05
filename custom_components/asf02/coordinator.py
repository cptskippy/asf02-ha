"""Data acquisition: timer-driven, GATT-polling coordinator for ASF02 feeders.

Built on core's ``ActiveBluetoothDataUpdateCoordinator``: the feeder's
advertisements carry no payload and are treated only as a liveness signal —
they mark the device available, and mark it unavailable when ads stop.

The poll itself is driven by a wall-clock timer (``poll_interval_seconds``),
not by the arrival of an advertisement: this device is always in range, and
gating the poll on advertisement arrival starves the sensors whenever the
advertisements go sparse (observed: HA received the feeder's ads only a
handful of times over five hours, so ad-gated polls fired once or twice an
hour). The timer schedules a poll using the last known device; an
advertisement, when it does arrive, simply accelerates the next due poll.
Each poll is one transient GATT session: connect → unlock → time sync →
info/status/schedule → disconnect.

All GATT sessions (polls and service calls) are serialized through
``session_lock``; the feeder is single-connection and re-locks on every
reconnect, so every session re-unlocks.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import zlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from bleak import BleakError
from homeassistant.components.bluetooth import BluetoothScanningMode
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.components.bluetooth.models import BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from asf02 import (
    ASF02Client,
    ASF02Error,
    ASF02LockedError,
)
from asf02.protocol import PlanEntry

from .const import (
    BATTERY_LOW_THRESHOLD,
    DEFAULT_POLL_MINUTES,
    EVENT_FEED,
    FEED_SECONDS_PER_PORTION,
    FEED_WAIT_MARGIN,
    FEED_WAIT_POLL_INTERVAL,
    FEED_WAIT_TIMEOUT,
    OPTION_POLL_MINUTES,
)
from .managed_client import ManagedClientError, make_client_factory

_LOGGER = logging.getLogger(__name__)

# Default poll interval: 15 minutes (see const.OPTION_POLL_MINUTES /
# coordinator.poll_interval_seconds; options-adjustable 1 min – 1 h). A
# session is ~10 s of GATT, so per feeder that is well under 10% slot
# duty-cycle.
# Stagger each feeder's first poll so several feeders don't all connect at
# once (protects 3-slot ESPHome proxies). crc32 instead of hash(): str
# hashing is salted per process and would re-shuffle the stagger on every
# HA restart.
POLL_STAGGER_MAX_SECONDS = 300.0


@dataclass(frozen=True)
class FeederSnapshot:
    """One full read of the feeder."""

    firmware: str
    hw_version: str
    model: str
    serial: str
    state: int
    feed_count: int
    feed_count_mirror: int
    battery_raw: int
    uptime_raw: int
    schedule: tuple[PlanEntry, ...]
    device_time: datetime.datetime | None
    fetched_at: datetime.datetime
    source: str | None = None
    battery_low: bool = False


class ASF02Coordinator(ActiveBluetoothDataUpdateCoordinator[FeederSnapshot]):
    """Poll the feeder with transient GATT sessions on an interval timer.

    Advertisements mark availability and can accelerate a poll, but the poll
    cadence is set by the wall-clock timer, not by ad arrival.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        *,
        address: str,
        token: str,
        title: str,
        config_entry: Any,
    ) -> None:
        super().__init__(
            hass,
            logger,
            address=address,
            mode=BluetoothScanningMode.ACTIVE,
            connectable=True,
            needs_poll_method=self._needs_poll,
            poll_method=self._do_poll,
        )
        self.config_entry = config_entry
        self.token = token
        self.title = title
        self._session_lock = asyncio.Lock()
        self._last_feed_count: int | None = None
        self._last_feed_at: datetime.datetime | None = None
        self._first_poll_at: float | None = None
        self._shutdown_requested = False
        self._feed_in_progress = False
        self._last_ad_log_at: float | None = None

    @property
    def last_feed_at(self) -> datetime.datetime | None:
        """When the feed counter last increased (drives fed_recently)."""
        return self._last_feed_at

    @property
    def session_lock(self) -> asyncio.Lock:
        return self._session_lock

    @property
    def tz_offset_seconds(self) -> int:
        """HA's configured UTC offset in seconds, DST included.

        Matches the vendor app, which sends (rawOffset + dstSavings)/1000.
        """
        zone = dt_util.get_time_zone(self.hass.config.time_zone)
        offset = dt_util.now(zone).utcoffset()
        return int(offset.total_seconds()) if offset else 0

    @property
    def poll_interval_seconds(self) -> float:
        """Poll cadence from the options flow (1 min – 1 h, default 15 min).

        Read live on every ad event so an options change takes effect at the
        next poll without a reload.
        """
        minutes = self.config_entry.options.get(OPTION_POLL_MINUTES, DEFAULT_POLL_MINUTES)
        return float(minutes) * 60.0

    # ------------------------------------------------------------------
    # Ad handling (availability only — the timer drives the poll)
    # ------------------------------------------------------------------
    @callback
    def _needs_poll(
        self,
        service_info: BluetoothServiceInfoBleak,
        seconds_since_last_poll: float | None,
    ) -> bool:
        """Ad events never schedule a poll — the timer is the sole trigger.

        Returning False stops the base class from firing the debounced poll
        on an advertisement. The ad's only jobs here are marking the device
        available and storing its BLEDevice (handled by the base before this
        is called), which the timer then uses to connect. A throttled
        diagnostic confirms ads are still reaching the coordinator.
        """
        now = self.hass.loop.time()
        if self._last_ad_log_at is None or now - self._last_ad_log_at >= 30.0:
            self._last_ad_log_at = now
            _LOGGER.debug(
                "%s: ad received (source=%s); availability only, poll is timer-driven",
                self.address,
                getattr(service_info, "source", None),
            )
        return False

    async def _do_poll(self, service_info: BluetoothServiceInfoBleak) -> FeederSnapshot:
        source = getattr(service_info, "source", None)
        _LOGGER.debug("%s: poll attempt starting (source=%s)", self.address, source)
        async with self._session_lock:
            try:
                snapshot = await self._session_read(service_info)
                _LOGGER.debug("%s: poll succeeded (source=%s)", self.address, source)
                return snapshot
            except ManagedClientError as err:
                raise UpdateFailed(f"no connectable Bluetooth path via {source}: {err}") from err
            except ASF02LockedError as err:
                # Token problem (e.g. device rebound via the vendor app).
                # The base poll path swallows ConfigEntryError, so this
                # surfaces as a persistent UpdateFailed with the remediation
                # in the message; re-configuring the entry (generated-token
                # path) re-pairs the feeder.
                raise UpdateFailed(
                    f"feeder rejected the stored token via {source} ({err}); "
                    "re-configure the entry — the 'generate a new token' path re-pairs it"
                ) from err
            except (TimeoutError, BleakError, ASF02Error) as err:
                _LOGGER.debug("%s: poll session error (source=%s): %s", self.address, source, err)
                raise UpdateFailed(f"feeder session failed via {source}: {err}") from err

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------
    def _session_client(self) -> ASF02Client:
        return ASF02Client(
            self.address,
            self.token,
            client_factory=make_client_factory(self.hass, self.address, self.title),
        )

    def _device_time(self, unlock_result: Any) -> datetime.datetime | None:
        """Device clock from the unlock response (epoch seconds), local tz."""
        if not isinstance(unlock_result, dict):
            return None
        value = unlock_result.get("t")
        if not isinstance(value, (int, float)):
            return None
        return datetime.datetime.fromtimestamp(int(value), tz=datetime.UTC).astimezone()

    async def _session_read(self, service_info: BluetoothServiceInfoBleak) -> FeederSnapshot:
        """One full poll: unlock → time sync → info/status/schedule."""
        async with self._session_client() as client:
            unlock_result = await client.unlock()
            await client.sync_time(self.tz_offset_seconds)
            info = await client.info()
            status = await client.status()
            schedule = await client.get_schedule()

        battery_raw = int(status.get("b", 0))
        snapshot = FeederSnapshot(
            firmware=str(info.get("f", "")),
            hw_version=str(info.get("h", "")),
            model=str(info.get("m", "")),
            serial=str(info.get("s", "")),
            state=int(status.get("s", 0)),
            feed_count=int(status.get("n", 0)),
            feed_count_mirror=int(status.get("t", 0)),
            battery_raw=battery_raw,
            uptime_raw=int(status.get("u", 0)),
            schedule=tuple(schedule),
            device_time=self._device_time(unlock_result),
            fetched_at=dt_util.now(dt_util.get_time_zone(self.hass.config.time_zone)),
            source=getattr(service_info, "source", None),
            battery_low=battery_raw <= BATTERY_LOW_THRESHOLD,
        )
        self._maybe_fire_feed_event(snapshot)
        return snapshot

    def _maybe_fire_feed_event(self, snapshot: FeederSnapshot) -> None:
        prev = self._last_feed_count
        self._last_feed_count = snapshot.feed_count
        if prev is not None and snapshot.feed_count > prev:
            self._last_feed_at = snapshot.fetched_at
            self._fire_feed_event(snapshot.feed_count, snapshot.fetched_at)

    def _fire_feed_event(self, new_count: int, detected_at: datetime.datetime) -> None:
        self.hass.bus.async_fire(
            EVENT_FEED,
            {
                "address": self.address,
                "new_count": new_count,
                "detected_at": detected_at.isoformat(),
            },
        )

    # ------------------------------------------------------------------
    # Service sessions
    # ------------------------------------------------------------------
    async def async_feed(self, portions: int, *, wait: bool = False) -> dict:
        """Dispense portions; ``wait`` blocks until the dispense completes."""
        if not isinstance(portions, int) or not 1 <= portions <= 99:
            raise ValueError("portions must be an integer 1..99")
        self._feed_in_progress = True
        try:
            async with self._session_lock, self._session_client() as client:
                await client.unlock()
                ack = await client.feed(portions)
                status = await client.status()
                if wait:
                    status = await self._await_completion(client, portions, status)
        finally:
            self._feed_in_progress = False
        feed_count = int(status.get("n", 0))
        self._maybe_fire_feed_event_from_count(feed_count)
        return {"fed": bool(ack), "portions": portions, "feed_count": feed_count}

    def _maybe_fire_feed_event_from_count(self, feed_count: int) -> None:
        prev = self._last_feed_count
        self._last_feed_count = feed_count
        if prev is not None and feed_count > prev:
            self._last_feed_at = dt_util.now(dt_util.get_time_zone(self.hass.config.time_zone))
            self._fire_feed_event(feed_count, self._last_feed_at)

    async def _await_completion(self, client: ASF02Client, portions: int, status: dict) -> dict:
        """Poll status() until s==0 (mid-feed polling, verified reliable).

        Fallback if the link drops mid-feed (observed once pre-2026-09-18,
        never in the dedicated probes): wait out the remaining estimate,
        reconnect, re-read once.
        """
        deadline = self.hass.loop.time() + FEED_WAIT_TIMEOUT
        while int(status.get("s", 0)) == 1:
            remaining = deadline - self.hass.loop.time()
            if remaining <= 0:
                raise UpdateFailed("timed out waiting for feed to finish")
            try:
                await asyncio.sleep(min(FEED_WAIT_POLL_INTERVAL, remaining))
                status = await client.status()
            except TimeoutError, BleakError, ASF02Error:
                estimated = max(0.0, portions * FEED_SECONDS_PER_PORTION - FEED_WAIT_POLL_INTERVAL)
                await asyncio.sleep(min(remaining, estimated + FEED_WAIT_MARGIN))
                async with self._session_client() as retry_client:
                    await retry_client.unlock()
                    status = await retry_client.status()
                break
        return status

    async def async_stop(self) -> dict:
        async with self._session_lock, self._session_client() as client:
            await client.unlock()
            result = await client.stop()
        return {"ok": bool(result)}

    async def async_set_schedule(self, entries: list[tuple[int, int, int, bool]]) -> list:
        """Full 10-slot schedule replace; returns the device-confirmed array."""
        async with self._session_lock, self._session_client() as client:
            await client.unlock()
            return list(await client.set_schedule(entries))

    async def async_clear_schedule(self) -> list:
        async with self._session_lock, self._session_client() as client:
            await client.unlock()
            return list(await client.set_schedule([], clear=True))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @callback
    def _async_start(self) -> None:
        """Start ad tracking and the interval poll timer."""
        super()._async_start()
        # Wall-clock poll driver: fires every 60 s and schedules a poll when
        # the configured interval has elapsed. The minimum poll interval is
        # 1 min, so a 60 s tick keeps a poll within a minute of being due.
        self._on_stop.append(
            async_track_time_interval(
                self.hass,
                self._interval_poll_check,
                timedelta(seconds=60),
            )
        )

    @callback
    def _interval_poll_check(self, _utc_now: datetime.datetime) -> None:
        """Timer-driven poll: the sole poll trigger.

        Called by the wall-clock timer (``async_track_time_interval``), which
        passes the fire time in UTC (unused here; the monotonic clock below is
        the reference). Independent of advertisement arrival — the device is
        always in range, so the cadence is the timer's, not the ad stream's.
        Connects using the last known service info (the BLEDevice, learned
        from the first ad). The first poll is staggered per-address (protects
        3-slot proxies); subsequent polls run on the configured interval.
        """
        if self._shutdown_requested or self._feed_in_progress:
            return
        if self._last_service_info is None:
            # No advertisement has been seen yet — nothing to connect to.
            return
        now = self.hass.loop.time()
        if self._last_poll is None:
            # First poll: stagger per-address.
            if self._first_poll_at is None:
                self._first_poll_at = now + _first_poll_stagger(self.address)
            if now < self._first_poll_at:
                return
        elif now - self._last_poll < self.poll_interval_seconds:
            # Not due yet.
            return
        self._debounced_poll.async_schedule_call()

    async def async_refresh_now(self) -> None:
        """Trigger a poll immediately (e.g. via the refresh service).

        Awaits the GATT session (serialized through ``session_lock``),
        updates the cached snapshot and listeners, and raises
        ``UpdateFailed`` on failure so the caller can surface it. Bypasses
        the timer and the debouncer's exception-swallowing on purpose: a
        user-pressed refresh should block until the feeder answers and tell
        the user if it didn't.
        """
        if self._feed_in_progress:
            raise UpdateFailed("feeder is mid-feed; try again once it finishes")
        if self._last_service_info is None:
            raise UpdateFailed("no advertisement seen yet — nothing to connect to; wait for one")
        snapshot = await self._do_poll(self._last_service_info)
        self.data = snapshot
        self._last_poll = self.hass.loop.time()
        self._async_handle_bluetooth_poll()

    @callback
    def async_mark_shutdown(self) -> None:
        """Stop scheduling new polls (called from entry unload)."""
        self._shutdown_requested = True


def _first_poll_stagger(address: str) -> float:
    """Deterministic per-address first-poll offset (0..300 s)."""
    return zlib.crc32(address.encode("ascii")) % int(POLL_STAGGER_MAX_SECONDS)
