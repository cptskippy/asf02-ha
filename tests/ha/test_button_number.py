"""Button + number entities: the Feed/Refresh buttons and the Portions number.

Covers the entity-surface rework: the derived binary sensors are gone,
Battery/Feed count/Device time are diagnostic, the Refresh button is a
config-category convenience for the refresh service, and the Feed button
dispenses whatever the Portions number is set to.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory

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


async def _make_available(hass, entry, patched_client):
    """Set up the entry and mark the feeder available (one ad, no poll)."""
    coordinator = await _setup(hass, entry, patched_client)
    fire_advertisement(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    return coordinator


async def test_portions_number_set_and_clamp(hass, entry, patched_client, fake_state):
    import pytest
    from homeassistant.exceptions import ServiceValidationError

    await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)

    portions = hass.states.get(entities["portions"])
    assert portions.state == "1"
    assert portions.attributes["min"] == 1
    assert portions.attributes["max"] == 99
    assert portions.attributes["step"] == 1

    await hass.services.async_call(
        "number", "set_value", {"entity_id": entities["portions"], "value": 3}, blocking=True
    )
    assert hass.states.get(entities["portions"]).state == "3"

    # Out-of-range values are rejected by the number service itself.
    with pytest.raises(ServiceValidationError, match="outside valid range"):
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entities["portions"], "value": 150},
            blocking=True,
        )
    assert hass.states.get(entities["portions"]).state == "3"


async def test_feed_button_dispenses_portions_value(hass, entry, patched_client, fake_state):
    coordinator = await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)

    await hass.services.async_call(
        "number", "set_value", {"entity_id": entities["portions"], "value": 3}, blocking=True
    )
    await hass.services.async_call(
        "button", "press", {"entity_id": entities["feed"]}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    # The feeder dispensed exactly the number's value, non-blocking.
    assert fake_state.feed_count == 3
    assert fake_state.feeding is True
    # Button state is now the last-press timestamp (was unavailable before).
    assert hass.states.get(entities["feed"]).state != "unavailable"
    # One transient GATT session for the feed (connect → unlock → feed → status).
    assert fake_state.connect_count == 1
    assert coordinator._feed_in_progress is False


async def test_feed_button_falls_back_to_default_portions(hass, entry, patched_client, fake_state):
    """A disabled/missing Portions number must not break the Feed button."""
    await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)

    registry = er.async_get(hass)
    registry.async_update_entity(entities["portions"], disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(entities["portions"]) is None

    await hass.services.async_call(
        "button", "press", {"entity_id": entities["feed"]}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert fake_state.feed_count == 1
    assert fake_state.feeding is True


async def test_refresh_button_polls_now(hass, entry, patched_client, fake_state):
    await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)
    assert fake_state.connect_count == 0  # no poll yet, just the ad

    fake_state.battery_raw = 480
    await hass.services.async_call(
        "button", "press", {"entity_id": entities["refresh"]}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert fake_state.connect_count == 1
    assert hass.states.get(entities["battery"]).state == "480"


async def test_portions_number_restores_after_restart(hass, entry, patched_client, fake_state):
    """The Portions value survives a restart (restore-state helper)."""
    from pytest_homeassistant_custom_component.common import (
        async_mock_restore_state_shutdown_restart,
    )

    await _make_available(hass, entry, patched_client)
    entities = _entities_by_key(hass, entry)

    await hass.services.async_call(
        "number", "set_value", {"entity_id": entities["portions"], "value": 5}, blocking=True
    )
    assert hass.states.get(entities["portions"]).state == "5"

    # Simulate a HA restart: dump + reload the restore-state store, then
    # re-set up the entry so the number entity is re-added.
    await async_mock_restore_state_shutdown_restart(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(entities["portions"]).state == "5"


async def test_entity_categories(hass, entry, patched_client):
    await _setup(hass, entry, patched_client)
    assert entry.state is ConfigEntryState.LOADED

    entities = _entities_by_key(hass, entry)
    registry = er.async_get(hass)

    def category(key: str) -> EntityCategory | None:
        return registry.async_get(entities[key]).entity_category

    # Diagnostic: battery, feed count, device time.
    for key in ("battery", "feed_count", "device_time"):
        assert category(key) is EntityCategory.DIAGNOSTIC, key
    # Control: refresh only.
    assert category("refresh") is EntityCategory.CONFIG
    # Default (main section): schedule sensor, feed button, portions number.
    for key in ("schedule", "feed", "portions"):
        assert category(key) is None, key
