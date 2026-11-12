"""Schedule slot editor: ten slot pairs + Save/Clear, backed by the model.

Covers the full-schedule save (positional 10-slot write built from the 20
draft entities), the 0-portion = disabled convention, the Schedule sensor
mirroring the model (firing slots only), poll-side model refresh, the Clear
schedule button, and restart persistence of drafts + model.
"""

from __future__ import annotations

import pytest
from asf02.protocol import encode_plan_entry
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


def _slot_keys(slot: int) -> tuple[str, str]:
    time_key = "slot_time" if slot == 1 else f"slot_{slot}_time"
    portion_key = "slot_portions" if slot == 1 else f"slot_{slot}_portions"
    return time_key, portion_key


async def _make_available(hass, entry, patched_client) -> ASF02Coordinator:
    """Set up the entry and mark the feeder available (one ad, no poll)."""
    coordinator = await _setup(hass, entry, patched_client)
    fire_advertisement(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    return coordinator


async def _set_draft(hass, entities: dict[str, str], slot: int, time_str: str, portion: int):
    time_key, portion_key = _slot_keys(slot)
    await hass.services.async_call(
        "time", "set_value", {"entity_id": entities[time_key], "time": time_str}, blocking=True
    )
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entities[portion_key], "value": portion}, blocking=True
    )


async def _press_save(hass, entities: dict[str, str]):
    await hass.services.async_call(
        "button", "press", {"entity_id": entities["save_schedule"]}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def _press_clear(hass, entities: dict[str, str]):
    await hass.services.async_call(
        "button", "press", {"entity_id": entities["clear_schedule"]}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)


def _disabled_default() -> str:
    """The encoded form of a slot left at its defaults (08:00, 0 portions)."""
    return encode_plan_entry(8, 0, 0, False)


async def test_save_commits_all_slots(hass, entry, patched_client, fake_state):
    """Save writes the full positional 10-slot schedule from the drafts."""
    coordinator = await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)
    assert coordinator.local_schedule == []

    # Configure two slots; the other eight stay at their defaults (disabled).
    await _set_draft(hass, entities, 1, "07:30:00", 2)
    await _set_draft(hass, entities, 3, "18:00:00", 1)
    await _press_save(hass, entities)

    # The device got the full positional array: the two set slots enabled,
    # the rest written as disabled (default 08:00 x 0).
    assert fake_state.schedule[0] == encode_plan_entry(7, 30, 2, True)
    assert fake_state.schedule[1] == _disabled_default()
    assert fake_state.schedule[2] == encode_plan_entry(18, 0, 1, True)
    assert all(slot == _disabled_default() for slot in fake_state.schedule[3:])

    # The model holds only the firing slots (front-packed, slot order).
    assert coordinator.local_schedule == [
        {"hour": 7, "minute": 30, "portion": 2, "enabled": True},
        {"hour": 18, "minute": 0, "portion": 1, "enabled": True},
    ]
    assert coordinator.schedule_source == "last_write"


async def test_disabled_slot_portion_zero(hass, entry, patched_client, fake_state):
    """A slot set to 0 portions is written disabled and stays out of the model."""
    coordinator = await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)

    await _set_draft(hass, entities, 1, "07:00:00", 1)
    await _set_draft(hass, entities, 2, "09:00:00", 0)
    await _press_save(hass, entities)

    # Slot 2 is written with its time but 0 portions / disabled.
    assert fake_state.schedule[0] == encode_plan_entry(7, 0, 1, True)
    assert fake_state.schedule[1] == encode_plan_entry(9, 0, 0, False)

    # Only the firing slot (slot 1) is in the model.
    assert coordinator.local_schedule == [{"hour": 7, "minute": 0, "portion": 1, "enabled": True}]


async def test_schedule_sensor_shows_firing_slots(hass, entry, patched_client, fake_state):
    """The Schedule sensor mirrors the local model (firing slots only)."""
    await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)

    await _set_draft(hass, entities, 1, "12:00:00", 3)
    await _press_save(hass, entities)

    schedule = hass.states.get(entities["schedule"])
    assert schedule.state == "12:00 ×3"
    assert schedule.attributes["entry_count"] == 1
    assert schedule.attributes["schedule_source"] == "last_write"


