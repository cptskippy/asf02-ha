"""Services: feed, stop, set_schedule, clear_schedule."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.asf02 import const

from .conftest import fire_advertisement, poll_once
from .test_coordinator import _entities_by_key, _setup


async def _ready(hass, entry, patched_client):
    coordinator = await _setup(hass, entry, patched_client)
    fire_advertisement(hass)
    await poll_once(hass, coordinator)
    assert coordinator.data is not None
    return coordinator


async def test_feed_service(hass, entry, patched_client, fake_state):
    await _ready(hass, entry, patched_client)
    entity_id = _entities_by_key(hass, entry)["feed_count"]

    await hass.services.async_call(
        const.DOMAIN, "feed", {"entity_id": entity_id, "portions": 3}, blocking=True
    )
    assert fake_state.feed_count == 3
    # The dispense runs asynchronously after the near-instant ACK.
    assert fake_state.feeding is True

    await hass.services.async_call(const.DOMAIN, "stop", {"entity_id": entity_id}, blocking=True)
    assert fake_state.feeding is False
    assert fake_state.feed_count == 3  # stop before any portion counts


async def test_feed_wait_completes(hass, entry, patched_client, fake_state, monkeypatch):
    import custom_components.asf02.coordinator as coordinator_module

    monkeypatch.setattr(coordinator_module, "FEED_WAIT_POLL_INTERVAL", 0.05)
    fake_state.feed_duration = 0.1

    await _ready(hass, entry, patched_client)
    entity_id = _entities_by_key(hass, entry)["feed_count"]

    await hass.services.async_call(
        const.DOMAIN,
        "feed",
        {"entity_id": entity_id, "portions": 2, "wait": True},
        blocking=True,
    )
    assert fake_state.feed_count == 2
    assert fake_state.feeding is False  # waited for the dispense to finish


async def test_set_schedule_service(hass, entry, patched_client, fake_state):
    await _ready(hass, entry, patched_client)
    entity_id = _entities_by_key(hass, entry)["feed_count"]

    await hass.services.async_call(
        const.DOMAIN,
        "set_schedule",
        {
            "entity_id": entity_id,
            "entries": [
                {"time": "07:30", "portion": 2},
                {"time": "18:00", "portion": 3, "enabled": False},
            ],
        },
        blocking=True,
    )
    assert fake_state.schedule[0] == "00071e0201"
    assert fake_state.schedule[1] == "0012000300"
    assert fake_state.schedule[2:] == [""] * 8


async def test_clear_schedule_service(hass, entry, patched_client, fake_state):
    await _ready(hass, entry, patched_client)
    entity_id = _entities_by_key(hass, entry)["feed_count"]

    await hass.services.async_call(
        const.DOMAIN, "clear_schedule", {"entity_id": entity_id}, blocking=True
    )
    assert fake_state.schedule[0] == "0000000000"
    assert fake_state.schedule[1:] == [""] * 9


async def test_feed_rejects_bad_portions(hass, entry, patched_client):
    import probatio

    await _ready(hass, entry, patched_client)
    entity_id = _entities_by_key(hass, entry)["feed_count"]

    # This core raises the raw schema error (not wrapped in
    # ServiceValidationError) when service data fails validation.
    for portions in (0, 100):
        with pytest.raises(probatio.Invalid):
            await hass.services.async_call(
                const.DOMAIN,
                "feed",
                {"entity_id": entity_id, "portions": portions},
                blocking=True,
            )


async def test_service_rejects_non_asf02_entity(hass, entry, patched_client, fake_state):
    await _ready(hass, entry, patched_client)

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    other = MockConfigEntry(domain="demo", unique_id="other", data={})
    other.add_to_hass(hass)
    # A non-ASF02 entity id must be rejected by the coordinator resolver.
    with pytest.raises(HomeAssistantError, match="not an ASF02 entity"):
        await hass.services.async_call(
            const.DOMAIN,
            "feed",
            {"entity_id": "light.nonexistent_entity", "portions": 1},
            blocking=True,
        )
