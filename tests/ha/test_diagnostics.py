"""Diagnostics: cached snapshot only, never a new GATT session."""

from __future__ import annotations

from custom_components.asf02 import const
from custom_components.asf02.diagnostics import async_get_config_entry_diagnostics

from .conftest import fire_advertisement
from .test_coordinator import _setup


async def test_diagnostics_before_any_poll(hass, entry, patched_client, fake_state):
    await _setup(hass, entry, patched_client)

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["loaded"] is True
    assert diag["integration_version"] == const.VERSION
    assert "snapshot" not in diag
    assert fake_state.connect_count == 0  # no new BLE query


async def test_diagnostics_with_snapshot(hass, entry, patched_client, fake_state):
    await _setup(hass, entry, patched_client)
    fake_state.feed_count = 42

    fire_advertisement(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    diag = await async_get_config_entry_diagnostics(hass, entry)
    snap = diag["snapshot"]
    assert snap["firmware"] == "1.19.12"
    assert snap["serial"] == "SNFAKE0001"
    assert snap["feed_count"] == 42
    assert snap["battery_raw"] == 522
    assert snap["source"] == "local:00:11:22:33:44:55"
