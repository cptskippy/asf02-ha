"""Binary sensors for the ASF02 feeder."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_ADDRESS, FED_RECENTLY_WINDOW_SECONDS
from .coordinator import ASF02Coordinator
from .entity import ASF02ConfigEntry, ASF02CoordinatorEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ASF02ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ASF02Coordinator = entry.runtime_data
    async_add_entities(
        [
            ASF02FeedingBinarySensor(coordinator, entry),
            ASF02BatteryLowBinarySensor(coordinator, entry),
            ASF02FedRecentlyBinarySensor(coordinator, entry),
        ]
    )


class ASF02BinarySensor(ASF02CoordinatorEntity, BinarySensorEntity):
    """Base for ASF02 binary sensors."""

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


class ASF02FeedingBinarySensor(ASF02BinarySensor):
    """On while the motor is dispensing (status.s == 1)."""

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(coordinator, entry, translation_key="feeding", unique_key="feeding")

    @property
    def is_on(self) -> bool | None:
        return bool(self.snapshot.state) if self.snapshot else None


class ASF02BatteryLowBinarySensor(ASF02BinarySensor):
    """On when the raw battery value is at/below the vendor low threshold (410)."""

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(
            coordinator,
            entry,
            translation_key="battery_low",
            unique_key="battery_low",
        )

    @property
    def is_on(self) -> bool | None:
        return self.snapshot.battery_low if self.snapshot else None


class ASF02FedRecentlyBinarySensor(ASF02BinarySensor):
    """On for 10 minutes after the feed counter increased."""

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(
            coordinator,
            entry,
            translation_key="fed_recently",
            unique_key="fed_recently",
        )

    @property
    def is_on(self) -> bool | None:
        last_feed_at = self.coordinator.last_feed_at
        if last_feed_at is None:
            return False
        age = (
            dt_util.now(dt_util.get_time_zone(self.hass.config.time_zone)) - last_feed_at
        ).total_seconds()
        return 0 <= age <= FED_RECENTLY_WINDOW_SECONDS
