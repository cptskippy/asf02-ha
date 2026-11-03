"""The ASF02 Pet Feeder integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from bleak import BleakError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from asf02 import ASF02Error

from .const import (
    CONF_TOKEN,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import ASF02Coordinator

_LOGGER = logging.getLogger(__name__)

type ASF02ConfigEntry = ConfigEntry[ASF02Coordinator]

SERVICE_FEED = "feed"
SERVICE_STOP = "stop"
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_CLEAR_SCHEDULE = "clear_schedule"

CONF_PORTIONS = "portions"
CONF_WAIT = "wait"
CONF_ENTRIES = "entries"

SCHEDULE_ENTRY_SCHEMA = vol.Schema(
    {
        vol.Required("time"): vol.All(cv.string, vol.Match(r"^\d{2}:\d{2}$")),
        vol.Required("portion"): vol.All(cv.positive_int, vol.Range(min=1, max=9)),
        vol.Optional("enabled", default=True): cv.boolean,
    }
)

ENTITY_IDS_SCHEMA = {CONF_ENTITY_ID: cv.entity_ids}

SERVICE_FEED_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PORTIONS): vol.All(cv.positive_int, vol.Range(min=1, max=99)),
        vol.Optional(CONF_WAIT, default=False): cv.boolean,
        **ENTITY_IDS_SCHEMA,
    }
)
SERVICE_STOP_SCHEMA = vol.Schema(ENTITY_IDS_SCHEMA)
SERVICE_SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENTRIES): vol.All(cv.ensure_list, [SCHEDULE_ENTRY_SCHEMA]),
        **ENTITY_IDS_SCHEMA,
    }
)
SERVICE_CLEAR_SCHEDULE_SCHEMA = vol.Schema(ENTITY_IDS_SCHEMA)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration (register services once)."""
    if not hass.services.has_service(DOMAIN, SERVICE_FEED):
        _register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ASF02ConfigEntry) -> bool:
    """Set up ASF02 from a config entry."""
    coordinator = ASF02Coordinator(
        hass,
        _LOGGER,
        address=entry.data[CONF_ADDRESS],
        token=entry.data[CONF_TOKEN],
        title=entry.title,
        config_entry=entry,
    )
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # The passive base starts the advertisement callback + the
    # learned-interval unavailable tracker; entry.async_on_unload stops them
    # on entry unload.
    entry.async_on_unload(coordinator.async_start())

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ASF02ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
def _coordinator_for(service: ServiceCall, entity_id: str) -> ASF02Coordinator:
    """Resolve an ASF02 entity_id to its entry's coordinator."""
    hass = service.hass
    registry = er.async_get(hass)
    reg = registry.async_get(entity_id)
    if reg is None or reg.platform != DOMAIN:
        raise HomeAssistantError(f"{entity_id} is not an ASF02 entity")
    entry = hass.config_entries.async_get_entry(reg.config_entry_id)
    if entry is None:
        raise HomeAssistantError(f"{entity_id} has no ASF02 config entry")
    return entry.runtime_data


def _parse_time(value: str) -> tuple[int, int]:
    hour, minute = (int(part) for part in value.split(":"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise HomeAssistantError(f"Invalid time {value!r}")
    return hour, minute


async def _async_feed(service: ServiceCall) -> None:
    portions = service.data[CONF_PORTIONS]
    wait = service.data[CONF_WAIT]
    for entity_id in service.data[CONF_ENTITY_ID]:
        coordinator = _coordinator_for(service, entity_id)
        try:
            await coordinator.async_feed(portions, wait=wait)
        except (BleakError, ASF02Error, TimeoutError) as err:
            raise HomeAssistantError(f"feed failed: {err}") from err


async def _async_stop(service: ServiceCall) -> None:
    for entity_id in service.data[CONF_ENTITY_ID]:
        coordinator = _coordinator_for(service, entity_id)
        try:
            await coordinator.async_stop()
        except (BleakError, ASF02Error, TimeoutError) as err:
            raise HomeAssistantError(f"stop failed: {err}") from err


async def _async_set_schedule(service: ServiceCall) -> None:
    raw_entries = service.data[CONF_ENTRIES]
    if len(raw_entries) > 10:
        raise HomeAssistantError("Max 10 schedule entries")
    entries = [
        (
            _parse_time(item["time"])[0],
            _parse_time(item["time"])[1],
            item["portion"],
            item["enabled"],
        )
        for item in raw_entries
    ]
    for entity_id in service.data[CONF_ENTITY_ID]:
        coordinator = _coordinator_for(service, entity_id)
        try:
            await coordinator.async_set_schedule(entries)
        except (BleakError, ASF02Error, TimeoutError) as err:
            raise HomeAssistantError(f"set_schedule failed: {err}") from err
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err


async def _async_clear_schedule(service: ServiceCall) -> None:
    for entity_id in service.data[CONF_ENTITY_ID]:
        coordinator = _coordinator_for(service, entity_id)
        try:
            await coordinator.async_clear_schedule()
        except (BleakError, ASF02Error, TimeoutError) as err:
            raise HomeAssistantError(f"clear_schedule failed: {err}") from err


def _register_services(hass: HomeAssistant) -> None:
    """Register the ASF02 services.

    Admin-only (``system: true`` in services.yaml — they drive the
    hardware); the registry itself has no admin variant, the frontend
    filters on the flag.
    """
    hass.services.async_register(DOMAIN, SERVICE_FEED, _async_feed, schema=SERVICE_FEED_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_STOP, _async_stop, schema=SERVICE_STOP_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCHEDULE, _async_set_schedule, schema=SERVICE_SET_SCHEDULE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_SCHEDULE, _async_clear_schedule, schema=SERVICE_CLEAR_SCHEDULE_SCHEMA
    )
