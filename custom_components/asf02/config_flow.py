"""Config flow for the ASF02 pet feeder.

Sources:
- ``bluetooth``: manifest matcher (local_name ``ASF02-*``) → device picker.
- ``user``: manual MAC entry.

Both converge on the token step: enter the existing 10-char pairing token,
or generate a fresh one and re-pair (bind) the device. Validation is a real
GATT session (unlock round-trip) using the same managed-client path the
coordinator uses, so a wrong token is caught before the entry exists.
"""

from __future__ import annotations

import logging
import re

import voluptuous as vol
from bleak import BleakError
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow, FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector

from asf02 import ASF02Client, ASF02Error, ASF02LockedError

from .const import (
    CONF_ADDRESS,
    CONF_TOKEN,
    CONF_TZ_OFFSET,
    DEFAULT_POLL_MINUTES,
    DOMAIN,
    MAX_POLL_MINUTES,
    MIN_POLL_MINUTES,
    OPTION_POLL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{10}$")
_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", re.IGNORECASE)

CONF_TOKEN_FIELD = "token"
CONF_GENERATE_FIELD = "generate_token"
CONF_CHANGE_TOKEN_FIELD = "change_token"


class ASF02ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the ASF02 feeder."""

    VERSION = 1

    _address: str
    _discovered_name: str | None

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------
    @callback
    def _async_abort_entries_match(self, addresses: set[str]) -> None:
        for entry in self._async_current_entries(include_ignore=True):
            if entry.data.get(CONF_ADDRESS) in addresses:
                raise AbortFlow("already_configured")

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Manual setup by MAC address."""
        errors: dict = {}
        if user_input is not None:
            raw = user_input[CONF_ADDRESS].strip().upper()
            if not _MAC_RE.match(raw):
                errors[CONF_ADDRESS] = "invalid_mac"
            else:
                await self.async_set_unique_id(raw)
                try:
                    self._async_abort_entries_match({raw})
                except AbortFlow:
                    raise
                self._address = raw
                self._discovered_name = None
                return await self.async_step_token()
        if not user_input:
            return self._async_show_user_form()
        return self._async_show_user_form(errors)

    async def async_step_bluetooth(self, data: BluetoothServiceInfoBleak) -> FlowResult:
        """Bluetooth discovery (manifest matcher on local_name ASF02-*)."""
        address = data.address.upper()
        await self.async_set_unique_id(address)
        self._async_abort_entries_match({address})
        if not data.connectable:
            # Ad-only scanner (e.g. advertisement-only ESPHome proxy).
            raise AbortFlow(
                "scanner_not_connectable",
                description_placeholders={
                    "name": data.name or address,
                },
            )
        self._address = address
        self._discovered_name = data.name
        # Per-device discovered-card title so the cards are distinguishable.
        # The subtitle already carries the integration name, so the title
        # uses the compact "ASF02-<last six MAC digits>" device-name format.
        mac_tail = address.replace(":", "")[-6:]
        self.context["title_placeholders"] = {"name": f"ASF02-{mac_tail}"}
        return await self.async_step_token()

    # ------------------------------------------------------------------
    # Token step
    # ------------------------------------------------------------------
    async def async_step_token(self, user_input: dict | None = None) -> FlowResult:
        """Enter the pairing token (or generate a new one and re-pair)."""
        errors: dict = {}
        if user_input is not None:
            generate = user_input[CONF_GENERATE_FIELD]
            token: str = ""
            if generate:
                token = ASF02Client.new_token()
            else:
                token = (user_input.get(CONF_TOKEN_FIELD) or "").strip()
                if not _TOKEN_RE.match(token):
                    errors[CONF_TOKEN_FIELD] = "invalid_token"
            if not errors:
                try:
                    await self._validate(
                        self._address,
                        token,
                        generate=generate,
                    )
                except ASF02LockedError:
                    errors["base"] = "invalid_token"
                except (BleakError, ASF02Error, TimeoutError) as err:
                    _LOGGER.debug("ASF02 validation session failed: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    return self._async_finish_entry(token)
        return self._async_show_token_form(errors)

    # ------------------------------------------------------------------
    # Validation session
    # ------------------------------------------------------------------
    async def _validate(self, address: str, token: str, *, generate: bool) -> None:
        """One GATT session: unlock, and bind first when re-pairing."""
        await _validate_token(self.hass, address, token, generate=generate)

    def _async_finish_entry(self, token: str) -> FlowResult:
        name = self._discovered_name or f"ASF02 {self._address[-5:]}"
        return self.async_create_entry(
            title=name,
            data={
                CONF_ADDRESS: self._address,
                CONF_TOKEN: token,
                CONF_TZ_OFFSET: _current_tz_offset(self.hass),
            },
        )

    # ------------------------------------------------------------------
    # Forms
    # ------------------------------------------------------------------
    def _async_show_user_form(self, errors: dict | None = None) -> FlowResult:
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS, default=""): cv.string,
                }
            ),
            errors=errors,
        )

    def _async_show_token_form(self, errors: dict) -> FlowResult:
        """Token form.

        The token field is optional in the schema so the "generate a new
        token" checkbox path can leave it blank; the flow re-shows the form
        with an error if it's blank without the checkbox.
        """
        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_GENERATE_FIELD, default=False): cv.boolean,
                    vol.Optional(CONF_TOKEN_FIELD): cv.string,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------
    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ASF02OptionsFlow()


