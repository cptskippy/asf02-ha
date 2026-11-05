"""Coordinator: entry setup, ad-gated transient GATT sessions, gating."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from custom_components.asf02 import const
from custom_components.asf02.coordinator import ASF02Coordinator

from .conftest import ADDRESS, fire_advertisement, poll_once


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


async def test_entry_setup_creates_entities_unavailable(hass, entry, patched_client):
    coordinator = await _setup(hass, entry, patched_client)
    assert entry.state is ConfigEntryState.LOADED

    entities = _entities_by_key(hass, entry)
    assert sorted(entities) == [
        "battery",
        "battery_low",
        "device_time",
        "fed_recently",
        "feed_count",
        "feeding",
        "schedule",
    ]
    # No advertisement yet: entities exist but are unavailable.
    for entity_id in entities.values():
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "unavailable"
    assert not coordinator.available
    assert coordinator.data is None


async def test_poll_fills_entities(hass, entry, patched_client, fake_state):
    coordinator = await _setup(hass, entry, patched_client)

    # Ad marks availability and gives the coordinator a device; the timer
    # then drives the actual poll.
    fire_advertisement(hass)
    await poll_once(hass, coordinator)

    assert coordinator.available
    snap = coordinator.data
    assert snap is not None
    assert snap.firmware == "1.19.12"
    assert snap.serial == "SNFAKE0001"
    assert snap.battery_raw == 522
    assert snap.battery_low is False
    assert snap.feed_count == 0
    assert snap.schedule == ()
    assert snap.source == "local:00:11:22:33:44:55"
    # One transient session: connected once, re-locked after disconnect.
    assert fake_state.connect_count == 1
    assert fake_state.locked is True
    # Time sync used HA's configured UTC offset.
    assert fake_state.tz_offset_seconds == coordinator.tz_offset_seconds

    entities = _entities_by_key(hass, entry)
    battery = hass.states.get(entities["battery"])
    assert battery.state == "522"
    feed_count = hass.states.get(entities["feed_count"])
    assert feed_count.state == "0"
    feeding = hass.states.get(entities["feeding"])
    assert feeding.state == "off"


async def test_battery_low_binary(hass, entry, patched_client, fake_state):
    coordinator = await _setup(hass, entry, patched_client)
    fake_state.battery_raw = const.BATTERY_LOW_THRESHOLD - 10

    fire_advertisement(hass)
    await poll_once(hass, coordinator)

    entities = _entities_by_key(hass, entry)
    assert hass.states.get(entities["battery_low"]).state == "on"


async def test_timer_does_not_poll_within_interval(hass, entry, patched_client, fake_state):
    coordinator = await _setup(hass, entry, patched_client)

    fire_advertisement(hass)
    await poll_once(hass, coordinator)
    assert fake_state.connect_count == 1

    # A further timer tick within the interval is not due for a poll.
    coordinator._interval_poll_check()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake_state.connect_count == 1


async def test_feed_in_progress_blocks_polls(hass, entry, patched_client, fake_state):
    coordinator = await _setup(hass, entry, patched_client)

    fire_advertisement(hass)
    await poll_once(hass, coordinator)
    assert fake_state.connect_count == 1

    # A feed in progress blocks the timer-driven poll.
    coordinator._feed_in_progress = True
    coordinator._last_poll = None
    coordinator._interval_poll_check()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake_state.connect_count == 1


async def test_feed_event_fires_on_counter_increment(hass, entry, patched_client, fake_state):
    coordinator = await _setup(hass, entry, patched_client)

    events: list = []

    # @callback, not a bare lambda: a non-callback listener is dispatched
    # through the executor pool, so its delivery would race the assert
    # below (flaked on CI). Callbacks run inline in async_fire.
    @callback
    def _on_feed(event) -> None:
        events.append(event)

    hass.bus.async_listen(const.EVENT_FEED, _on_feed)

    fire_advertisement(hass)
    await poll_once(hass, coordinator)
    assert fake_state.connect_count == 1

    # Counter moves between two direct session reads → event.
    fake_state.feed_count = 5
    await coordinator._do_poll(coordinator._last_service_info)
    assert [e.data["new_count"] for e in events] == [5]
    assert events[0].data["address"] == ADDRESS

    fake_state.feed_count = 7
    await coordinator._do_poll(coordinator._last_service_info)
    assert [e.data["new_count"] for e in events] == [5, 7]


async def test_do_poll_wrong_token_raises_update_failed(hass, entry, patched_client):
    """_do_poll wraps a rejected token in an UpdateFailed with remediation."""
    import pytest
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coordinator = await _setup(hass, entry, patched_client)
    info = fire_advertisement(hass)
    coordinator.token = "wrongtoken99"
    with pytest.raises(UpdateFailed, match="re-configure"):
        await coordinator._do_poll(info)


async def test_failed_poll_keeps_previous_data(hass, entry, patched_client):
    """The poll path swallows failures instead of crashing.

    A bad token must not take the entry down: previous data is kept, the
    failure is logged, and the next poll retries.
    """
    coordinator = await _setup(hass, entry, patched_client)

    fire_advertisement(hass)
    await poll_once(hass, coordinator)
    before = coordinator.data
    assert before is not None
    assert coordinator.last_poll_successful is True

    coordinator.token = "wrongtoken99"
    await coordinator._async_poll()
    assert coordinator.data is before
    assert coordinator.last_poll_successful is False
