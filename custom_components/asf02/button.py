"""Button entities for the ASF02 feeder."""

from __future__ import annotations

import logging
from datetime import time as dt_time

from bleak import BleakError
from homeassistant.components.button import ButtonEntity
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import UpdateFailed

from asf02 import ASF02Error

from .const import (
    CONF_ADDRESS,
    DEFAULT_PORTIONS,
    DOMAIN,
    PORTIONS_MAX,
    PORTIONS_MIN,
    SCHEDULE_SLOTS,
)
from .coordinator import ASF02Coordinator
from .entity import ASF02ConfigEntry, ASF02CoordinatorEntity

_LOGGER = logging.getLogger(__name__)


def _clamp_portions(value: float) -> int:
    """Round to a whole portion and clamp to the feeder's accepted range."""
    return max(PORTIONS_MIN, min(PORTIONS_MAX, round(value)))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ASF02ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ASF02Coordinator = entry.runtime_data
    async_add_entities(
        [
            ASF02FeedButton(coordinator, entry),
            ASF02RefreshButton(coordinator, entry),
            ASF02SaveScheduleButton(coordinator, entry),
            ASF02ClearScheduleButton(coordinator, entry),
        ]
    )


class ASF02Button(ASF02CoordinatorEntity, ButtonEntity):
    """Base for ASF02 buttons."""

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


class ASF02FeedButton(ASF02Button):
    """Dispenses the number of portions set in the Portions number entity.

    Non-blocking, like the `asf02.feed` service default: it returns once the
    feeder accepts the command. Progress shows up in the feed count sensor.
    """

    _attr_icon = "mdi:paw"

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(coordinator, entry, translation_key="feed", unique_key="feed")

    def _portions(self) -> int:
        """The Portions number's current value (its state is the source of truth)."""
        registry = er.async_get(self.hass)
        number_entity_id = registry.async_get_entity_id(
            "number", DOMAIN, f"{self.coordinator.address}_portions"
        )
        state = self.hass.states.get(number_entity_id) if number_entity_id else None
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.debug(
                "%s: Portions number not available, falling back to %s",
                self.coordinator.address,
                DEFAULT_PORTIONS,
            )
            return DEFAULT_PORTIONS
        try:
            return _clamp_portions(float(state.state))
        except ValueError:
            return DEFAULT_PORTIONS

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_feed(self._portions())
        except (BleakError, ASF02Error, TimeoutError) as err:
            raise HomeAssistantError(f"feed failed: {err}") from err


class ASF02RefreshButton(ASF02Button):
    """Poll the feeder now (bypass the interval timer) and update its entities."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(coordinator, entry, translation_key="refresh", unique_key="refresh")

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_refresh_now()
        except (UpdateFailed, BleakError, ASF02Error, TimeoutError) as err:
            raise HomeAssistantError(f"refresh failed: {err}") from err


class ASF02SaveScheduleButton(ASF02Button):
    """Commit every slot's time + portion drafts to the feeder's schedule.

    Reads the 20 draft entities (Slot N time, Slot N portions) from the
    state machine and writes the full positional 10-slot schedule. A slot
    whose portion count is 0 is written as disabled. The device's
    write-confirmation array becomes the new local model.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:content-save"

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(
            coordinator, entry, translation_key="save_schedule", unique_key="save_schedule"
        )

    @staticmethod
    def _slot_keys(slot: int) -> tuple[str, str]:
        """(time unique_key, portions unique_key) for a slot. Slot 1 keeps
        its pre-0.2.0 keys so existing entities are still found."""
        time_key = "slot_time" if slot == 1 else f"slot_{slot}_time"
        portion_key = "slot_portions" if slot == 1 else f"slot_{slot}_portions"
        return time_key, portion_key

    def _draft_slot(self, slot: int) -> tuple[int, int, int]:
        """(hour, minute, portion) for one slot.

        A missing or unavailable draft entity defaults to 08:00 and 0
        portions (disabled), so saving always works even if some slot
        entities have been removed from the registry.
        """
        registry = er.async_get(self.hass)
        address = self.coordinator.address
        time_key, portion_key = self._slot_keys(slot)
        hour, minute = 8, 0
        portion = 0
        time_id = registry.async_get_entity_id("time", DOMAIN, f"{address}_{time_key}")
        time_state = self.hass.states.get(time_id) if time_id else None
        if time_state is not None and time_state.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            try:
                parsed = dt_time.fromisoformat(time_state.state)
                hour, minute = parsed.hour, parsed.minute
            except ValueError:
                pass
        portion_id = registry.async_get_entity_id("number", DOMAIN, f"{address}_{portion_key}")
        portion_state = self.hass.states.get(portion_id) if portion_id else None
        if portion_state is not None and portion_state.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            try:
                portion = int(float(portion_state.state))
            except ValueError:
                portion = 0
        return hour, minute, portion

    def _entries(self) -> list[tuple[int, int, int, bool]]:
        return [
            (hour, minute, portion, portion > 0)
            for hour, minute, portion in (
                self._draft_slot(slot) for slot in range(1, SCHEDULE_SLOTS + 1)
            )
        ]

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_save_schedule(self._entries())
        except (BleakError, ASF02Error, TimeoutError, ValueError) as err:
            raise HomeAssistantError(f"schedule save failed: {err}") from err


class ASF02ClearScheduleButton(ASF02Button):
    """Clear the feeder's entire schedule (all 10 slots)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:content-clear"

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(
            coordinator, entry, translation_key="clear_schedule", unique_key="clear_schedule"
        )

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_clear_schedule()
        except (BleakError, ASF02Error, TimeoutError) as err:
            raise HomeAssistantError(f"schedule clear failed: {err}") from err