class ASF02OptionsFlow(OptionsFlow):
    """Options: poll interval (1 min – 1 h) + pairing-token management.

    The current token is shown read-only for backup, and a "change token"
    path re-pairs the feeder in place (generate new / enter existing) — the
    same GATT validation the add flow uses, so HA stays intact.
    """

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            new_options = {OPTION_POLL_MINUTES: user_input[OPTION_POLL_MINUTES]}
            if user_input.get(CONF_CHANGE_TOKEN_FIELD):
                # Persist the poll interval, then go to the token step.
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    options={**self.config_entry.options, **new_options},
                )
                return await self.async_step_token()
            return self.async_create_entry(data=new_options)
        current = self.config_entry.options.get(OPTION_POLL_MINUTES, DEFAULT_POLL_MINUTES)
        token = self.config_entry.data.get(CONF_TOKEN, "")
        return self.async_show_form(
            step_id="init",
            description_placeholders={"token": token},
            data_schema=vol.Schema(
                {
                    vol.Required(OPTION_POLL_MINUTES, default=current): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_POLL_MINUTES,
                            max=MAX_POLL_MINUTES,
                            step=1,
                            unit_of_measurement="min",
                            mode=selector.NumberSelectorMode.SLIDER,
                        )
                    ),
                    vol.Optional(CONF_CHANGE_TOKEN_FIELD, default=False): cv.boolean,
                }
            ),
        )

    async def async_step_token(self, user_input: dict | None = None) -> FlowResult:
        """Change the pairing token (generate new / enter existing)."""
        errors: dict = {}
        if user_input is not None:
            generate = user_input[CONF_GENERATE_FIELD]
            token: str = ""
            if generate:
                token = ASF02Client.new_token()
            else:
                token = (user_input.get(CONF_TOKEN_FIELD) or "").strip()
                if not _TOKEN_RE.match(token):
                    errors[CONF_TOKEN_FIELD] = "invalid_token"
            if not errors:
                address = self.config_entry.data[CONF_ADDRESS]
                try:
                    await _validate_token(self.hass, address, token, generate=generate)
                except ASF02LockedError:
                    errors["base"] = "invalid_token"
                except (BleakError, ASF02Error, TimeoutError) as err:
                    _LOGGER.debug("ASF02 token-change session failed: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    # Update the entry in place (reload picks up the new token).
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        data={**self.config_entry.data, CONF_TOKEN: token},
                    )
                    # Preserve the existing options (this step only changed the token).
                    return self.async_create_entry(data=dict(self.config_entry.options))
        return self._async_show_token_form(errors)

    def _async_show_token_form(self, errors: dict) -> FlowResult:
        """Token form (generate a new token, or enter an existing one)."""
        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_GENERATE_FIELD, default=False): cv.boolean,
                    vol.Optional(CONF_TOKEN_FIELD): cv.string,
                }
            ),
            errors=errors,
        )


def _current_tz_offset(hass) -> int:
    """HA's configured UTC offset in seconds, DST included."""
    from homeassistant.util import dt as dt_util

    zone = dt_util.get_time_zone(hass.config.time_zone)
    offset = dt_util.now(zone).utcoffset()
    return int(offset.total_seconds()) if offset else 0


async def _validate_token(hass, address: str, token: str, *, generate: bool) -> None:
    """One GATT session: unlock, and bind first when re-pairing.

    ``bind`` works while locked (verified in live protocol captures), so the
    recovery path needs no prior knowledge of the old token. Shared by the
    config flow (add) and the options flow (change token).
    """
    from .managed_client import make_client_factory

    tz_offset = _current_tz_offset(hass)
    client = ASF02Client(
        address,
        token,
        client_factory=make_client_factory(hass, address, address),
    )
    async with client:
        if generate:
            await client.bind(token, tz_offset_seconds=tz_offset)
        await client.unlock()
