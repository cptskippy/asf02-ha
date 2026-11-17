"""Options flow: poll interval (1 min – 1 h), read live by the coordinator."""

from __future__ import annotations

import pytest
from homeassistant.data_entry_flow import FlowResultType, InvalidData

import custom_components.asf02.config_flow as config_flow
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
            result["flow_id"], {const.OPTION_POLL_MINUTES: 0}
        )
    with pytest.raises(InvalidData, match="poll_minutes"):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {const.OPTION_POLL_MINUTES: 120}
        )


async def test_options_change_token_reloads_coordinator(hass, entry, patched_client):
    """A token change via options must reach the coordinator without a restart.

    Regression test for #19: the coordinator captures the token at setup, and
    the token lives in entry.data (the options are unchanged), so a plain
    options flow would never pick up the new token. Changing it now schedules
    an entry reload, and the reloaded coordinator holds the new token.
    """
    coordinator = await _setup(hass, entry, patched_client)
    original_token = entry.data[const.CONF_TOKEN]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    # Enter the token-change path (poll minutes left at the current value).
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            const.OPTION_POLL_MINUTES: const.DEFAULT_POLL_MINUTES,
            config_flow.CONF_CHANGE_TOKEN_FIELD: True,
        },
    )
    assert result["step_id"] == "token"
    # Generate a fresh token; bind() re-points the fake feeder at it.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {config_flow.CONF_GENERATE_FIELD: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    new_token = entry.data[const.CONF_TOKEN]
    assert new_token != original_token

    # Let the scheduled reload run: a fresh coordinator (new runtime_data)
    # holding the new token replaces the original without a full HA restart.
    await hass.async_block_till_done(wait_background_tasks=True)
    reloaded = entry.runtime_data
    assert reloaded is not coordinator
    assert reloaded.token == new_token
