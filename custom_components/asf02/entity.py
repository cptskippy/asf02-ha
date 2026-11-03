"""Shared coordinator-backed entity base for ASF02."""

from __future__ import annotations

from typing import Any

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import (
    CONNECTION_BLUETOOTH,
    DeviceInfo,
)

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import ASF02Coordinator, FeederSnapshot

type ASF02ConfigEntry = ConfigEntry[ASF02Coordinator]


class ASF02CoordinatorEntity(PassiveBluetoothCoordinatorEntity[ASF02Coordinator]):
    """Base for all ASF02 entities.

    Availability is ad-driven (inherited): the passive base flips
    ``_available`` on advertisements and on the learned-interval
    unavailable callback, and ``async_update`` is a no-op — data flows
    only through the coordinator's poll sessions.
    """

    _attr_has_entity_name = True
    config_entry: ASF02ConfigEntry

    def __init__(self, coordinator: ASF02Coordinator, entry: ASF02ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self) -> DeviceInfo:
        """One device registry entry per feeder (stable on MAC)."""
        address = self.coordinator.address
        snapshot = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            name=self._entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
            sw_version=snapshot.firmware if snapshot else None,
            serial_number=snapshot.serial if snapshot else None,
        )

    @property
    def snapshot(self) -> FeederSnapshot | None:
        return self.coordinator.data

    @property
    def common_attributes(self) -> dict[str, Any]:
        """Attributes shared by all entities of a feeder."""
        snapshot = self.snapshot
        if snapshot is None:
            return {}
        return {
            "firmware": snapshot.firmware,
            "serial": snapshot.serial,
            "hw_version": snapshot.hw_version,
            "model": snapshot.model,
            "fetched_at": snapshot.fetched_at.isoformat(),
            "source": snapshot.source,
        }
