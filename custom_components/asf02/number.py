"""Number entities for the ASF02 feeder."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_ADDRESS,
    DEFAULT_PORTIONS,
    DEFAULT_SCHEDULE_PORTION,
    PORTIONS_MAX,
    PORTIONS_MIN,
    SCHEDULE_PORTION_MAX,
    SCHEDULE_PORTION_MIN,
    SCHEDULE_SLOTS,
)
from .coordinator import ASF02Coordinator
from .entity import ASF02ConfigEntry, ASF02CoordinatorEntity


def _clamp_portions(value: float) -> int:
    """Round to a whole portion and clamp to the feeder's accepted range."""
    return max(PORTIONS_MIN, min(PORTIONS_MAX, round(value)))


def _clamp_schedule_portion(value: float) -> int:
    """Round to a whole portion and clamp to the schedule slot's range."""
    return max(SCHEDULE_PORTION_MIN, min(SCHEDULE_PORTION_MAX, round(value)))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ASF02ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ASF02Coordinator = entry.runtime_data
    async_add_entities(
        [
            ASF02PortionsNumber(coordinator, entry),
            *[
                ASF02SlotPortionNumber(coordinator, entry, slot)
                for slot in range(1, SCHEDULE_SLOTS + 1)
            ],
        ]
    )


class ASF02PortionsNumber(ASF02CoordinatorEntity, RestoreEntity, NumberEntity):
    """How many portions the Feed button will dispense.

    The value lives in the entity's state, persisted across restarts by the
    restore-state helper. The Feed button reads it from the state machine at
    press time, so this entity is the single source of truth.
    """

    _attr_icon = "mdi:counter"
    _attr_mode = NumberMode.BOX
    _attr_native_value: int = DEFAULT_PORTIONS

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_translation_key = "portions"
        self._attr_unique_id = f"{entry.data[CONF_ADDRESS]}_portions"
        self._attr_native_min_value = PORTIONS_MIN
        self._attr_native_max_value = PORTIONS_MAX
        self._attr_native_step = 1

    async def async_added_to_hass(self) -> None:
        """Restore the last-used value across restarts."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        try:
            self._attr_native_value = _clamp_portions(float(last_state.state))
        except ValueError:
            pass

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = _clamp_portions(value)
        self.async_write_ha_state()


class ASF02SlotPortionNumber(ASF02CoordinatorEntity, RestoreEntity, NumberEntity):
    """Draft portion count for one schedule slot.

    A value of 0 disables the slot (it is written to the feeder but never
    fires). The value lives in the entity's state, persisted across restarts
    by the restore-state helper. The Save schedule button commits every
    slot's portion count (together with its time) to the feeder.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:counter"
    _attr_mode = NumberMode.BOX
    _attr_native_value: int = DEFAULT_SCHEDULE_PORTION

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry, slot: int) -> None:
        super().__init__(coordinator, entry)
        self._slot = slot
        self._attr_translation_key = f"slot_{slot}_portions"
        # Slot 1 keeps its pre-0.2.0 unique_id so existing entities are
        # renamed in place rather than orphaned and recreated.
        unique_key = "slot_portions" if slot == 1 else f"slot_{slot}_portions"
        self._attr_unique_id = f"{entry.data[CONF_ADDRESS]}_{unique_key}"
        self._attr_native_min_value = SCHEDULE_PORTION_MIN
        self._attr_native_max_value = SCHEDULE_PORTION_MAX
        self._attr_native_step = 1

    async def async_added_to_hass(self) -> None:
        """Restore the last-used value across restarts."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        try:
            self._attr_native_value = _clamp_schedule_portion(float(last_state.state))
        except ValueError:
            pass

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = _clamp_schedule_portion(value)
        self.async_write_ha_state()
