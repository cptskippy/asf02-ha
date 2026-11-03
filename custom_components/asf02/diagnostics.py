"""Diagnostics for the ASF02 integration.

Reports the last cached snapshot only — never opens a GATT session, so a
diagnostics download is always safe even while the feeder is feeding.
The config entry's own data (address, token) is included in the download
by core; that is intentional — the token is what support needs to help
re-pair a feeder.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, VERSION
from .coordinator import ASF02Coordinator, FeederSnapshot


def _snapshot_dict(snapshot: FeederSnapshot) -> dict[str, Any]:
    return {
        "firmware": snapshot.firmware,
        "hw_version": snapshot.hw_version,
        "model": snapshot.model,
        "serial": snapshot.serial,
        "state": snapshot.state,
        "feed_count": snapshot.feed_count,
        "feed_count_mirror": snapshot.feed_count_mirror,
        "battery_raw": snapshot.battery_raw,
        "battery_low": snapshot.battery_low,
        "uptime_raw": snapshot.uptime_raw,
        "schedule": [
            {"hour": e.hour, "minute": e.minute, "portion": e.portion, "enabled": e.enabled}
            for e in snapshot.schedule
        ],
        "device_time": snapshot.device_time.isoformat() if snapshot.device_time else None,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "source": snapshot.source,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the cached coordinator state (no new BLE query)."""
    coordinator = entry.runtime_data
    if not isinstance(coordinator, ASF02Coordinator):
        return {
            "domain": DOMAIN,
            "integration_version": VERSION,
            "loaded": False,
            "note": "entry not set up yet (coordinator absent)",
        }
    result: dict[str, Any] = {
        "domain": DOMAIN,
        "integration_version": VERSION,
        "loaded": True,
        "available": coordinator.available,
    }
    snapshot = coordinator.data
    if snapshot is not None:
        result["snapshot"] = _snapshot_dict(snapshot)
    return result
