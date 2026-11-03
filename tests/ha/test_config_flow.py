"""Config flow: manual MAC, token entry, generate-and-repair, discovery."""

from __future__ import annotations

from asf02.protocol import unlock_code_for
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.asf02 import const
from custom_components.asf02.config_flow import CONF_GENERATE_FIELD, CONF_TOKEN_FIELD

from .conftest import ADDRESS, make_service_info


async def _start_user_flow(hass):
    return await hass.config_entries.flow.async_init(const.DOMAIN, context={"source": "user"})


async def test_user_flow_with_known_token(hass, patched_client, fake_state):
    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADDRESS: ADDRESS}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "token"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN_FIELD: fake_state.token}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][const.CONF_ADDRESS] == ADDRESS
    assert result["data"][const.CONF_TOKEN] == fake_state.token


async def test_user_flow_rejects_bad_mac(hass, patched_client):
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADDRESS: "not-a-mac"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"]["address"] == "invalid_mac"


async def test_wrong_token_reshows_token_form(hass, patched_client, fake_state):
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADDRESS: ADDRESS}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": "wrongtok99"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "token"
    assert result["errors"]["base"] == "invalid_token"
    # And the correct token still works after the failed attempt.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": fake_state.token}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_generate_token_repairs_device(hass, patched_client, fake_state):
    old_token = fake_state.token

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADDRESS: ADDRESS}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GENERATE_FIELD: True, CONF_TOKEN_FIELD: ""}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # The device was re-bound: new 10-char token, old one is dead.
    new_token = fake_state.token
    assert new_token != old_token
    assert len(new_token) == 10
    assert result["data"][const.CONF_TOKEN] == new_token
    # And the new token actually unlocks (the flow validated it).
    assert unlock_code_for(new_token)  # sanity: a valid code exists


async def test_duplicate_address_aborts(hass, patched_client, fake_state):
    existing = MockConfigEntry(
        domain=const.DOMAIN,
        title="Existing",
        unique_id=ADDRESS,
        data={const.CONF_ADDRESS: ADDRESS, const.CONF_TOKEN: fake_state.token},
    )
    existing.add_to_hass(hass)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {const.CONF_ADDRESS: ADDRESS}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_bluetooth_discovery_connectable(hass, patched_client, fake_state):
    info = make_service_info(hass)
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "bluetooth"}, data=info
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "token"


async def test_bluetooth_discovery_not_connectable_aborts(hass, patched_client, fake_state):
    info = make_service_info(hass, connectable=False)
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "bluetooth"}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "scanner_not_connectable"
