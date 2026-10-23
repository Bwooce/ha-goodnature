# Goodnature BLE (Home Assistant Custom Integration)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Bwooce&repository=ha-goodnature&category=integration)

This integration supports Goodnature BLE traps (A24 Chirp and C20) via:

- passive BLE advertisement tracking, and
- optional active GATT polling/writes.

The goal is to use trap-reported counters when available and only infer from BLE bursts when needed.

## This fork

Fork of [codyc1515/ha-goodnature](https://github.com/codyc1515/ha-goodnature) with the A24
reworked using the protocol notes published in
[kedube/ha-goodnature-gateway](https://github.com/kedube/ha-goodnature-gateway/blob/main/docs/ble-protocol.md)
(derived from the Goodnature Android app and real traps). That project is an ESPHome gateway
under GPL-3.0; no code was copied from it, only protocol facts. The C20 code is unchanged from upstream.

A24 changes in 0.3.0:

- **Works through passive Bluetooth proxies.** The advertisement listener no longer requires
  connectable packets, so traps heard only by a passive ESPHome proxy are tracked.
- **Decodes the A24 advertisement** (from the raw bytes): serial, kill count, unread kills, battery
  voltage and internal resistance, low-battery and error flags. An A24 Chirp's main advertisement
  is an iBeacon whose major/minor carry the serial; that is also recognised.
- **Syncs like the app, then acknowledges.** When the trap wakes, it connects, reads the kill count,
  firmware and battery, reads the newest kill record (time and air temperature), marks it read,
  sets the trap's clock and writes the "sync done" control value. Without that last write the trap
  keeps its new-event flag set and keeps waking to report, which costs battery. Side effect: the
  Goodnature app still downloads kills but no longer shows its own new-strike notification.
- **Kill Alert** turns on when the kill count goes up (the first count seen is the baseline) and
  stays on until **Clear Kill Alert**.
- **State is saved** across Home Assistant restarts, since an A24 can be silent for days.
- **Removed for the A24:** Test Fire (the BLE command does not fire an A24) and Reset Alert
  (acknowledgement is now automatic). The A24 has no battery percentage or CO2 count over BLE.
- Fixed the device name being overwritten with the advertised `GN\0\0`.

A24 entities: Strikes, Kill Alert, Last Strike, Last Strike Temperature, Battery Voltage,
Battery Status, Battery Low, Unread Kills, Last Seen, Last Sync, Signal Strength,
Poll Now, Clear Kill Alert. **Poll Now** syncs immediately if the trap is awake, otherwise at its
next wake-up; an A24 cannot be woken over Bluetooth.

Changes in 0.3.3, from a byte-by-byte review against the kedube gateway and app notes:

- A failed or missing kill record no longer blocks the acknowledgement (the trap would keep
  waking); only an unreadable kill count aborts a sync. Failed syncs back off 1, 2, 4 ... 30 min.
- The advertised kill byte only moves the count forward by less than 128; anything else waits for
  the next GATT read, which can also lower the count. An advert-only starting count (low 8 bits)
  is replaced silently by the first full count.
- Kill Alert also turns on for kills the trap reports as unread, including on first contact, and for
  kills read but not yet acknowledged on the trap.
- Kill records with a time before 2017 or in the future, or that are not the newest, are ignored.
- The newest record is not re-read when it is already known and acknowledged.

Limitation: the A24 status slots are decoded from raw advertisement bytes, which Home Assistant
provides for ESPHome Bluetooth proxies but not for a local adapter. Through a local adapter the
trap is still identified (iBeacon) and synced, but advert-only values (unread kills, flags) are not
available between syncs. Home Assistant does not keep the order of the 16-bit UUID list, so the
slots cannot be rebuilt from it safely.

Mouse Trap ("C20") changes, **untested on a real Mouse Trap** (none available), following the
kedube notes and gateway, which were developed against two Mouse Traps on firmware 0.3.730:

- Polls every 15 minutes (it was every 2 minutes for as long as the trap advertises, which is
  always), with the same failure backoff as the A24.
- **Test Fire is refused** unless the trap's state was read in the last 20 minutes and it is not
  in error, not on the charger, has no kill waiting and has its tray closed (the app's conditions).
- Strikes come only from the advertisement's lifetime counter; the device-state and striker-event
  counts are not lifetime totals and no longer overwrite it.
- The advertisement's last byte is the battery *state* (4 = normal), not a percentage; the
  percentage comes from the poll. The armed state is not taken from the advertisement (armed traps
  send 0x04, not the documented 0x01).
- Only sensor-triggered strikes count as kills (not test fires or motor self-tests).
- Every session starts with the app's WAKE command.

Known gaps: the app's kill-history request (0x14) is not used (kills are counted from the
advertisement), and a Mouse Trap whose Bluetooth address changes would need re-adding.

Where the upstream notes below disagree with this section, this section is current.

Tests: `pip install -r requirements_test.txt && pytest`.

## Current status

- Discovery by:
  - local name `GN`
  - `D00D`, `DE11`, `FADE`, `E010` service UUIDs
  - legacy `1234` and `600D` service UUID advertisements (short or expanded form)
- Passive mode works without connecting to the trap.
- GATT/UART mode reads trap counters and metadata.
- Acknowledge/reset actions are implemented from observed device write flows.
- C20 UART session uses a consistent polling flow (version, battery percent, device state, kill history).

## Installation

1. Copy `custom_components/goodnature_ble` into your Home Assistant config directory.
2. Restart Home Assistant.
3. Go to **Settings -> Devices & Services -> Add Integration**.
4. Add **Goodnature BLE**.
5. If not auto-discovered, enter trap Bluetooth MAC manually.

## Entities exposed

### Binary sensor

- `Charge` (battery_charging class)
- `Battery` (problem class)
- `Trap Occupancy` (occupancy class)
- `USB` (connectivity class)
- `Tray` (opening class)
- `Discoverable` (C20 advertisement flag)

### Core sensors

- `Activations` (counter with source precedence above)
- `Signal Strength`

### Trap-state sensors (GATT-backed when available)

- `Serial Number`
- `Firmware Version`
- `Battery` (C20 percent, trap-reported)
- `Device Type` (C20 advertisement value, disabled by default)
- `Last Kill At` (C20 kill history-derived, diagnostic)
- `Device` (C20, diagnostic)

### Buttons

- `Poll Now`
- `Test Fire`
- `Clear Kill Alert` (C20)
- `Reset Alert` (A24)

`Test Fire` uses model-specific write paths:

- C20:
  - Nordic UART write to `6E400002...`
  - framed `SetCommandRequest(FIRE)` packet (command id `1049702590`)
- A24:
  - writes device control `DE13 = 0x05`
  - selects the returned kill via `D20D` and reads its `D30D` strike record

`Clear Kill Alert` sends the C20 `SetCommandRequest(CLEAR)` command id `1682499629`.

## Options

No user-configurable runtime options are currently exposed. The integration uses built-in defaults.

## Known caveats

- Chirp is a sleepy BLE device; proximity matters.
- Manual wake/shake can resemble real strikes in passive mode.
- Some fields remain unknown/partially understood; unknown raws are exposed for analysis.
- Not all protocol paths are guaranteed stable across firmware versions.
- App UI items like donut life and full activity timeline likely include cloud/server data; BLE alone may not provide all of those derived values.
- Pairing-key retrieval, firmware transfer, and Memfault diagnostic export are intentionally
  not exposed: they are provisioning/support flows that require secrets, signed firmware,
  or an app/cloud upload destination rather than Home Assistant entities.

## Recommended topology

Use ESPHome Bluetooth proxies near remote traps for reliable range and better packet capture.

## Technical details

### Key terminology

These are the most important semantics.

#### Displayed / Read / Pending

For both kills and events the trap exposes two counters:

- `displayed`: how many items the trap says are currently raised/displayed as new.
- `read`: how many items the client has acknowledged/read.
- `pending`: computed by this integration as `max(0, displayed - read)`.

So yes, this creates three related sensors per channel. They are not duplicates:

- `displayed` is trap-side surfaced count.
- `read` is client-side acknowledged pointer.
- `pending` is actionable delta.

#### Kill vs Event

- `kill`: strike/kill channel (`D20D` displayed, `D60D` read pointer, `D30D` strike payload).
- `event`: broader event/alert channel (`D2ED` displayed, `D3ED` read pointer, `DEED` payload).

In practice:

- Kills are the trap-hit stream users usually care about first.
- Events appear to include other noteworthy trap/device events.

### Activation counting model

The `Activations` sensor prefers trap counters over inferred bursts:

1. `kill_displayed` if available
2. else `kill_read` if available
3. else fallback to inferred BLE burst count

That means in GATT mode activation total is no longer purely estimated.

### BLE/GATT protocol map (known)

All Goodnature custom UUIDs use base suffix:

- `-1212-efde-1523-785fef13d123`

#### Device info/service area

- `DE11` service root (device information service area)
- `DE12` serial number (string)
- `DE13` device control (u8 observed; writable)
- `DE14` device state raw
- `DE15` firmware/version text
- `DE18` unknown raw (captured)

#### Kill channel

- `D00D` service root
- `D20D` kill displayed counter
- `D30D` kill/strike payload
- `D50D` kill state raw
- `D60D` kill read pointer (writable)

#### Event channel

- `D2ED` event displayed counter
- `DEED` event payload raw
- `D3ED` event read pointer (writable)

#### Battery / telemetry

- `FADE` unknown raw
- `FAD1` battery voltage raw
- `FAD2` battery internal resistance raw
- `FAD3` unknown battery-related raw

#### Time

- `F1AE` time service
- `F1AF` time characteristic (writable, little-endian minutes)

#### Unknown/diagnostic

- `E010` unknown service root (observed on C20-like profiles)
- `E771`, `E772`, `E773` unknown raw (E771 appears writable in protocol docs)

### Observed write flows (action perspective)

These flows are based on observed device/client behavior.

#### A24

- On connect/sync start:
  - writes current time to `F1AF` (`write_time_on_connect` option)
- Control write path:
  - `DE13 = 0x00` exists as a control action path
- Acknowledge/reset path:
  - optional write to `D60D` (kill read pointer)
  - optional write to `D3ED` (event read pointer)
  - then `DE13 = 0x02`

#### C20

C20 uses Nordic UART writes (`6E400002`) with framed command packets, including:

- set time
- get version/state/battery/history
- set command (`FIRE`, `CLEAR`, etc)

Memfault diagnostics also write to a separate characteristic (`54220005...`).

This integration follows the C20 connect/poll sequence implemented by app 3.2.0:

- subscribe notify on `6E400003`
- set time (`type 0x04`, subtype `0x02`)
- request firmware (`0x08`), battery (`0x11`), device state (`0x10`)
- request striker history with `LogsRequest` (`0x40`, striker-event flag)
- parse responses (`subtype 0x01`)

From `btsnoop2` (triggering a strike/test-fire action), the flow is:

- write `SetCommandRequest(FIRE)` (`type 0x12`, `subtype 0x02`, payload `0x3E9130BE`)
- receive command response (`type 0x12`, `subtype 0x01`)
- receive `DeviceStateResponse` (`0x10`) and `StrikerEvent` (`0x31`)

The `StrikerEvent` includes source (`TRIGGER` vs `USER`) and timing metrics; `USER` indicates manual test fire.
