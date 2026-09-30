# Building a Home Assistant Integration — A Working Guide

Companion to `DESIGN.md` (which is *our* integration's design spec). This document
is the *general* guide: how an HA integration is built from the current official
developer documentation, with our feeder's decisions annotated where they
diverge from the default path.

Sources: developers.home-assistant.io (docs repo `home-assistant/developers.
home-assistant`, master branch, checked 2026-09-18) + HACS publish docs.
Doc URLs are in §13.

---

## 1. What an integration actually is

An **integration** (a.k.a. a *component*) is a Python package that registers
with HA's core under a unique **domain** name. Its pieces:

- **`manifest.json`** — metadata: domain, name, requirements (PyPI deps HA
  installs for you), discovery matchers (Bluetooth/Zeroconf/DHCP/SSDP/MQTT/
  USB/HomeKit), `integration_type`, `iot_class`, and flags like
  `config_flow: true`.
- **`__init__.py`** — the integration's setup entry point. For config-entry
  integrations it defines `async_setup_entry(hass, entry)` /
  `async_unload_entry(hass, entry)` / `async_remove_entry`.
- **`config_flow.py`** — the UI flow that lets a user *add* the integration
  (and later reconfigure/reauth). This is what makes an integration
  first-class in the UI.
- **Entity platforms** — `sensor.py`, `binary_sensor.py`, `number.py`,
  `button.py`, etc. Each defines `async_setup_entry` and adds entity objects
  to HA.
- **`services.yaml` + service handlers** — custom actions (`asf02.feed`,
  …).
- **`strings.json`** — all user-facing text, for translation.
- **`translations/en.json`** — generated from strings.json.

Two deployment forms, same code:

| Form | Lives in | Extra manifest key |
|------|----------|--------------------|
| Core | `homeassistant/components/<domain>/` | none |
| Custom | `<config_dir>/custom_components/<domain>/` | **`version` is required** |

We build a **custom** integration (`custom_components/asf02`), distributed
via HACS. Key implication: the `version` key in `manifest.json` is mandatory
for custom integrations (SemVer or CalVer), and HA shows it in the UI.

### Why an integration and not an add-on (our §2 decision, restated)

Add-ons run in their own container and cannot reach HA core's `bluetooth`
component — adapter management, shared scanners, ESPHome-proxy aggregation,
and connection-slot allocation all live in core. For a BLE GATT device that
needs to go through HA's managed Bluetooth stack, **integration is the only
viable form**. This is a load-bearing decision, not a stylistic one.

## 2. Development environment

The official setup is the HA dev container / dev environment (a clone of
`home-assistant/core` with a `.devcontainer`). Inside it:

```sh
python3 -m script.scaffold integration     # generate a new integration skeleton
python3 -m script.scaffold config_flow_discovery   # scaffold for discovery-only flows
python3 -m script.hassfest                 # validate manifest/strings/quality rules
```

For a *custom* integration you don't need the full core clone — the
`example-custom-config` repository
(`home-assistant/example-custom-config`) is the canonical reference for the
`custom_components/` layout. Our repo `asf02-ha` follows that layout (the
integration code at the repo root *is* the `custom_components/asf02`
directory, via HACS `content_in_root` or a `custom_components/` wrapper —
decided at publish time, §11).

Testing uses `pytest-homeassistant-custom-component`, which provides
`MockConfigEntry`, `MockBluetooth`, and friends. The scaffold generates
config-flow tests as a starting point.

## 3. The manifest

Minimal working custom manifest:

```json
{
  "domain": "asf02",
  "name": "ASF02 Pet Feeder",
  "version": "0.1.0",
  "codeowners": [],
  "documentation": "https://github.com/<owner>/asf02-ha",
  "issue_tracker": "https://github.com/<owner>/asf02-ha/issues",
  "integration_type": "device",
  "iot_class": "local_polling",
  "config_flow": true,
  "requirements": ["asf02>=0.2,<0.3"],
  "bluetooth": [
    { "local_name": "ASF02-*", "connectable": true }
  ]
}
```

Rules that matter:

- **`domain`** must match the directory name, be unique, and can never be
  changed later.
- **`integration_type`**: `device` = one physical device per config entry
  (correct for us — each feeder is its own entry). `hub` = one entry
  governing many devices. Our design spec currently says `hub`; given that
  each config entry maps to exactly one feeder, `device` is the better fit
  per the docs' own definitions. *Open point — revisit at implementation.*
- **`iot_class`**: `local_polling` (direct local comms, we poll),
  `local_push` (device notifies us), `cloud_polling`, `cloud_push`,
  `assumed_state`, `calculated`. Ours is honestly `local_polling`.
- **`requirements`**: pip strings. HA installs them into its deps dir at
  setup. Pin conservatively (`asf02>=0.2,<0.3`). During development you can
  override a specific version with
  `pip install asf02==X.Y.Z --target ~/.homeassistant/deps` and launch HA
  with `--skip-pip-packages asf02`, or point at a git ref:
  `asf02@git+https://github.com/<owner>/asf02.git@<ref>`.
- **`dependencies` / `after_dependencies`**: other integrations HA must
  have set up first. `dependencies` = hard requirement (our setup fails
  without it); `after_dependencies` = soft (wait for it *if configured*,
  and its requirements get installed, but load anyway if absent).
  For BLE-through-HA the docs recommend `dependencies: ["bluetooth_adapters"]`
  so remote adapters (ESPHome proxies) are connected before our entry loads.
- **`bluetooth` matcher**: when the `bluetooth` integration is active, an
  advertisement matching *all* fields of *any* listed matcher triggers our
  `async_step_bluetooth` discovery flow. Matchable fields: `local_name`
  (glob allowed, but no pattern in the first 3 characters), `service_uuid`,
  `service_data_uuid`, `manufacturer_id`, `manufacturer_data_start`.
  **`connectable: false`** opts in to discovery from advertisement-only
  controllers (e.g. some ESPHome proxies). Since we need outgoing GATT, we
  match `connectable: true` and the flow must reject non-connectable sources
  with a clear error.

## 4. Config entries: the lifecycle contract

A **config entry** is the persisted record of "the user added this
integration with these parameters", created by the config flow. Core
lifecycle (from `config_entries_index.md`):

1. `async_setup(hass, config)` — runs once per integration at startup.
   For a pure config-entry integration it just returns `True`.
2. `async_setup_entry(hass, entry)` — called for each entry (at startup and
   when a new one is created at runtime). **This is where real setup
   happens**: build the API client, create the coordinator, forward to
   platforms:

   ```python
   async def async_setup_entry(hass, entry):
       coordinator = ASF02Coordinator(hass, entry)
       await coordinator.async_config_entry_first_refresh()
       entry.runtime_data = coordinator
       await hass.config_entries.async_forward_entry_setups(
           entry, PLATFORMS
       )
       return True
   ```

   - **`entry.runtime_data`** is the sanctioned place to stash the
     coordinator/API object for platforms to reach (do not use
     `entry.data` — that's persisted storage).
   - `async_config_entry_first_refresh()` retries setup with backoff if the
     first fetch fails (raises `ConfigEntryNotReady`); use plain
     `async_refresh()` if you'd rather not retry setup.
3. `async_unload_entry(hass, entry)` — **optional but expected**: forward
   the unload to the platforms and return `True`.

   ```python
   async def async_unload_entry(hass, entry):
       return await hass.config_entries.async_unload_platforms(
           entry, PLATFORMS
       )
   ```
   If it returns `False`, the entry ends up in *failed unload* state and
   the user can't remove it — a real trap.
4. `async_remove_entry(hass, entry)` — optional; runs *after* the entry is
   deleted from storage. Only for extra cleanup.
5. `async_migrate_entry(hass, entry)` — required if you ever bump the entry
   `version`.

Rules that bite people:

- **Never mutate `entry.data` / `entry.options` directly** — use
  `hass.config_entries.async_update_entry`.
- Entity-level cleanup belongs in `Entity.async_will_remove_from_hass`,
  not in the unload function.
- Setup failures: raise `ConfigEntryNotReady` (retry later),
  `ConfigEntryAuthFailed` (kicks off a `reauth` flow), or
  `ConfigEntryError` (terminal, shown to user).

## 5. Config flow: the UI path to a config entry

`config_flow.py` defines a class extending `ConfigFlow` with the domain:

```python
class ASF02ConfigFlow(ConfigFlow, domain=DOMAIN):
    ...
```

Steps are methods named `async_step_<step_id>`. The standard step names:

| Step | When it runs |
|------|--------------|
| `user` | user clicks "Add integration" |
| `bluetooth` | a manifest `bluetooth` matcher fires |
| `zeroconf` / `dhcp` / `ssdp` / `usb` / `homekit` / `mqtt` / `hassio` | respective discovery |
| `reauth` | you raise `ConfigEntryAuthFailed` at runtime |
| `reconfigure` | user edits an entry's data |
| `import` | YAML → config entry migration (legacy) |

Each step returns one of `self.async_show_form(...)` (render a form,
`data_schema` built from `voluptuous` + `homeassistant.helpers.selector`),
`self.async_create_entry(title=..., data=...)`, `self.async_abort(reason)`,
or `self.async_external_step` / `async_show_progress` (for multi-minute
operations — BLE validation qualifies).

**Unique IDs** — the heart of duplicate protection:

```python
await self.async_set_unique_id(mac_address)
self._abort_if_unique_id_configured()
```

- A unique ID must be a **stable string** — MAC, serial, burned-in
  identifier. Explicitly *unacceptable*: IP address, user-changeable
  hostname, device name, URL.
- **Required** for any integration that supports a discovery method.
- Bonus: once set, users can "ignore" the discovery suggestion in the UI.
- If a config entry already exists with that ID,
  `_abort_if_unique_id_configured(updates={...})` both aborts *and*
  updates the stored data (e.g. new MAC/credentials discovered).

Flow form schema idiom (modern, selector-based):

```python
from homeassistant.helpers.selector import (
    NumberSelector, NumberSelectorConfig, NumberSelectorMode,
    TextSelector, TextSelectorConfig, TextSelectorType,
)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required("token"): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional("poll_minutes", default=15): NumberSelector(
            NumberSelectorConfig(min=5, max=60, mode=NumberSelectorMode.BOX)
        ),
    }
)
```

**Error surfacing**: a form can carry `errors={"base": "wrong_token"}` and
the key resolves through `strings.json`:

```json
{ "config": { "error": { "wrong_token": "Token was rejected by the device." } } }
```

**Options flow** (`options_flow.py`, class extends `OptionsFlow`) is for
tunables that don't affect identity (poll interval, thresholds). Its result
goes into `entry.options`, and `async_update_entry(..., options=...)`
persists it. Keep connection/secret data in `entry.data`, tunables in
`entry.options` — `SchemaConfigFlowHandler` (a helper for simple
schema-driven flows) enforces this by putting everything in options, which
is *why* it's unsuitable for us.

## 6. Fetching data: the coordinator pattern

HA assumes entities **poll** unless you say otherwise
(`Entity.should_poll -> False`). For anything beyond one trivial entity,
the right tool is **`DataUpdateCoordinator`**
(`homeassistant.helpers.update_coordinator`):

```python
class ASF02Coordinator(DataUpdateCoordinator[FeederSnapshot]):
    def __init__(self, hass, entry):
        super().__init__(
            hass,
            _LOGGER,
            name="asf02",
            config_entry=entry,
            update_interval=timedelta(minutes=15),
            always_update=False,   # snapshot dataclass is comparable
        )
        ...

    async def _async_setup(self):
        """One-time init — run during first refresh."""

    async def _async_update_data(self) -> FeederSnapshot:
        # on success: return data
        # auth problem: raise ConfigEntryAuthFailed
        # transient failure: raise UpdateFailed("...")  (retry_after=... supported)
```

Behavior worth internalizing:

- **Polls only while entities are subscribed.** A disabled sensor stops the
  polling for it; if all entities go away the coordinator idles.
- **`always_update=False`** skips listener callbacks when
  `data == old_data` — important for BLE where a poll costs ~10 s of radio
  time; fewer redundant state writes = less DB churn.
- Entities subclass **`CoordinatorEntity`** and implement
  `_handle_coordinator_update()` (sync callback) instead of their own
  `async_update`. They get `self.available` for free from coordinator state.
- After a service call that changes device state, call
  `await coordinator.async_request_refresh()` (or
  `coordinator.async_set_updated_data(new_snapshot)` if you already fetched).

**Push variant**: construct the coordinator without `update_method` /
`update_interval` and feed it via `async_set_updated_data`. Our device is
GATT-poll-only (advertisements carry no payload), so we use the plain
polling coordinator — this matches the docs' own decision table in
*Fetching Bluetooth data* ("device only communicates with an active
Bluetooth connection → DataUpdateCoordinator").

### The Bluetooth-specific coordinators (for orientation)

`docs/core/bluetooth/bluetooth_fetching_data.md` describes four
advertisements-driven coordinators:

| Coordinator | Use when |
|-------------|----------|
| `PassiveBluetoothProcessorCoordinator` | all data arrives in advertisements; entities are sensors/binary-sensors/events |
| `ActiveBluetoothProcessorCoordinator` | ad-driven, with on-demand GATT polls (`needs_poll_method` + `poll_method`) |
| `PassiveBluetoothDataUpdateCoordinator` | ad-driven, non-sensor primary function |
| `ActiveBluetoothDataUpdateCoordinator` | ad-driven + active polls, custom processing |

They take care of scanning subscription, dedup, and even entity creation on
demand. The processor variants require the library to emit a
`PassiveBluetoothDataUpdate` (entities described + keyed by
`PassiveBluetoothEntityKey`). **Decision rule**: if your device pushes
meaningful data in *advertisements*, use these; if advertisements are only
a liveness/presence signal and the data requires a GATT session, use a
plain `DataUpdateCoordinator` and let ad events drive *availability* only.
That's exactly our case (static `ASF02-<last6>` advertisements, all data
behind unlock).

## 7. Bluetooth: the rules that matter for GATT integrations

From the dev docs (Bluetooth best-practices + `docs/core/bluetooth/api.md`),
the non-negotiables:

1. **Never instantiate `BleakClient` yourself.** Always obtain a
   connectable `BLEDevice` from HA:

   ```python
   from homeassistant.components import bluetooth

   ble_device = bluetooth.async_ble_device_from_address(
       hass, address, connectable=True
   )
   ```

   The returned object is `habluetooth`'s `HaBleakClientWrapper` —
   `BleakClient`-compatible, and it reserves a **connection slot** from HA's
   slot manager and routes through the best adapter (local dongle or
   `active: true` ESPHome proxy). Bypassing this is what breaks proxies,
   slot accounting, and multi-adapter routing.
   (*There is no `bluetooth.async_connect` in current core — older
   third-party guides showing a `BluetoothClient` helper are stale.*)

2. **Dependency on `bluetooth_adapters`** so remote adapters are up before
   our entry loads.

3. **≥10 s connect timeout** (BlueZ service resolution is slow on first
   touch).

4. **Availability tracking is free**:
   `bluetooth.async_track_unavailable(hass, callback, address,
   connectable=True)` fires when the device's advertisements stop being
   seen (learned interval, no polling cost). This is the idiomatic
   "device left range" signal — pair it with the coordinator marking
   entities unavailable.
   (Caveat from the docs: on macOS/CoreBluetooth, cached advertisements may
   mean the unavailable callback never fires. Not our target platform, but
   noted.)

5. **Transient sessions are fine and common** for devices that can't hold a
   persistent connection: connect → do the work → disconnect, per operation.
   The slot is released between sessions. For single-connection devices you
   must serialize operations yourself (an `asyncio.Lock`) — HA does not
   serialize *your* calls for you, it only manages the slot.

6. **`async_process_advertisements(hass, address)`** — inside a config
   flow, forces a fresh advertisement pass to obtain a current
   `BluetoothServiceInfoBleak` before validating reachability.

7. Check `service_info.connectable` (or the `BLEDevice.connectable`
   property) before promising a GATT session — advertisement-only proxies
   can surface a device the user can see but not connect to.

## 8. Entity platforms

Each platform file (`sensor.py`, `binary_sensor.py`, …) defines:

```python
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

async def async_setup_entry(
    hass, entry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator: ASF02Coordinator = entry.runtime_data
    async_add_entities(
        ASF02BatterySensor(coordinator),
        ASF02FeedCountSensor(coordinator),
        ASF02FeedingSensor(coordinator),
        # ...
    )
```

Entity essentials (`docs/core/entity.md` + platform docs):

- Extend the platform's base class (`SensorEntity`, `BinarySensorEntity`,
  `NumberEntity`, `ButtonEntity`, …) and, for coordinator-backed entities,
  `CoordinatorEntity`.
- **`unique_id`** is required per entity (stable, namespaced to the domain,
  e.g. `f"{address}-battery"`). Without it entities get re-created on every
  reload and users can't track them.
- **`device_info`** attaches the entity to a device in the registry:

  ```python
  @property
  def device_info(self) -> DeviceInfo:
      return {
          "identifiers": {(DOMAIN, self.address)},
          "connections": {(CONNECTION_BLUETOOTH, self.address)},
          "name": self.title,
          "manufacturer": "...",
          "model": "...",
          "sw_version": self.firmware,
          "serial_number": self.serial,
      }
  ```

  Entities sharing an identifier land on the same device card.
- **Device classes and state classes** are what make entities behave in
  dashboards and automations: `SensorDeviceClass.BATTERY` (0–100% assumed),
  `SensorDeviceClass.TIMESTAMP`, `state_class=TOTAL_INCREASING` (enables
  statistics), `BinarySensorDeviceClass.BATTERY`, etc. Pick the class that
  *honestly* describes the value; a wrong device class misleads the UI
  (e.g. a raw battery unit with `BATTERY` class will render as a bogus
  percentage). If no class fits, leave it `None` and expose via
  attributes.
- **`_attr_*` properties** set attributes declaratively
  (`self._attr_native_value`, `self._attr_extra_state_attributes`, …).
- **`async_added_to_hass`** — subscribe to push sources here; mirror the
  unsubscribe in `async_will_remove_from_hass`.
- **`should_poll`** — `True` (default) for coordinator/poll entities;
  `False` if the entity updates itself (then *you* must call
  `async_write_ha_state()` / `schedule_update_ha_state()`).

### Service entities vs. services

Two ways to expose actions:

- **Entity-based** (`button.ButtonEntity` with `async_press`,
  `number.NumberEntity` with `async_set_value`, `select` with options,
  `switch` with on/off): appears on the device card, usable in dashboards
  and automations by entity id. Best for actions bound to a device.
- **Integration services** (`services.yaml` + `hass.services.async_register`
  in `async_setup_entry`): `asf02.feed(entity_id=..., portions=...)`,
  `asf02.set_schedule(entity_id=..., entries=[...])`. Best for actions
  with richer parameters than a button/number can carry, and for
  automation-facing APIs.

Use both where they fit: a "Feed now" *button* for the card, and the
`feed`/`set_schedule`/`stop` *services* for automations with parameters.
Service schemas are voluptuous; validate `entity_id` lists with
`cv.entity_ids`.

## 9. Events (optional)

Integrations can fire bus events (`hass.bus.async_fire("asf02.feed",
{...})`) for things that are inherently event-shaped (a feed happened).
The modern docs steer most use-cases toward entities (binary sensor with a
time window, `TOTAL_INCREASING` counter for statistics) rather than custom
events; fire an event only when the "something happened at time T" shape
can't be expressed as entity state.

## 10. Testing

- `pytest-homeassistant-custom-component` — the fixture package:
  `pytest.mark.asyncio`, `MockConfigEntry`, `MockConfigEntryData`,
  `MockBluetooth`, `MockServiceInfo`, `MockBluetoothServiceInfoBleak`,
  `patch_time`.
- **Config flow tests are required for core** (full coverage of
  `config_flow.py`); for a custom integration they're still the fastest
  way to catch the classic bugs (duplicate discovery, unique-id handling,
  error paths).
- Mock the *library client*, not bleak: with an injected client factory
  (our §3.1 seam) the test patches the factory and gets a fake object with
  canned responses. This keeps tests hermetic — no radio, no timing.
- `script.hassfest` (in the core dev env) validates manifest, strings, and
  quality-scale rules; run it in CI.
- Live acceptance checklist belongs in the integration repo's README
  (ours is in `DESIGN.md` §12.3).

## 11. Shipping: HACS

From the HACS publish docs (hacs.xyz/docs/publish/integration):

- The repo needs a `manifest.json` in the integration directory with at
  least: `domain`, `name`, `version`, `documentation`, `issue_tracker`,
  `codeowners`.
- Brand assets: a `hacs.json` at repo root is optional; HACS reads
  `manifest.json` for the essentials. If the integration is not at the
  repo root, set `"filename"` or use `content_in_root: true`.
- A GitHub **release** (tag) marks the version users see; the manifest
  `version` should match the tag.
- The repo must be discoverable (public, or HACS "custom repo" addition
  pointing at it).
- `requirements` in the manifest get installed by HA automatically — for
  us, `asf02` from PyPI, which is why the library is published there first.

Development install without HACS: copy/symlink the directory into
`<config>/custom_components/asf02`, restart HA, add via UI.

## 12. Mapping the guide onto our feeder (decision summary)

| Concern | General best practice (this guide) | Our decision | Why |
|---------|-----------------------------------|--------------|-----|
| Form | Integration if you need core services (BLE, cloud, etc.) | Integration | needs core `bluetooth` stack + proxies |
| integration_type | `device` per physical device; `hub` for gateways | `device` (spec says `hub` — revisit) | 1 entry = 1 feeder |
| Discovery | manifest matcher + `async_step_bluetooth` + unique_id | `local_name: ASF02-*`, unique_id = MAC | continuous advertising, stable MAC |
| Auth | config flow step; `reauth` if creds expire | token step + generate-token re-pair path | token never expires; re-pair covers loss |
| Data | `DataUpdateCoordinator`, `always_update=False`, poll-only-while-subscribed | 15-min transient GATT sessions, jittered per MAC | single-connection device, ~10 s/session |
| Bluetooth | never own `BleakClient`; use `async_ble_device_from_address` | client_factory injection in `asf02` (v0.2) | library stays HA-free; integration injects the HA wrapper |
| Availability | ad-driven `async_track_unavailable` | yes | zero-GATT-cost presence |
| Entities | coordinator-backed, unique_id'd, honest device classes | battery (raw + low sensor), feed count (`TOTAL_INCREASING`), schedule (text+attrs), device time, feeding (`s==1`), fed-recently window | per DESIGN.md §7 |
| Actions | button/number/select entities + parameterized services | feed/stop/set_schedule/clear services + feed button | rich params don't fit buttons |
| Unload | forward platform unload, return True | standard | "failed unload" is a trap |
| Distribution | HACS, manifest version == tag, brand assets | HACS custom repo; `asf02` pinned in requirements | library-first PyPI strategy |

## 13. Source links

- Creating your first integration — `developers.home-assistant.io/docs/creating_component_index/`
- Integration manifest — `developers.home-assistant.io/docs/creating_integration_manifest/`
- Config entries — `developers.home-assistant.io/docs/config_entries_index/`
- Config flow — `developers.home-assistant.io/docs/core/integration/config_flow/`
- Options flow — `developers.home-assistant.io/docs/core/integration/options_flow/`
- Fetching data (coordinators) — `developers.home-assistant.io/docs/integration_fetching_data/`
- Fetching Bluetooth data — `developers.home-assistant.io/docs/core/bluetooth/bluetooth_fetching_data/`
- Bluetooth API — `developers.home-assistant.io/docs/core/bluetooth/api/`
- Entities — `developers.home-assistant.io/docs/core/entity/` (+ per-platform: `sensor/`, `binary-sensor/`, `button/`, `number/`, `select/`)
- Devices & services architecture — `developers.home-assistant.io/docs/architecture/devices-and-services/`
- Example custom integrations — `github.com/home-assistant/example-custom-config`
- HACS: publishing an integration — `hacs.xyz/docs/publish/integration/`
- GitHub source of the docs — `github.com/home-assistant/developers.home-assistant` (`docs/` tree)

## 14. Gotchas checklist (things that will actually bite)

1. Custom integration without `version` in manifest → not listed in UI.
2. `async_unload_entry` returning `False` (or raising) → entry stuck in
   *failed unload*; user can't remove it.
3. Mutating `entry.data` in place → silently lost or corrupted on save;
   use `async_update_entry`.
4. Unique ID from an unstable source (IP, name) → duplicate entries after
   network changes; discovery spam.
5. `BleakClient(address)` directly → bypasses slot manager; breaks
   ESPHome proxies and multi-adapter setups.
6. Short connect timeout → intermittent "device not found" on BlueZ cold
   starts.
7. Polling a single-connection BLE device from two code paths (coordinator
   + service handler) at once → connection flaps; serialize with a lock.
8. Wrong `SensorDeviceClass` on a raw unit value → UI renders nonsense.
9. Forgetting `always_update=False` on comparable data → constant state
   writes, DB growth.
10. Publishing an HACS release whose tag doesn't match manifest `version`
    → confusing update behavior.
