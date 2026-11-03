"""Options flow: poll interval (5 min – 1 h), read live by the coordinator."""

from __future__ import annotations

import pytest
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.asf02 import const

from .conftest import fire_advertisement  # noqa: F401  (fixture availability)
from .test_coordinator import _setup


async def test_options_poll_interval(hass, entry, patched_client):
    coordinator = await _setup(hass, entry, patched_client)
    assert coordinator.poll_interval_seconds == const.DEFAULT_POLL_MINUTES * 60.0

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {const.OPTION_POLL_MINUTES: 30}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[const.OPTION_POLL_MINUTES] == 30

    # Read live — no reload needed.
    assert coordinator.poll_interval_seconds == 30 * 60.0


async def test_options_rejects_out_of_range(hass, entry, patched_client):
    await _setup(hass, entry, patched_client)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    # Out-of-range values are rejected by the flow machinery (schema
    # validation raises before the step ever sees the data).
    with pytest.raises(InvalidData, match="poll_minutes"):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {const.OPTION_POLL_MINUTES: 2}
        )
    with pytest.raises(InvalidData, match="poll_minutes"):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {const.OPTION_POLL_MINUTES: 120}
        )
