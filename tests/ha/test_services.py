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


async def test_set_schedule_service_updates_local_model(hass, entry, patched_client, fake_state):
    """The set_schedule service refreshes the local model so the slot
    entities reflect the new schedule immediately, not on the next poll (#21)."""
    await _ready(hass, entry, patched_client)
    coordinator = entry.runtime_data
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

    # The model now matches the write: only the firing slot is kept, and it
    # is attributed to this write (not a device read).
    assert coordinator.local_schedule == [{"hour": 7, "minute": 30, "portion": 2, "enabled": True}]
    assert coordinator.schedule_source == "last_write"


async def test_set_schedule_service_portion_range(hass, entry, patched_client, fake_state):
    """The set_schedule service accepts the editor's portion range (0-99),
    not the old 1-9 limit (#22)."""
    import probatio

    await _ready(hass, entry, patched_client)
    entity_id = _entities_by_key(hass, entry)["feed_count"]

    # 50 was rejected by the old max=9; it is accepted now.
    await hass.services.async_call(
        const.DOMAIN,
        "set_schedule",
        {"entity_id": entity_id, "entries": [{"time": "07:30", "portion": 50}]},
        blocking=True,
    )
    assert fake_state.schedule[0] == "00071e3201"

    # 0 is now a valid portion and implies a disabled slot (matching the
    # editor), even though enabled defaults to true.
    await hass.services.async_call(
        const.DOMAIN,
        "set_schedule",
        {"entity_id": entity_id, "entries": [{"time": "08:00", "portion": 0}]},
        blocking=True,
    )
    assert fake_state.schedule[0] == "0008000000"

    # 100 still exceeds the 0-99 range and is rejected by the schema.
    with pytest.raises(probatio.Invalid):
        await hass.services.async_call(
            const.DOMAIN,
            "set_schedule",
            {"entity_id": entity_id, "entries": [{"time": "07:30", "portion": 100}]},
            blocking=True,
        )


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


def test_managed_client_error_is_a_bleak_error():
    """ManagedClientError subclasses BleakError so the standard
    connection-error handlers catch it (#20)."""
    from bleak import BleakError

    from custom_components.asf02.managed_client import ManagedClientError

    assert issubclass(ManagedClientError, BleakError)
    assert isinstance(ManagedClientError("AA:BB:CC:DD:EE:FF"), BleakError)


async def test_feed_service_converts_managed_client_error(hass, entry, patched_client, monkeypatch):
    """A ManagedClientError from the radio surfaces as a friendly
    HomeAssistantError, not a raw traceback (#20)."""
    from custom_components.asf02.managed_client import ManagedClientError

    await _ready(hass, entry, patched_client)
    coordinator = entry.runtime_data
    entity_id = _entities_by_key(hass, entry)["feed_count"]

    async def raise_managed(_portions, wait=False):
        raise ManagedClientError(coordinator.address)

    monkeypatch.setattr(coordinator, "async_feed", raise_managed)

    with pytest.raises(HomeAssistantError, match="feed failed"):
        await hass.services.async_call(
            const.DOMAIN,
            "feed",
            {"entity_id": entity_id, "portions": 1},
            blocking=True,
        )
