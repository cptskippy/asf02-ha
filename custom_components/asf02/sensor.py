"""Sensors for the ASF02 feeder."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ADDRESS
from .coordinator import ASF02Coordinator
from .entity import ASF02ConfigEntry, ASF02CoordinatorEntity


def _fmt_schedule(entries: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Human string + machine-readable attributes for the schedule model."""
    if not entries:
        return "—", {}
    human = " · ".join(
        f"{e['hour']:02d}:{e['minute']:02d} ×{e['portion']}"
        + ("" if e.get("enabled", True) else " (off)")
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
    ≈428–461 on USB) and would render as a nonsense percentage.
    """

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(coordinator, entry, translation_key="battery", unique_key="battery")
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> int | None:
        return self.snapshot.battery_raw if self.snapshot else None


class ASF02FeedCountSensor(ASF02Sensor):
    """Total portions dispensed (live-incrementing, monotonically increasing)."""

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(coordinator, entry, translation_key="feed_count", unique_key="feed_count")
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
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
    """The schedule per HA's local model, as a human string.

    Backed by the coordinator's local schedule model, not the device read
    (the read is unreliable on in-use firmware). ``schedule_source`` says
    where the model last came from: ``device_read`` (a poll read that
    answered), ``last_write`` (the device's confirmation of the last save),
    or ``local`` (built locally, unconfirmed).
    """

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(coordinator, entry, translation_key="schedule", unique_key="schedule")

    @property
    def native_value(self) -> str | None:
        # The model is local state (loaded at setup), not a live device read —
        # no snapshot gate. Availability stays ad-driven like the other
        # entities, so this is unavailable until the first advertisement.
        return _fmt_schedule(self.coordinator.local_schedule)[0]

    def _sensor_attributes(self) -> dict[str, Any]:
        attrs = dict(_fmt_schedule(self.coordinator.local_schedule)[1])
        attrs["schedule_source"] = self.coordinator.schedule_source
        return attrs


class ASF02DeviceTimeSensor(ASF02Sensor):
    """What the *device* clock says (drift diagnostic)."""

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(
            coordinator, entry, translation_key="device_time", unique_key="device_time"
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_class = "timestamp"

    @property
    def native_value(self) -> datetime | None:
        return self.snapshot.device_time if self.snapshot else None
