"""Sensors for the ASF02 feeder."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ADDRESS
from .coordinator import ASF02Coordinator
from .entity import ASF02ConfigEntry, ASF02CoordinatorEntity


def _fmt_schedule(snapshot) -> tuple[str, dict[str, Any]]:
    """Human string + machine-readable attributes for the schedule."""
    entries = snapshot.schedule
    if not entries:
        return "—", {}
    human = " · ".join(
        f"{e.hour:02d}:{e.minute:02d} ×{e.portion}" + ("" if e.enabled else " (off)")
        for e in entries
    )
    return human, {"entry_count": len(entries)}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ASF02ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ASF02Coordinator = entry.runtime_data
    async_add_entities(
        [
            ASF02BatterySensor(coordinator, entry),
            ASF02FeedCountSensor(coordinator, entry),
            ASF02ScheduleSensor(coordinator, entry),
            ASF02DeviceTimeSensor(coordinator, entry),
        ]
    )


class ASF02Sensor(ASF02CoordinatorEntity, SensorEntity):
    """Base for ASF02 sensors (coordinator data, ad-driven availability)."""

    def __init__(
        self,
        coordinator: ASF02Coordinator,
        entry: ASF02ConfigEntry,
        *,
        translation_key: str,
        unique_key: str,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{entry.data[CONF_ADDRESS]}_{unique_key}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {**super().common_attributes, **self._sensor_attributes()}

    def _sensor_attributes(self) -> dict[str, Any]:
        return {}


class ASF02BatterySensor(ASF02Sensor):
    """Raw battery value.

    Deliberately NO battery device class: that class assumes a 0–100%
    value, but the raw reading is power-source-dependent (≈522 fresh NiMH,
    ≈428–461 on USB) and would render as a nonsense percentage. The
    actionable semantic is binary_sensor ..._battery_low (b ≤ 410).
    """

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(coordinator, entry, translation_key="battery", unique_key="battery")

    @property
    def native_value(self) -> int | None:
        return self.snapshot.battery_raw if self.snapshot else None


class ASF02FeedCountSensor(ASF02Sensor):
    """Total portions dispensed (live-incrementing, monotonically increasing)."""

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(coordinator, entry, translation_key="feed_count", unique_key="feed_count")
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = "portions"

    @property
    def native_value(self) -> int | None:
        return self.snapshot.feed_count if self.snapshot else None

    def _sensor_attributes(self) -> dict[str, Any]:
        if not self.snapshot:
            return {}
        return {
            "feed_count_mirror": self.snapshot.feed_count_mirror,
            "last_feed_at": (
                self.coordinator.last_feed_at.isoformat() if self.coordinator.last_feed_at else None
            ),
        }


class ASF02ScheduleSensor(ASF02Sensor):
    """The 10-slot schedule as a human string; slots in attributes."""

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(coordinator, entry, translation_key="schedule", unique_key="schedule")

    @property
    def native_value(self) -> str | None:
        if not self.snapshot:
            return None
        return _fmt_schedule(self.snapshot)[0]

    def _sensor_attributes(self) -> dict[str, Any]:
        if not self.snapshot:
            return {}
        return _fmt_schedule(self.snapshot)[1]


class ASF02DeviceTimeSensor(ASF02Sensor):
    """What the *device* clock says (drift diagnostic)."""

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(
            coordinator, entry, translation_key="device_time", unique_key="device_time"
        )
        self._attr_device_class = "timestamp"

    @property
    def native_value(self) -> datetime | None:
        return self.snapshot.device_time if self.snapshot else None
