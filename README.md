# ASF02 Pet Feeder Integration for Home Assistant

![ASF02 pet feeder](images/slowfeeder.png)

This custom integration lets you monitor and control the ASF02 Bluetooth pet
feeder from Home Assistant. Everything runs locally over Bluetooth — there is
no cloud and no vendor server in the loop. The integration exposes read-only
sensors for battery level, feed count, schedule, and device time, and provides
services to dispense food, stop a dispense, and manage the feeding schedule.

## Features

- **Bluetooth discovery**: automatically finds ASF02 feeders in Bluetooth range, or add one manually by MAC address.
- **Read-only sensors**: battery, feed count, the current schedule, and the feeder's internal clock.
- **Binary sensors**: whether the feeder is actively dispensing, whether the battery is low, and whether it fed recently.
- **Dispensing services**: feed a number of portions on demand, stop a dispense in progress, and set or clear the feeding schedule.
- **Adjustable polling**: choose how often the feeder is read, from 1 minute up to 1 hour.
- **Local-only**: all communication happens over Bluetooth, directly from your Home Assistant.

## Requirements

- Home Assistant **2026.9.3 or newer**.
- A way for Home Assistant to use Bluetooth — a Bluetooth adapter on the host,
  or a Bluetooth proxy (for example an ESPHome `bluetooth_proxy`). See the
  [Bluetooth integration](https://www.home-assistant.io/integrations/bluetooth/).
- The feeder itself, powered on and in Bluetooth range.

The feeder supports a single Bluetooth connection at a time. If the vendor's
phone app is open and connected to the feeder, it will compete with Home
Assistant for that connection. Keep the vendor app closed while Home Assistant
is in use.

## Installation

### 1. Install

Choose one of the following methods to install the integration:

A. Install via HACS (recommended)
   - Open Home Assistant and click on **HACS** in the sidebar.
   - Search for **"ASF02"** and select it.
   - Click **"Download"**.

B. Install manually
   - Copy the `custom_components/asf02` folder into your Home Assistant `config/custom_components/` directory.

### 2. Configure

   - **Restart Home Assistant**.
   - After restarting, navigate to **Settings > Devices & Services**.
   - Click **"Add Integration"**, find **"ASF02 Pet Feeder"**, and follow the
     prompts.

If the feeder is advertising, it will be discovered automatically and shown in
a device picker. You can also add it manually by entering its MAC address.

## Setup

Two pieces of information are needed:

- **MAC address** — the feeder's full Bluetooth MAC address (for example
  `AA:BB:CC:DD:EE:FF`). It is shown in the vendor app, and the last six
  characters also appear in the name the feeder advertises over Bluetooth.
- **Pairing token** — the 10-character pairing token from the vendor app.

If you no longer have the original token, you can instead have the integration
**generate a new token**. This re-pairs the feeder and the old token stops
working — useful for recovering a feeder you lost the token to.

The token is checked against the feeder during setup, so a wrong token is
caught right away instead of surfacing later.

## Entities

Once configured, each feeder appears in Home Assistant with the following
entities.

### Sensors

| Sensor | Description |
| --- | --- |
| **Battery** | A number that reflects the battery's charge — higher when fresh, lower as it drains. It is not a percentage (the value depends on how the feeder is powered). For a simple low-battery alert, use the **Battery low** binary sensor. |
| **Feed count** | Total portions dispensed. Counts up and never resets. |
| **Schedule** | The feeder's current feeding schedule, shown as a readable list of times and portions. |
| **Device time** | The time shown on the feeder's own internal clock. |

### Binary sensors

| Binary sensor | Description |
| --- | --- |
| **Feeding** | On while the feeder's motor is actively dispensing food. |
| **Battery low** | On when the battery reading reaches the feeder's low-battery threshold. |
| **Fed recently** | On for 10 minutes after the feeder dispenses. |

## Services

The integration provides four services. Each takes one or more `entity_id`
values so it can be pointed at a specific feeder.

### `asf02.feed`

Dispenses a number of portions immediately. By default it returns as soon as
the feeder accepts the command and does not block; set `wait` to `true` to
block until the dispense finishes.

```yaml
service: asf02.feed
data:
  entity_id: sensor.my_feeder_feed_count
  portions: 2
  wait: false
```

- `entity_id` (required) — the target feeder entity.
- `portions` (required) — how many portions to dispense, 1–99.
- `wait` (optional, default `false`) — block until the dispense completes.

### `asf02.stop`

Stops a dispense that is in progress. If the feeder is idle it does nothing.

```yaml
service: asf02.stop
data:
  entity_id: sensor.my_feeder_feed_count
```

### `asf02.set_schedule`

Replaces the feeder's entire schedule. Provide up to 10 entries, each with a
time, a portion count, and an optional enabled flag. This is best driven from
a script or automation.

```yaml
service: asf02.set_schedule
data:
  entity_id: sensor.my_feeder_feed_count
  entries:
    - time: "08:00"
      portion: 2
      enabled: true
    - time: "18:00"
      portion: 1
```

- `entity_id` (required) — the target feeder entity.
- `entries` (required) — a list of up to 10 entries. Each entry has `time`
  (`HH:MM`), `portion` (1–9), and an optional `enabled` (boolean, default
  `true`).

### `asf02.clear_schedule`

Removes all schedule entries.

```yaml
service: asf02.clear_schedule
data:
  entity_id: sensor.my_feeder_feed_count
```

## Options

- **Poll interval (minutes)** — how often Home Assistant reads the feeder,
  from 1 minute to 60 minutes (default 15). A shorter interval updates the
  sensors faster but drains the feeder's battery more quickly. Use a 1-minute
  interval only while troubleshooting.

Change this from the integration's options without restarting Home Assistant.

## Troubleshooting

### Device not discovered

1. Make sure the feeder is powered on and **not already connected to the vendor phone app** — the feeder can only talk to one device at a time.
2. Power-cycle the feeder and try again.
3. Check that Bluetooth is enabled on your Home Assistant host or that a Bluetooth proxy is configured.
4. Confirm the feeder advertises with a name beginning `ASF02-` (visible in the Bluetooth integration's device list).
5. If it is still not found, add it manually by entering its MAC address.

### Setup fails to connect or rejects the token

- A connection error usually means the feeder is out of range or the Bluetooth
  path is unavailable — check the adapter or proxy and try again.
- If the token is rejected, the feeder may have been re-paired through the
  vendor app. Re-run setup and choose the **generate a new token** option to
  re-pair the feeder.

### Nothing is dispensing

- Confirm the feeder is not currently locked out by the vendor app.
- The **Feeding** binary sensor turns on while the motor runs — use it to
  verify a dispense actually started.