async def test_poll_refreshes_model_from_read(hass, entry, patched_client, fake_state):
    """Non-empty reads refresh the model (firing slots); empty reads keep it."""
    coordinator = await _make_available(hass, entry, patched_client)

    # Empty read (the quirk): the model stays empty.
    await coordinator._do_poll(coordinator._last_service_info)
    assert coordinator.local_schedule == []
    assert coordinator.schedule_source is None

    # A read that answers is device truth: only the firing slot lands in the
    # model (the disabled one is filtered out).
    fake_state.schedule[0] = encode_plan_entry(9, 0, 1, True)
    fake_state.schedule[1] = encode_plan_entry(21, 0, 1, False)
    await coordinator._do_poll(coordinator._last_service_info)
    assert coordinator.local_schedule == [{"hour": 9, "minute": 0, "portion": 1, "enabled": True}]
    assert coordinator.schedule_source == "device_read"

    # The device's schedule later reports empty (entries consumed after
    # firing): an empty read must not wipe the local model.
    fake_state.schedule = [""] * 10
    await coordinator._do_poll(coordinator._last_service_info)
    assert len(coordinator.local_schedule) == 1


async def test_clear_schedule_wipes_device_and_model(hass, entry, patched_client, fake_state):
    """Clear wipes the device schedule and the local model."""
    coordinator = await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)

    await _set_draft(hass, entities, 1, "07:00:00", 2)
    await _press_save(hass, entities)
    assert coordinator.local_schedule == [{"hour": 7, "minute": 0, "portion": 2, "enabled": True}]

    await _press_clear(hass, entities)

    # The device schedule is cleared (clear marker + empties).
    assert fake_state.schedule[0] == "0000000000"
    assert all(slot == "" for slot in fake_state.schedule[1:])
    # The model is empty and the sensor shows the empty state.
    assert coordinator.local_schedule == []
    assert coordinator.schedule_source == "last_write"
    assert hass.states.get(entities["schedule"]).state == "—"


async def test_missing_draft_slot_defaults_to_disabled(hass, entry, patched_client, fake_state):
    """A removed draft entity defaults its slot to disabled (save still works)."""
    await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)

    # Remove slot 1's portions entity; its time stays. Slot 1 should save as
    # disabled (0 portions) rather than failing the whole save.
    registry = er.async_get(hass)
    registry.async_remove(entities["slot_portions"])
    await hass.async_block_till_done(wait_background_tasks=True)

    await _set_draft(hass, entities, 1, "07:00:00", 5)  # time set, portions entity gone
    await _press_save(hass, entities)

    # Slot 1 written with its time but 0 portions / disabled.
    assert fake_state.schedule[0] == encode_plan_entry(7, 0, 0, False)
    # No firing slots, so the model is empty.
    coordinator = entry.runtime_data
    assert coordinator.local_schedule == []


async def test_save_schedule_validation(hass, entry, patched_client, fake_state):
    """The coordinator rejects malformed schedules (no write reaches the device)."""
    coordinator = await _make_available(hass, entry, patched_client)

    with pytest.raises(ValueError, match="expected 10 schedule entries"):
        await coordinator.async_save_schedule([(7, 0, 1, True)])
    with pytest.raises(ValueError, match="invalid time"):
        await coordinator.async_save_schedule([(24, 0, 1, True)] * 10)
    with pytest.raises(ValueError, match="portion must be"):
        await coordinator.async_save_schedule([(7, 0, 100, True)] * 10)
    assert fake_state.connect_count == 0


async def test_editor_and_model_survive_restart(hass, entry, patched_client, fake_state):
    """The drafts (restore-state) and the model (per-entry store) persist."""
    from pytest_homeassistant_custom_component.common import (
        async_mock_restore_state_shutdown_restart,
    )

    coordinator = await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)

    await _set_draft(hass, entities, 1, "07:15:00", 3)
    await _set_draft(hass, entities, 4, "20:30:00", 2)
    await _press_save(hass, entities)
    assert coordinator.local_schedule == [
        {"hour": 7, "minute": 15, "portion": 3, "enabled": True},
        {"hour": 20, "minute": 30, "portion": 2, "enabled": True},
    ]

    # Simulate a HA restart: dump + reload the restore-state store, then
    # re-set up the entry.
    await async_mock_restore_state_shutdown_restart(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    # The drafts are restored (slots 1 and 4)...
    assert hass.states.get(entities["slot_time"]).state == "07:15:00"
    assert hass.states.get(entities["slot_portions"]).state == "3"
    assert hass.states.get(entities["slot_4_time"]).state == "20:30:00"
    assert hass.states.get(entities["slot_4_portions"]).state == "2"
    # ...and the model is reloaded from the per-entry store.
    new_coordinator = entry.runtime_data
    assert new_coordinator.local_schedule == [
        {"hour": 7, "minute": 15, "portion": 3, "enabled": True},
        {"hour": 20, "minute": 30, "portion": 2, "enabled": True},
    ]
    assert new_coordinator.schedule_source == "last_write"
