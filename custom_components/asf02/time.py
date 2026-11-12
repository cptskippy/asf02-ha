"""Time entity for the ASF02 feeder (schedule slot editor)."""

from __future__ import annotations

from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_ADDRESS
from .coordinator import ASF02Coordinator
from .entity import ASF02ConfigEntry, ASF02CoordinatorEntity

_DEFAULT_SLOT_TIME = dt_time(8, 0, 0)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ASF02ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ASF02Coordinator = entry.runtime_data
    async_add_entities([ASF02SlotTimeEntity(coordinator, entry)])


class ASF02SlotTimeEntity(ASF02CoordinatorEntity, RestoreEntity, TimeEntity):
    """Draft time for the schedule slot targeted by the Options "Schedule slot".

    The value lives in the entity's state, persisted across restarts by the
    restore-state helper. The Save schedule button commits it to the feeder
    together with the slot portion number.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-edit-outline"
    _attr_native_value: dt_time = _DEFAULT_SLOT_TIME

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_translation_key = "slot_time"
        self._attr_unique_id = f"{entry.data[CONF_ADDRESS]}_slot_time"

    async def async_added_to_hass(self) -> None:
        """Restore the last-used time across restarts."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        try:
            parsed = dt_time.fromisoformat(last_state.state)
        except ValueError:
            return
        self._attr_native_value = parsed.replace(second=0, microsecond=0)

    async def async_set_value(self, value: dt_time) -> None:
        self._attr_native_value = value.replace(second=0, microsecond=0)
        self.async_write_ha_state()
