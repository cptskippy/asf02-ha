"""Real HA fixtures; only radio I/O is simulated.

The asf02 library's protocol machinery (framing, lock semantics,
request/response matching) and the integration's coordinator, config flow,
options flow, and services all run unmodified; ``FakeFeeder`` stands in for
the radio. Advertisements are injected through the real bluetooth manager,
so the ad pipeline (matchers, history, availability tracking) is exercised
for real too.
"""

from __future__ import annotations

from typing import Any

import pytest
from asf02 import ASF02Client
from asf02.protocol import SERVICE_UUID
from bleak.backends.device import BLEDevice
from home_assistant_bluetooth import BluetoothServiceInfoBleak
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.asf02 as integration
import custom_components.asf02.config_flow as config_flow_module
import custom_components.asf02.coordinator as coordinator_module
import custom_components.asf02.managed_client as managed_client_module

from .fake_feeder import FakeFeeder, FakeFeederState

pytest_plugins = "pytest_homeassistant_custom_component"

ADDRESS = "AA:BB:CC:DD:EE:FF"
# Real device name: "ASF02-" + last 5 hex digits of the MAC, no colons
# (protocol.py decode_advertisement: model = name.split("-", 1)[0]).
LOCAL_NAME = f"ASF02-{ADDRESS.replace(':', '')[-5:]}"


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    """Let the real HA loader see this repository's integration."""


@pytest.fixture(autouse=True)
def suppress_foreign_bluetooth_discovery(monkeypatch: pytest.MonkeyPatch):
    """The feeder's 0xFFFF-manufacturer advertisement also matches OTHER core
    integrations' discovery matchers (e.g. kegtron), whose PyPI requirements
    are not installed in the test env. Real HA installs core-requirement
    packages at runtime; in tests, skip the auto-discovery flows for foreign
    domains. The asf02 config flows are always started explicitly by the
    tests, so asf02's own flows are left untouched.
    """
    from homeassistant.helpers import discovery_flow

    original = discovery_flow.async_create_flow

    def filtered(hass, domain, context, data, **kwargs):
        if domain != integration.DOMAIN:
            return
        return original(hass, domain, context, data, **kwargs)

    monkeypatch.setattr(discovery_flow, "async_create_flow", filtered)


class _FastASF02Client:
    """Stand-in for ASF02Client: the real class with the 1 s post-connect
    delay removed and a short response timeout (tests don't have a silent
    radio to time out on).

    ``new_token`` is a real staticmethod on the class, so the config
    flow's generate path (``ASF02Client.new_token()``) works unchanged.
    """

    new_token = staticmethod(ASF02Client.new_token)

    def __new__(cls, address: str, token: str, **kwargs: Any) -> ASF02Client:
        kwargs.setdefault("post_connect_delay", 0.0)
        kwargs.setdefault("response_timeout", 2.0)
        return ASF02Client(address, token, **kwargs)


@pytest.fixture
def fake_state() -> FakeFeederState:
    return FakeFeederState()


@pytest.fixture
def fake_feeder(fake_state: FakeFeederState) -> FakeFeeder:
    return FakeFeeder(ADDRESS, fake_state)


@pytest.fixture
def patched_client(fake_feeder: FakeFeeder, monkeypatch: pytest.MonkeyPatch) -> FakeFeeder:
    """Route all integration GATT sessions to ``fake_feeder``.

    Patches ASF02Client (fast, same real class) and the client_factory seam
    in the modules that use it (the coordinator binds it at import time; the
    config flow imports it locally inside _validate, so the source module is
    patched too). The first-poll stagger is zeroed so tests don't wait for
    it.
    """

    def factory(_address: str) -> FakeFeeder:
        return fake_feeder

    def make_factory(_hass: Any, _address: str, _title: str) -> Any:
        return factory

    monkeypatch.setattr(coordinator_module, "ASF02Client", _FastASF02Client)
    monkeypatch.setattr(config_flow_module, "ASF02Client", _FastASF02Client)
    monkeypatch.setattr(coordinator_module, "make_client_factory", make_factory)
    monkeypatch.setattr(managed_client_module, "make_client_factory", make_factory)
    monkeypatch.setattr(coordinator_module, "_first_poll_stagger", lambda _address: 0.0)
    return fake_feeder


@pytest.fixture
def entry(hass, fake_state: FakeFeederState) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=integration.DOMAIN,
        title=LOCAL_NAME,
        unique_id=ADDRESS,
        data={
            integration.CONF_ADDRESS: ADDRESS,
            integration.CONF_TOKEN: fake_state.token,
        },
    )
    entry.add_to_hass(hass)
    return entry


def make_service_info(
    hass,
    address: str = ADDRESS,
    name: str | None = None,
    connectable: bool = True,
) -> BluetoothServiceInfoBleak:
    """Build a feeder advertisement (real feeder ad shape, protocol.py)."""
    name = name or f"ASF02-{address.replace(':', '')[-5:]}"
    device = BLEDevice(address, name, {})
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={0xFFFF: b"\x02\x03" + bytes.fromhex(address.replace(":", ""))},
        service_data={},
        service_uuids=[SERVICE_UUID],
        source="local:00:11:22:33:44:55",
        device=device,
        advertisement=None,
        connectable=connectable,
        time=hass.loop.time(),
        tx_power=None,
    )


def fire_advertisement(
    hass,
    address: str = ADDRESS,
    name: str | None = None,
    connectable: bool = True,
) -> BluetoothServiceInfoBleak:
    """Inject an advertisement through the real bluetooth manager."""
    from homeassistant.components.bluetooth.api import _get_manager

    info = make_service_info(hass, address, name, connectable)
    _get_manager(hass).scanner_adv_received(info)
    return info


async def poll_once(hass, coordinator) -> None:
    """Drive one poll through the wall-clock timer (the sole poll trigger).

    Resets the last-poll bookkeeping so the interval check passes, then
    invokes the timer callback directly (no 60 s wait) and awaits the
    debounced GATT session. The feeder must have seen at least one ad first
    so the coordinator has a device to connect to.
    """
    coordinator._last_poll = None
    coordinator._first_poll_at = None
    coordinator._interval_poll_check()
    await hass.async_block_till_done(wait_background_tasks=True)
