"""Managed GATT client construction for the ASF02 feeder.

The ``asf02`` library takes a ``client_factory`` and drives the returned
object with the bleak-client surface (``connect``, ``start_notify``,
``write_gatt_char``, ``disconnect``). In Home Assistant the GATT client must
not be a plain ``BleakClient`` — it has to be HA's managed wrapper so the
connection reserves a connection slot and routes through the right local
adapter or ESPHome proxy.

``bluetooth.async_ble_device_from_address`` returns the advertisement-history
``BLEDevice`` (no client surface). The managed client is built from it with
``bleak_retry_connector.establish_connection``:

- ``HaBleakClientWrapper`` (habluetooth) allocates/releases the connection
  slot and picks the scanner/connector backend at ``connect()`` time.
- ``establish_connection`` supplies retry/backoff and marks the client with
  ``_is_retry_client=True`` (the wrapper logs a warning otherwise).

The library calls ``client_factory(address)`` synchronously and then
``await client.connect()``, so the factory returns :class:`_ManagedClient`,
a thin proxy that resolves to a connected ``HaBleakClientWrapper`` on its
first ``connect()`` (``establish_connection`` returns an already-connected
client; subsequent wrapper ``connect()`` calls no-op via ``is_connected``).
"""

from __future__ import annotations

import logging
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection
from habluetooth import HaBleakClientWrapper

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# A _ManagedClient._client holds this until connect(); __getattr__ only fires
# for attributes that are truly absent, so the sentinel is unreachable from
# normal attribute access and no recursion is possible.
_UNCONNECTED = object()


class ManagedClientError(Exception):
    """No connectable adapter/proxy can reach the device right now."""

    def __init__(self, address: str) -> None:
        super().__init__(f"no connectable Bluetooth path to {address}")
        self.address = address


class _ManagedClient:
    """BleakClient-surface proxy that connects through HA's bluetooth stack.

    Only the attributes the ``asf02`` client touches are forwarded
    (``start_notify``, ``write_gatt_char``, ``disconnect``, ``is_connected``,
    ``address``). Forwarding is lazy so a misused pre-connect access fails
    loudly instead of against a dead object.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
        disconnected_callback: Any | None = None,
    ) -> None:
        self._hass = hass
        self._address = address
        self._name = name
        self._disconnected_callback = disconnected_callback
        self._client: HaBleakClientWrapper | Any = _UNCONNECTED

    @property
    def address(self) -> str:
        return self._address

    @property
    def is_connected(self) -> bool:
        return self._client is not _UNCONNECTED and self._client.is_connected

    def __getattr__(self, name: str) -> Any:
        client = self.__dict__.get("_client", _UNCONNECTED)
        if client is _UNCONNECTED:
            raise AttributeError(
                f"_ManagedClient: {name!r} accessed before connect()"
            )
        return getattr(client, name)

    async def connect(self) -> None:
        if self._client is not _UNCONNECTED:
            return
        ble_device: BLEDevice | None = bluetooth.async_ble_device_from_address(
            self._hass, self._address, connectable=True
        )
        if ble_device is None:
            raise ManagedClientError(self._address)
        _LOGGER.debug(
            "%s: establishing managed GATT connection to %s",
            self._name,
            self._address,
        )
        self._client = await establish_connection(
            HaBleakClientWrapper,
            ble_device,
            self._address,
            disconnected_callback=self._disconnected_callback,
        )

    async def disconnect(self) -> None:
        client, self._client = self._client, _UNCONNECTED
        if client is _UNCONNECTED:
            return
        await client.disconnect()


def make_client_factory(
    hass: HomeAssistant, address: str, name: str
) -> Any:
    """Build the ``client_factory`` callable for ``ASF02Client``.

    Exposed as a function (rather than a closure literal) so tests can
    monkeypatch it in one place.
    """

    def _disconnected(client: BleakClient) -> None:
        _LOGGER.debug("%s: GATT link dropped: %s", name, address)

    def factory(addr: str) -> _ManagedClient:
        return _ManagedClient(
            hass, addr, name, disconnected_callback=_disconnected
        )

    return factory
