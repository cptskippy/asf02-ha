"""Schedule slot editor: the local schedule model (Option A).

Covers the two draft entities (slot time + slot portions), the Save
schedule button, the per-entry model store, seed-from-read on first save,
gap filling, poll-side model refresh, and restart persistence.
"""

from __future__ import annotations

import pytest
from asf02.protocol import encode_plan_entry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.asf02.coordinator import ASF02Coordinator

from .conftest import fire_advertisement


async def _setup(hass, entry, patched_client) -> ASF02Coordinator:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    assert isinstance(coordinator, ASF02Coordinator)
    return coordinator


def _entities_by_key(hass, entry) -> dict[str, str]:
    registry = er.async_get(hass)
    return {
        e.unique_id.split("_", 1)[1]: e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }


async def _make_available(hass, entry, patched_client) -> ASF02Coordinator:
    """Set up the entry and mark the feeder available (one ad, no poll)."""
    coordinator = await _setup(hass, entry, patched_client)
    fire_advertisement(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    return coordinator


async def _set_draft(hass, entities: dict[str, str], time_str: str, portion: int) -> None:
    await hass.services.async_call(
        "time",
        "set_value",
        {"entity_id": entities["slot_time"], "time": time_str},
        blocking=True,
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": entities["slot_portions"], "value": portion},
        blocking=True,
    )


async def _press_save(hass, entities: dict[str, str]) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": entities["save_schedule"]}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_save_slot_seeds_and_merges(hass, entry, patched_client, fake_state):
    """First save seeds the model from the device read, then merges in."""
    # The device already has an entry in slot 1 and its read answers.
    fake_state.schedule[0] = encode_plan_entry(7, 30, 2, True)

    coordinator = await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)
    assert coordinator.local_schedule == []

    hass.config_entries.async_update_entry(entry, options={"schedule_slot": 2})
    await _set_draft(hass, entities, "18:30:00", 4)
    await _press_save(hass, entities)

    # The device got the original entry plus the new one (front-packed).
    assert fake_state.schedule[0] == encode_plan_entry(7, 30, 2, True)
    assert fake_state.schedule[1] == encode_plan_entry(18, 30, 4, True)
    assert all(slot in ("",) for slot in fake_state.schedule[2:])

    # The model refreshed from the device's write confirmation.
    assert coordinator.local_schedule == [
        {"hour": 7, "minute": 30, "portion": 2, "enabled": True},
        {"hour": 18, "minute": 30, "portion": 4, "enabled": True},
    ]
    assert coordinator.schedule_source == "last_write"


async def test_save_slot_gap_fill_on_silent_read(hass, entry, patched_client, fake_state):
    """Silent read (the firmware quirk): gap slots become disabled entries."""
    coordinator = await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)

    hass.config_entries.async_update_entry(entry, options={"schedule_slot": 3})
    await _set_draft(hass, entities, "06:15:00", 2)
    await _press_save(hass, entities)

    # Slots 1-2 are disabled gap fills; slot 3 carries the draft.
    assert fake_state.schedule[0] == encode_plan_entry(6, 15, 2, False)
    assert fake_state.schedule[1] == encode_plan_entry(6, 15, 2, False)
    assert fake_state.schedule[2] == encode_plan_entry(6, 15, 2, True)

    model = coordinator.local_schedule
    assert len(model) == 3
    assert model[2]["enabled"] is True
    assert model[0]["enabled"] is False
    assert coordinator.schedule_source == "last_write"


async def test_schedule_sensor_shows_local_model(hass, entry, patched_client, fake_state):
    """The Schedule sensor mirrors the local model, not the device read."""
    await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)

    await _set_draft(hass, entities, "12:00:00", 3)
    await _press_save(hass, entities)

    schedule = hass.states.get(entities["schedule"])
    assert schedule.state == "12:00 ×3"
    assert schedule.attributes["entry_count"] == 1
    assert schedule.attributes["schedule_source"] == "last_write"


async def test_poll_refreshes_model_from_read(hass, entry, patched_client, fake_state):
    """Non-empty reads refresh the model; empty reads never wipe it."""
    coordinator = await _make_available(hass, entry, patched_client)

    # Empty read (the quirk): the model stays empty.
    await coordinator._do_poll(coordinator._last_service_info)
    assert coordinator.local_schedule == []
    assert coordinator.schedule_source is None

    # A read that answers is device truth and refreshes the model.
    fake_state.schedule[0] = encode_plan_entry(9, 0, 1, True)
    fake_state.schedule[1] = encode_plan_entry(21, 0, 1, False)
    await coordinator._do_poll(coordinator._last_service_info)
    assert coordinator.local_schedule == [
        {"hour": 9, "minute": 0, "portion": 1, "enabled": True},
        {"hour": 21, "minute": 0, "portion": 1, "enabled": False},
    ]
    assert coordinator.schedule_source == "device_read"

    # The device's schedule later reports empty (entries consumed after
    # firing): an empty read must not wipe the local model.
    fake_state.schedule = [""] * 10
    await coordinator._do_poll(coordinator._last_service_info)
    assert len(coordinator.local_schedule) == 2


async def test_save_button_requires_drafts(hass, entry, patched_client, fake_state):
    """A missing draft entity fails the save with a clear error (no write)."""
    await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)

    registry = er.async_get(hass)
    registry.async_remove(entities["slot_time"])
    await hass.async_block_till_done(wait_background_tasks=True)

    with pytest.raises(HomeAssistantError, match="must be set before saving"):
        await hass.services.async_call(
            "button", "press", {"entity_id": entities["save_schedule"]}, blocking=True
        )
    # No write reached the device.
    assert fake_state.schedule == [""] * 10
    assert fake_state.connect_count == 0


async def test_save_slot_validation(hass, entry, patched_client, fake_state):
    """The coordinator rejects out-of-range slot/time/portion values."""
    coordinator = await _make_available(hass, entry, patched_client)

    with pytest.raises(ValueError, match="slot must be"):
        await coordinator.async_save_slot(11, 12, 0, 1)
    with pytest.raises(ValueError, match="invalid time"):
        await coordinator.async_save_slot(1, 24, 0, 1)
    with pytest.raises(ValueError, match="portion must be"):
        await coordinator.async_save_slot(1, 12, 0, 100)
    # Nothing reached the device.
    assert fake_state.connect_count == 0


async def test_editor_and_model_survive_restart(hass, entry, patched_client, fake_state):
    """The drafts (restore-state) and the model (per-entry store) persist."""
    from pytest_homeassistant_custom_component.common import (
        async_mock_restore_state_shutdown_restart,
    )

    coordinator = await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)

    await _set_draft(hass, entities, "07:15:00", 3)
    await _press_save(hass, entities)
    assert coordinator.local_schedule == [{"hour": 7, "minute": 15, "portion": 3, "enabled": True}]

    # Simulate a HA restart: dump + reload the restore-state store, then
    # re-set up the entry.
    await async_mock_restore_state_shutdown_restart(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    # The drafts are restored...
    assert hass.states.get(entities["slot_time"]).state == "07:15:00"
    assert hass.states.get(entities["slot_portions"]).state == "3"
    # ...and the model is reloaded from the per-entry store.
    new_coordinator = entry.runtime_data
    assert new_coordinator.local_schedule == [
        {"hour": 7, "minute": 15, "portion": 3, "enabled": True}
    ]
    assert new_coordinator.schedule_source == "last_write"
