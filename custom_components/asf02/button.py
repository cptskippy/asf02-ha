"""Button entities for the ASF02 feeder."""

from __future__ import annotations

import logging

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

    Reads each slot's draft via the coordinator (from the draft entity
    objects in memory, not the state machine) and writes the full positional
    10-slot schedule. A slot whose portion count is 0 is written as disabled,
    and a slot with no draft (its entity removed, or platform setup not
    complete) is written empty. Reading from the entity objects — not the
    state machine — means a transiently unavailable state can't silently zero
    a slot on save. The device's write-confirmation array becomes the new
    local model.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:content-save"

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(
            coordinator, entry, translation_key="save_schedule", unique_key="save_schedule"
        )

    def _entries(self) -> list[tuple[int, int, int, bool]]:
        """Build the full 10-slot schedule from the slot drafts.

        A slot with no draft (the user removed the entity, or platform setup
        is incomplete) is written empty rather than given an arbitrary
        default.
        """
        entries: list[tuple[int, int, int, bool]] = []
        for slot in range(1, SCHEDULE_SLOTS + 1):
            draft = self.coordinator.draft_slot(slot)
            if draft is None:
                entries.append((0, 0, 0, False))
            else:
                hour, minute, portion = draft
                entries.append((hour, minute, portion, portion > 0))
        return entries

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
