# ASF02 Feeder — Home Assistant Integration Design

Status: **design spec** (not yet implemented)
Date: 2026-09-11; revised 2026-09-18 (aligned with published `asf02` 0.1.0 API and
`INTEGRATION_GUIDE.md`)
Library: [`asf02`](https://pypi.org/project/asf02/) v0.1.x (sibling repo, on PyPI)
HA target: current core (dev-branch APIs verified 2026-09-11; see §13 Sources)

---

## 1. Goal

Expose Lulumiao pet slow feeders (OEM model ASF02, sold as CWH01L-A1) in Home
Assistant as a **custom
integration** (`custom_components/asf02`) that:

1. **Discovers** feeders over BLE — through HA's shared Bluetooth stack, so
   local USB dongles **and** ESPHome `bluetooth_proxy` remotes are both
   reachable with zero integration-side networking code.
2. **Adopts** feeders via a token-based config flow (vendor-app-style
   pairing; no cloud, no passcode).
3. **Exposes** on-demand feed/stop, the 10-slot schedule (read/edit), battery,
   total feed counter, and device-time state.

Non-goals: persistent GATT connections (feeders run autonomously; we do short
transient sessions), OTA firmware update (service exists on-device; not
exposed in v1), the hello-fairy LED family (different product, different
protocol — no relationship).

## 2. Why an integration, not an add-on

An add-on runs in a separate container and cannot reach HA core's Bluetooth
component (adapter management, scanner multiplexing, proxy aggregation,
connection-slot management). All of that lives in core, behind
`homeassistant.components.bluetooth`. The integration is the only form that
gets ESPHome-proxy reach and multi-adapter routing for free.

## 3. Architecture

```
┌──────────────────────────── HA core ────────────────────────────┐
│  bluetooth integration  (scanners, adapters, proxies, slots)    │
│    ▲  advertisements in        ▲  BLEDevice (client wrapper)    │
│  ┌──────────────────────────── custom_components/asf02 ────────┐│
│  │  config_flow (discovery+token)  coordinator (poll session)  ││
│  │  sensor/binary_sensor platforms   services (feed/schedule)  ││
│  └───────────────────────────────┬─────────────────────────────┘│
└──────────────────────────────────┼──────────────────────────────┘
                                   │ asf02 library (pure Python, bleak)
                                   ▼
                          ┌──────────────────┐
                          │  ASF02 feeder    │  GATT service 55535343-…
                          │  (JSON-RPC over  │  write 49535343-…0a0b0c0d1912
                          │  GATT, 11 meths) │  notify 49535343-…0a0b0c0d2b12
                          └──────────────────┘
```

Three layers, strict dependency direction: **platforms → coordinator/flow →
`asf02` library → bleak**. The library never imports Home Assistant.

### 3.1 The one seam needed: client injection

The `asf02` library currently constructs its own `BleakClient(address)`.
In HA we must **not** do that — the connection has to go through HA's
managed wrapper (which is what reserves a connection slot and routes through
the right adapter/proxy). Fix (small library patch, backwards compatible):

```python
# asf02/client.py
BleakClientFactory = Callable[..., "BleakClient"]  # like bleak's own factory

class ASF02Client:
    def __init__(self, address, token, *, client_factory: BleakClientFactory = BleakClient, ...):
        ...
    async def connect(self):
        client = self._client_factory(self.address)
        ...
```

The integration supplies:

```python
ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
async with ASF02Client(address, token, client_factory=lambda addr: ble_device) as feeder:
    ...
```

`async_ble_device_from_address` returns a `HaBleakClientWrapper` (HA core
`bluetooth/api.py`, verified 2026-09-11) — a `BleakClient`-compatible
object that participates in HA's slot manager. No HA imports leak into the
library; a non-HA caller gets the exact same code path as today.

> **Status (verified 2026-09-18 against published `asf02` 0.1.0):** the
> shipped constructor is `ASF02Client(address, token, *, response_timeout,
> post_connect_delay)` and `connect()` builds `BleakClient(self.address,
> timeout=20)` internally — so this seam does **not exist yet** and is a
> prerequisite for any integration work (plan item 1).

> Note: older guides (e.g. ha-petkit-ble's `BluetoothClient` helper)
> predate the current API. The verified current path is
> `async_ble_device_from_address` + injecting the returned device as the
> client. `bluetooth.async_connect` does not exist in current core.

### 3.2 Hardware findings, 2026-09-18 (dedicated probes, fw 1.19.12)

Two dedicated probe sessions on the spare unit (adapter power, then
freshly-charged NiMH AAs; script `tmp/ble_drain.py`, data in
`tmp/drain_20260918_164735.csv`). These supersede earlier assumptions
where they conflict:

- **Mid-feed link drops: not reproducible.** ~60 portions total across
  both sessions (feed(1)/2/3/50) with 91 mid-feed status polls:
  **zero link drops, zero poll failures**, on both power sources. ACKs
  arrive in 86–100 ms and the link stays up the entire dispense. The
  "device drops the GATT connection during a feed" observation from
  earlier sessions did not reproduce; treat it as rare/unknown-condition
  and keep the fallback path (§7.4) rather than designing around it.
- **Mid-feed status polling is reliable**: responses ~90–127 ms (no
  measurable slowdown vs idle), `s=1` stable throughout the dispense.
- **`n` increments live during dispense**: ~1 per portion, ~5.1 s apart
  (feed(50): 1243→1293 while `s=1`), total delta exactly equals the
  requested portions. The "counter updates only after the dispense"
  assumption is wrong.
- **`stop()` cancels before any portion counts**: `feed(3)` + stop at
  2 s → `n`/`t` delta 0. (Verified on both power sources.)
- **Battery `b` on fresh NiMH (100%)**: 522–524 at rest. A feed(50)
  dropped it to 515 (−7 over ~250 s of motor time), front-loaded sag
  (~−4 in the first 30 s) then flat, recovering to 518 after 15 s rest.
  Compare adapter power (earlier 100-portion study: 428–461 range, −39
  drop). The value is clearly power-source-dependent and **not a simple
  mV/cell reading** — reinforces "expose raw + `≤410` low threshold, no
  percentage gauge in v1."
- **Implications adopted in this spec**: §7.4 `asf02_feed` now polls
  `s` until 0 in-session (no blind wait+reconnect); §6.2/§7.1 counter is
  live progress; `binary_sensor.feeding` can be driven by mid-feed polls
  (service-triggered refresh), not just the 15-min cadence.

## 4. Manifest

```json
{
  "domain": "asf02",
  "name": "ASF02 Pet Feeder",
  "version": "0.1.0",
  "codeowners": ["@cptskippy", "@AgentCarlBrutananadilewski"],
  "config_flow": true,
  "dependencies": ["bluetooth_adapters"],
  "integration_type": "device",
  "iot_class": "local_polling",
  "bluetooth": [
    {
      "local_name": "ASF02-*",
      "connectable": true
    }
  ],
  "requirements": ["asf02>=0.2,<0.3"]
}
```

Key points:
- `version` — **required for custom integrations** (HACS + UI display); keep in
  lockstep with the repo tag.
- `integration_type: "device"` — per the current docs, `device` = one config
  entry per physical device; `hub` = one entry governing many (Hue-style).
  Each feeder is its own config entry, so `device` is the honest value.
  (Original draft said `hub`; corrected 2026-09-18 after re-reading the
  manifest docs.)
- `dependencies: ["bluetooth_adapters"]` — ensures all remote adapters
  (ESPHome proxies) are connected before our entry sets up (HA dev docs,
  verified).
- `iot_class: local_polling` — honest: no cloud, short periodic GATT
  sessions.
- `bluetooth` matcher on `local_name: "ASF02-*"` drives **discovery flows**
  (the feeders advertise continuously with `ASF02-<last6>` names).
  `connectable: true` is mandatory — we need outgoing GATT connections.
  Consequence: advertisement-only proxies (BK72xx/LN882x) can surface the
  device in the picker but the flow must reject with a clear
  "this scanner can't connect" error (check
  `service_info.connectable` per HA docs).
- `requirements: asf02` — HA installs the library from PyPI at setup time.
  (Pre-release/PyPI-unpublished phase: ship a local `requirements` wheel or
  use HACS with `requirements` pinned to a git ref. Decide at publish time.)

## 5. Config flow

Sources: `bluetooth` (discovery), `user` (manual), `reauth` not needed
(token never expires; wrong token → setup error, recoverable via options).

```
SOURCE_BLUETOOTH (match on local_name ASF02-*)
   │
   ├─ Step 1  "device" — list discovered ASF02 devices (name + MAC + rssi +
   │                    source adapter). Manual "add device by MAC" fallback.
   │                    (unique_id = MAC, uppercase, no colons? — see §10)
   │
   └─ Step 2  "token"  — text field for the 10-char pairing token
                         (pattern: 10 alphanumerics, case-insensitive → stored
                          as entered).
                         + checkbox "Generate a new token and pair now"
                         (recovery path: calls asf02 bind() with a fresh
                          token; overwrites the device's previous token —
                          confirm wording says so explicitly).
                         On submit: async_process_advertisements() to get a
                         fresh BLEDevice, then validate:
                             client = ASF02Client(addr, token, client_factory=…)
                             await client.connect(); await client.unlock()
                         Success  → create entry.
                         ASF02LockedError / unlock failure → form error:
                             "Token was rejected. If you don't have the
                              token, check 'generate a new token' (this
                              re-pairs the feeder)."
```

Validation inside the flow uses the **same injected-client pattern** as
runtime (flow has `hass`, so it can call
`bluetooth.async_ble_device_from_address` / `async_process_advertisements`).

Config entry data (persisted):

| Key | Type | Notes |
|-----|------|-------|
| `address` | str | MAC `AA:BB:CC:DD:EE:FF` (canonical form we store) |
| `token` | str | 10-char pairing token (device-side secret; entry data is encrypted at rest by HA) |
| `tz_offset_seconds` | int | e.g. -25200 (PDT); used for `time.calibration` each session. Wire protocol uses total seconds, not minutes — verified against app source. |
| `name` | str | friendly name (title) |

Options flow (later, v1.1): poll interval, timezone, per-device override of
battery-low threshold.

## 6. Data acquisition — `ASF02Coordinator`

Plain `DataUpdateCoordinator` (device is GATT-only; advertisements carry no
payload — no processor coordinator needed).

### 6.1 The session

```python
async def _async_update_data(self) -> FeederSnapshot:
    if not bluetooth.async_address_present(hass, address):
        raise UpdateFailed("feeder not in range")
    ble_device = bluetooth.async_ble_device_from_address(hass, address, True)
    if ble_device is None:
        raise UpdateFailed("no connectable adapter can reach feeder")
    async with ASF02Client(address, token, client_factory=…) as f:
        await f.unlock()
        await f.sync_time(tz_offset)      # always — schedules run on device clock
        info    = await f.info()
        status  = await f.status()
        sched   = await f.get_schedule()  # may be [] (fw quirk) — fine
        return FeederSnapshot(info, status, sched, fetched_at=now)
```

- `update_interval`: **15 min** default (options-adjustable 5 min–1 h).
  Session cost ≈ 8–15 s of GATT (connect + ~6 commands), i.e. ~10% slot
  duty-cycle worst case per feeder; 4 feeders stagger trivially.
- Jitter: `DataUpdateCoordinator` already staggers; additionally randomize
  each feeder's offset by `hash(address) % 300s` at setup so four feeders
  never connect simultaneously (protects 3-slot proxies).
- **Every session recalibrates time** (vendor app behavior; protects against
  RTC drift on adapter-powered units and is the fix for the years-stale
  spare-unit case).
- Errors map to `UpdateFailed` → entities go stale (keep last value,
  `last_updated` frozen), **not** unavailable. Availability comes from §6.3.

### 6.2 `FeederSnapshot`

```python
@dataclasses.dataclass(frozen=True)
class FeederSnapshot:
    firmware: str          # info.f
    hw_version: str        # info.h
    serial: str            # info.s
    state: int             # status.s (0=idle, 1=feeding — live-verified)
    feed_count: int        # status.n (portions dispensed). Increments LIVE, ~1/portion as each
                           # dispenses (verified 2026-09-18: feed(50) climbed 1243→1293 during s=1),
                           # NOT just on the post-dispense re-read. Total delta == requested portions.
    feed_count_mirror: int # status.t (separate counter, survives power cycle — does NOT track n. Verified: power cycle resets u but not n or t)
    battery_raw: int       # status.b (unit unconfirmed; 410 = low threshold per vendor app)
    uptime_raw: int        # status.u
    schedule: tuple[PlanEntry, ...]
    fetched_at: datetime
```

### 6.3 Availability (ad-driven, zero GATT cost)

- `bluetooth.async_track_unavailable(hass, cb, address, connectable=True)`
  → coordinator marks device unavailable (all entities) when advertisements
  stop for the learned interval.
- `_async_handle_unavailable` semantics: `UpdateFailed("not in range")`
  keeps `available=False` until next successful update.
- HA's learned advertising interval + fallback stale-advertisement logic is
  trusted as-is (don't override in v1).

### 6.4 Why not the processor coordinators

`Passive/ActiveBluetoothProcessorCoordinator` exist for devices that push
data in **advertisements**. ASF02 advertisements are static (name + mfg
data only, no payload changes) — the docs' own decision table routes
"device only communicates via active connection" → `DataUpdateCoordinator`.
The ad events are still useful for availability + discovery, just not as the
data source.

## 7. Entities

### 7.1 Sensors

| Entity | Type | Source | Notes |
|--------|------|--------|-------|
| `sensor.<dev>_battery` | `SensorDeviceClass.BATTERY` | `battery_raw` → native | raw value + **`battery_low` binary sensor** from the vendor threshold: `b ≤ 410` = low (app source-verified 2026-09-12). Raw unit still unconfirmed (NiMH fresh ≈522, adapter ≈428–461) — expose raw as-is; a 0–100% gauge can come later calibrated against the 410 threshold |
| `sensor.<dev>_feed_count` | `SensorDeviceClass.TOTAL_INCREASING` | `feed_count` | `state_class: TOTAL_INCREASING`. Counts **portions**; increments **live, ~1 per portion, while the motor runs** (verified 2026-09-18 on feed(50): climbed steadily during `s=1`). Earlier note "update arrives on the post-feed re-read" is superseded — polling during a feed sees progress. |
| `sensor.<dev>_schedule` | `SensorDeviceClass.TIMESTAMP`? **No** — text | schedule | `native_value`: human string `"15:30 ×1 · 16:00 ×1"` (or `—`); **attributes**: `slots_0..9` = `"15:30|1|on"` per slot + `entry_count`. Automations can read attributes for machine use |
| `sensor.<dev>_device_time` | `SensorDeviceClass.TIMESTAMP` | last `info`/unlock `t` + tz | shows what the *device* thinks the time is (drift diagnostic) |

Common attributes on all: `firmware`, `serial`, `hw_version`, `fetched_at`,
`source` (which adapter/proxy served the last session — from
`BluetoothServiceInfoBleak.source`, exposed by the coordinator).

### 7.2 Binary sensors

- `binary_sensor.<dev>_feeding` — **on** while `status.s == 1` (live-verified state:
  flips ~1 s after a feed command, clears after the dispense finishes). `device_class: None`.
  Drives "is the feeder running right now" automations.
- `binary_sensor.<dev>_battery_low` — on when `status.b ≤ 410` (vendor threshold).
- `binary_sensor.<dev>_fed_recently` — on for 10 min after `feed_count` increases
  between two consecutive polls (or immediately after a service call). Coordinator
  compares snapshots, stores `last_feed_at = fetched_at` on change.

### 7.3 Events

`asf02.feed` (bus event) fired on every count increment — payload
`{entity_id, new_count, detected_at}`. Lets automations react without
relying on the binary sensor window.

### 7.4 Services (all take `entity_id` list, resolve to coordinator)

| Service | Fields | Behavior |
|---------|--------|----------|
| `asf02_feed` | `portions: int (1..99)` | single session: unlock → `feed(n)` (ACK `true` ~90 ms) → **stay connected** and poll `status()` every ~3 s until `s==0` (mid-feed polling verified reliable 2026-09-18: 11/11 probes on feed(2), 80/80 on feed(50), zero link drops on adapter *and* fresh-battery power). This yields live progress via `feed_count` and an accurate completion signal (~5.1 s/portion; feed(50) ran 250 s). **Fallback**: if the link drops mid-feed (observed once in earlier sessions, never reproduced in the 2026-09-18 probes) → disconnect, wait remaining estimate (`n × 5.1 s + 10 s`, capped), reconnect+unlock, read status. Returns `{"fed": true, "portions": n, "feed_count": N}`. Errors: `HomeAssistantError` with the ASF02 error text (locked/in-range/timeout). |
| `asf02_stop` | — | `stop()`; returns `{"ok": true}`. **Verified cancel semantics (2026-09-18):** called mid-feed it stops the dispense *before* any portion counts — `n`/`t` delta was 0 for a `feed(3)` stopped at ~2 s. Safe to expose as a "stop feeding" button; it cannot double-count. |
| `asf02_set_schedule` | `entries: list[{time: "HH:MM", portion: int(1..9), enabled: bool = true}]` max 10 | full-array replace (the only reliable write form, live-verified); uses the **write-response echo** as confirmation (read-back is unreliable on fw 1.19.12); refreshes coordinator; returns the stored slots |
| `asf02_clear_schedule` | — | `set_schedule([])` |

Service schema: `vol.Schema({vol.Required("entity_id"): cv.entity_ids, ...})`
standard helpers. `asf02_set_schedule` time field validated as `"HH:MM"`
string (library does hex encoding; the hex-not-decimal trap stays in the
library and is unit-tested there).

### 7.5 Device registry

One `DeviceInfo` per feeder: `identifiers={(DOMAIN, address)}`,
`name=<title>`, `manufacturer="Hong Kong M Spacer Limited"`,
`model="CWH01L-A1"` (FCC ID 2A9QN-CWH01L-A1; device reports OEM model
`ASF02` via `info` and advertising — use the sold model in the UI), `sw_version=firmware` (from last info),
`serial_number`, `connections={(CONNECTION_BLUETOOTH, address)}`,
`via_device`: the adapter device that served the last session (via
`source` → adapter registry entry) — nice-to-have, best-effort.

## 8. Connection management rules

1. **Transient sessions only.** Every operation (poll, service) = one
   connect→unlock→…→disconnect. No persistent handle.
2. **One session at a time per feeder** — an `asyncio.Lock` in the
   coordinator serializes poll vs. service sessions (device is
   single-connection; concurrent centrals are not supported).
3. **Re-unlock is mandatory every session** (device locks across every
   reconnect — live-verified). `connect_unlocked()` handles it; never cache
   unlocked state across sessions.
4. **Re-time is mandatory every session** (§6.1).
5. **Retries**: rely on `bleak-retry-connector` semantics baked into HA's
   wrapper for the GATT layer; application layer adds *one* full session
   retry for `UpdateFailed` on transient errors only (not for
   `ASF02LockedError` — that's a token problem, retrying wastes time and
   should surface immediately in setup/flow validation).
6. **Timeouts**: connection ≥10 s (HA dev-doc requirement for BlueZ
   service resolution); per-command response timeout 10 s (library
   default); whole-session budget ~45 s.
7. **Vendor app contention**: if the vendor Android app is connected, our
   connect will fail or the device will misbehave. Documented behavior:
   surface a clear error suggesting the vendor app be closed. (Single-host
   BLE is a physical constraint of the device, not our design.)

## 9. ESPHome proxy / multi-adapter behavior

- No integration code touches adapters. `bluetooth_adapters` dependency +
  `async_ble_device_from_address(connectable=True)` is the entire adapter
  story: HA picks the best connectable path (local dongle or ESPHome proxy
  with `active: true`), routes the GATT session through it, and manages
  slot allocation.
- **Proxy requirement (user-facing doc in README):** ESPHome
  `bluetooth_proxy:` with `active: true` (default) — advertisement-only
  proxies can't carry the GATT session.
- 4 feeders on one 3-slot proxy: fine for transient sessions (slots free
  between sessions); the per-feeder stagger (§6.1) makes simultaneous
  contention unlikely.
- If a feeder is only visible to an advertisement-only scanner, the config
  flow rejects with `service_info.connectable == False` check (per HA
  dev-doc guidance for mixed connectable/non-connectable devices).

## 10. Identity & migration

- **`unique_id = address`** (uppercase MAC). One config entry per physical
  feeder; the address is stable (burnt into the radio).
- Device registry: `(asf02, address)` identifier — stable across
  re-pairing (token changes don't affect identity).
- No migration path exists yet (v1 is first). If the library later supports
  sibling OEM models (other mfg-data product IDs in the ISSC family),
  `unique_id` stays address-based; a `model` key in entry data gates
  capability sets.

## 11. Open questions (carry from protocol work)

| # | Question | Default decision until answered |
|---|----------|---------------------------------|
| Q1 | `feeder.log` row shape (device silent on spare) | No log entity in v1; re-test on an in-use unit |
| Q2 | `status.b` units (428–461 on adapter; ~522–526 on fresh NiMH AAs — not plain mV) | **Partially resolved 2026-09-12:** vendor app threshold = `b ≤ 410` ⇒ low battery (source-verified). Expose raw `battery_raw` + `battery_low` binary sensor; skip % gauge in v1. |
| Q3 | Schedule *read* reliability on in-use firmware (spare: silent) | **Resolved 2026-09-15:** writes DO persist and fire correctly. Verified by writing a 2-portion entry for 22:30, checking counters at 22:45: `n` and `t` both incremented exactly +2. Entry was consumed after firing (post-fire read showed empty schedule). **Device clock must be correct** — entries are naive local time matched against the device's internal clock. Stale clock = silent no-fire. Always `time.calibration` first. |
| Q4 | `status.u` / `d` meaning | Expose `u` raw as attribute only |
| Q5 | Idle-disconnect timeout of the feeder (does it drop us mid-session after N s?) | **Re-opened 2026-09-18:** dedicated probes (feed 1/2/3/50, ~60 portions, 91 mid-feed polls, adapter + fresh-battery power) saw **zero** mid-feed link drops — the earlier "drops during a feed" observation did not reproduce. `s==1` and status polls work throughout. Kept a defensive fallback in §7.4 in case it's condition-dependent (see §3.2). |
| Q6 | Vendor brand string for `DeviceInfo.manufacturer` | **Resolved** (2026-09-11): brand Lulumiao, manufacturer Hong Kong M Spacer Limited, model CWH01L-A1, FCC ID 2A9QN-CWH01L-A1 (user-provided label data; matches original FCC grant 2AKG5-ASF02, 2022) |
| Q7 | Battery sag/drain **with food** (kibble) in the hopper | **TODO (needs hardware time):** both drain studies so far (2026-09-12: −39 `b` / 100 portions on adapter; 2026-09-18: −7 `b` / 50 portions on fresh NiMH) were on an **empty** feeder — floors, not production values. Re-run with kibble to get the realistic motor load (expect deeper instantaneous sag + steeper drain). |

## 12. Testing strategy

1. **Library** (done): 25 unit tests against captured wire traffic; E2E
   against the spare feeder via `examples/cli.py`.
2. **Integration unit tests**: `pytest-homeassistant-custom-component`;
   mock `ASF02Client` (the injected-client seam makes this clean — monkeypatch
   the factory), fake `bluetooth.async_ble_device_from_address`; cover:
   config flow (discovery→token→validate→entry), coordinator session
   (success/timeout/locked), service handlers, entity values.
3. **Live acceptance checklist** (spare feeder, then in-use feeder):
   - [ ] discovery flow lists feeder (local dongle)
   - [ ] discovery flow lists feeder (ESPHome proxy only, dongle unplugged)
   - [ ] adopt with existing token; adopt with generated token (re-pair)
   - [ ] wrong token → clear form error, entry not created
   - [ ] sensors update on poll; battery/counter values sane
   - [ ] `asf02_feed` dispenses; counter increments; event fires
   - [ ] `asf02_set_schedule` persists (verified via vendor app read-back as
         ground truth, since device reads are unreliable)
   - [ ] feeder out of range → entities unavailable; back in range → auto-recover
   - [ ] vendor app connected simultaneously → clean error
4. **Soak**: 4 feeders, 1 proxy, 15-min poll for 48 h; watch for slot
   exhaustion / BlueZ errors in HA logs.

## 13. Sources (verified 2026-09-11; doc re-check 2026-09-18)

- `INTEGRATION_GUIDE.md` (this directory, 2026-09-18) — current official
  developer docs: manifest keys, config entries/flows, coordinators,
  Bluetooth API rules, HACS publishing. Re-verified against
  `home-assistant/developers.home-assistant` master.
- HA core dev `homeassistant/components/bluetooth/api.py` — full current
  API surface (`async_ble_device_from_address`, `async_process_advertisements`,
  `async_track_unavailable`, `async_get_scanner`, …; **no** `async_connect`).
- HA core dev `homeassistant/components/bluetooth/__init__.py` — manager,
  slot manager (`BleakSlotManager`), remote-scanner registration.
- HA developer docs: *Bluetooth — best practices for integration authors*
  (`bluetooth_adapters` dependency, shared scanner, ≥10 s connect timeout,
  don't reuse `BleakClient`, connectable vs non-connectable controllers).
- HA developer docs: *Fetching Bluetooth data* — coordinator decision table
  (GATT-only device → `DataUpdateCoordinator`).
- `habluetooth` 7.0.0 (PyPI, 2026-09-09) — `HaBleakClientWrapper`,
  `HaBleakScannerWrapper`, `BluetoothScanningMode` exports.
- `home-assistant-bluetooth` (PyPI) — `BluetoothServiceInfo(.Bleak)`,
  `source` field (which adapter/proxy saw the device).
- ESPHome `bluetooth_proxy` docs — `active: true`, `connection_slots` (3
  default), brief-connect slot reuse.
- Protocol facts: `carl/android_apps/slow-feeder-ble-protocol.md`
  (live captures, fw 1.19.12) + `asf02` test suite.
- ha-petkit-ble `HOME_ASSISTANT_BLUETOOTH_GUIDE.md` — directionally useful,
  but its `BluetoothClient` example does **not** match current core API;
  treated as historical, not normative.

## 14. Implementation plan (after spec approval)

Status notes (2026-09-18): `asf02` 0.1.0 is **published on PyPI** and
verified against the live spare feeder (connect → unlock → info → status,
clean session). The `client_factory` seam (§3.1) is confirmed **not** in
0.1.0 — it is the gating change.

1. **Library v0.2**: `client_factory` injection seam (§3.1; small,
   backwards compatible) + release to PyPI. (Also corrects the README
   battery-unit wording.) *Everything below is blocked on this.*
2. **Skeleton**: manifest (per §4, incl. `version`), `__init__.py`
   (entry setup/unload, coordinator wiring), device registry, no entities
   yet. Load in HA, confirm entry setup with the spare.
3. **Config flow**: discovery + manual + token + generate-token paths
   (follow the patterns in `INTEGRATION_GUIDE.md` §5).
4. **Sensors + binary sensors + event** (from `FeederSnapshot`).
5. **Services** (feed/stop/set/clear).
6. **Test suite** (§12.2) to green, then **live acceptance** (§12.3).
7. **Polish**: options flow, README (proxy requirements, token recovery),
   HACS publish (manifest `version` == repo tag; see
   `INTEGRATION_GUIDE.md` §11).

Each step is independently shippable; 1 is a prerequisite for everything
else.

## 15. Companion documents

- `INTEGRATION_GUIDE.md` (this directory) — general HA integration
  development guide from the current official docs, with a
  best-practice → our-decision mapping table. Read that first, then this
  spec; this spec is the *specifics*, the guide is the *vocabulary*.
- `../android_apps/slow-feeder-ble-protocol.md` — protocol reference
  (live wire captures, fw 1.19.12).
- `asf02` PyPI project page / repo README — current library API.
