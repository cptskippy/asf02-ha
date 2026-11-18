"""Time entities for the ASF02 feeder (one per schedule slot)."""

from __future__ import annotations

from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_ADDRESS, SCHEDULE_SLOTS
from .coordinator import ASF02Coordinator
from .entity import ASF02ConfigEntry, ASF02CoordinatorEntity

_DEFAULT_SLOT_TIME = dt_time(8, 0, 0)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ASF02ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ASF02Coordinator = entry.runtime_data
    async_add_entities(
        [ASF02SlotTimeEntity(coordinator, entry, slot) for slot in range(1, SCHEDULE_SLOTS + 1)]
    )


class ASF02SlotTimeEntity(ASF02CoordinatorEntity, RestoreEntity, TimeEntity):
    """Draft time for one schedule slot.

    The value lives in the entity's state, persisted across restarts by the
    restore-state helper. The Save schedule button commits every slot's time
    (together with its portion count) to the feeder.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-edit-outline"
    _attr_native_value: dt_time = _DEFAULT_SLOT_TIME

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry, slot: int) -> None:
        super().__init__(coordinator, entry)
        self._slot = slot
        self._attr_translation_key = f"slot_{slot}_time"
        # Slot 1 keeps its pre-0.2.0 unique_id so existing entities are
        # renamed in place rather than orphaned and recreated.
        unique_key = "slot_time" if slot == 1 else f"slot_{slot}_time"
        self._attr_unique_id = f"{entry.data[CONF_ADDRESS]}_{unique_key}"

    async def async_added_to_hass(self) -> None:
        """Restore the last-used time across restarts."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            try:
                parsed = dt_time.fromisoformat(last_state.state)
                self._attr_native_value = parsed.replace(second=0, microsecond=0)
            except ValueError:
                pass
        # Register with the coordinator so the Save button can read this slot's
        # draft from the entity object (in memory), not the state machine (#24).
        self.coordinator.register_slot_time_draft(self._slot, self)

    def draft_value(self) -> dt_time:
        """The slot's draft time (the entity's in-memory value)."""
        return self._attr_native_value

    def sync_draft(self, value: dt_time) -> None:
        """Push a synced time into the draft (from the coordinator's model).

        Keeps the draft in sync when the model changes outside a save (a
        service write or a device read), so a later Save doesn't revert it.
        """
        self._attr_native_value = value.replace(second=0, microsecond=0)
        self.async_write_ha_state()

    async def async_set_value(self, value: dt_time) -> None:
        self._attr_native_value = value.replace(second=0, microsecond=0)
        self.async_write_ha_state()
