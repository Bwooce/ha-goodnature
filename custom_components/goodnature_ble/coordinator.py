"""Coordinator for Goodnature BLE trap data."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothCallbackMatcher,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_register_callback,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    A24_FAILED_SYNC_RETRY_SECONDS,
    A24_MIN_SYNC_INTERVAL_SECONDS,
    A24_RECONNECT_GUARD_SECONDS,
    A24_SYNC_WINDOW_SECONDS,
    CONF_ADDRESS,
    CONF_NAME,
    COORDINATOR_UPDATE_INTERVAL,
    DEFAULT_ACTIVE_WINDOW_SECONDS,
    DEFAULT_BURST_GAP_SECONDS,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_ENABLE_GATT,
    DEFAULT_GATT_MIN_POLL_SECONDS,
    DEFAULT_MIN_PACKETS_FOR_ACTIVATION,
    DEFAULT_NAME,
    DEFAULT_WRITE_TIME_ON_CONNECT,
    DOMAIN,
    MODEL_A24,
    MODEL_C20,
    MODEL_UNKNOWN,
    STORAGE_VERSION,
)
from .protocol import (
    A24_CONTROL_SYNC_DONE,
    APPLE_COMPANY_ID,
    UUIDS,
    a24_battery_status,
    a24_battery_volts,
    clean_name,
    encode_minutes_now,
    firmware_from_bytes,
    gn_uuid,
    parse_a24_advertisement,
    parse_c20_advertisement,
    parse_d30d,
    parse_goodnature_ibeacon,
    parse_u8,
    parse_u16_le,
    serial_from_bytes,
)

try:
    from bleak import BleakClient
except ImportError:  # pragma: no cover - Home Assistant normally provides bleak
    BleakClient = None

try:
    from bleak_retry_connector import establish_connection
except ImportError:  # pragma: no cover - local dev env may not include HA deps
    establish_connection = None

_LOGGER = logging.getLogger(__name__)
U = UUIDS
NORDIC_UART_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NORDIC_UART_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NORDIC_UART_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
MEMFAULT_DIAG_SERVICE_UUID = "54220000-f6a5-4007-a371-722f4ebd8436"
MFG_GOODNATURE = 0x0059
C20_COMMAND_FIRE = 1049702590
C20_COMMAND_CLEAR = 1682499629
C20_TYPE_SET_TIME = 0x04
C20_TYPE_FIRMWARE = 0x08
C20_TYPE_DEVICE_STATE = 0x10
C20_TYPE_BATTERY_LEVEL = 0x11
C20_TYPE_SET_COMMAND = 0x12
C20_TYPE_LOGS = 0x40
C20_TYPE_LOG_EVENT = 0x45
C20_TYPE_STRIKER_EVENT = 0x31
C20_REQUEST_SUBTYPE = 0x00
C20_RESPONSE_SUBTYPE = 0x01
C20_SET_TIME_SUBTYPE = 0x02
C20_UART_TIMEOUT_SECONDS = 4.0
C20_DEVICE_STATE = ("DEACTIVATED", "ACTIVATED", "ERROR", "NO_DATA")
C20_KILL_STATE = ("CLEARED", "DETECTED", "NO_DATA")
C20_TRAY_STATE = ("OPEN", "CLOSED", "UNKNOWN", "NO_DATA")
C20_BATTERY_STATE = ("STARTUP", "NOT_CONNECTED", "CRITICAL", "LOW", "NORMAL", "NO_DATA")
C20_CHARGE_STATE = ("NOT_CHARGING", "CHARGING", "NO_DATA")
C20_USB_STATE = ("DISCONNECTED", "CONNECTED", "NO_DATA")
C20_STRIKER_SOURCE = ("TRIGGER", "UNUSED", "USER", "NO_DATA")

# Fields of TrapState that are saved across restarts. An A24 can stay silent
# for days, so without this every restart would blank its sensors.
PERSISTED_FIELDS = (
    "model_family",
    "serial_number",
    "firmware_version",
    "kill_displayed",
    "kill_read",
    "last_strike_id",
    "last_strike_at",
    "strike_temperature_c",
    "kill_alert",
    "battery_volts",
    "battery_internal_resistance",
    "low_battery",
    "new_error",
    "unread_kills",
    "adv_flags",
    "last_seen",
    "last_sync",
)
DATETIME_FIELDS = {"last_strike_at", "last_seen", "last_sync"}


def _normalized_service_uuids(service_info: BluetoothServiceInfoBleak) -> set[str]:
    """Return service UUIDs in normalized lower-case forms."""
    normalized: set[str] = set()
    for value in service_info.service_uuids:
        uuid = value.strip().lower()
        if not uuid:
            continue
        normalized.add(uuid)

        compact = uuid.replace("-", "")
        if len(compact) == 4:
            normalized.add(f"0000{compact}-0000-1000-8000-00805f9b34fb")
        elif len(compact) == 8:
            normalized.add(f"{compact}-0000-1000-8000-00805f9b34fb")

    return normalized


@dataclass(slots=True)
class TrapState:
    """Current observed state from BLE advertisements and GATT reads."""

    address: str
    name: str
    last_seen: datetime | None = None
    last_activation: datetime | None = None
    burst_started: datetime | None = None
    last_packet: datetime | None = None
    packets_in_burst: int = 0
    last_rssi: int | None = None
    estimated_activations: int = 0
    activation_counted_for_burst: bool = False
    model_family: str = MODEL_UNKNOWN
    model_source: str | None = None

    # Identity
    serial_number: str | None = None
    firmware_version: str | None = None

    # A24 kill counters and latest record
    kill_displayed: int | None = None
    kill_read: int | None = None
    d30d_raw: str | None = None
    last_strike_id: int | None = None
    last_strike_at: datetime | None = None
    strike_temperature_c: int | None = None
    kill_alert: bool = False

    # A24 advertisement status
    adv_flags: int | None = None
    unread_kills: int | None = None
    battery_volts: float | None = None
    battery_internal_resistance: int | None = None
    low_battery: bool | None = None
    new_error: bool | None = None

    # A24 sync bookkeeping
    last_sync: datetime | None = None
    last_sync_attempt: datetime | None = None
    sync_requested: bool = False

    # C20
    c20_battery_percent: int | None = None
    c20_device_type: int | None = None
    c20_discoverable: bool | None = None
    c20_device_time: datetime | None = None
    c20_last_kill_at: datetime | None = None
    c20_strike_count: int | None = None
    c20_device_state: str | None = None
    c20_kill_state: str | None = None
    c20_tray_state: str | None = None
    c20_battery_state: str | None = None
    c20_charge_state: str | None = None
    c20_usb_state: str | None = None
    c20_kill_history_raw: str | None = None
    c20_last_striker_source: str | None = None
    c20_last_trigger_number: int | None = None
    c20_last_fire_time_ms: int | None = None
    c20_last_rewind_time_ms: int | None = None
    c20_last_backdrive_time_ms: int | None = None

    last_gatt_poll: datetime | None = None
    gatt_failures: int = 0
    last_gatt_error: str | None = None

    @property
    def battery_status(self) -> str | None:
        return a24_battery_status(self.battery_internal_resistance)


class GoodnatureCoordinator(DataUpdateCoordinator[None]):
    """Track BLE advertisements and sync trap data."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize coordinator."""
        self.config_entry = config_entry

        self.address: str = config_entry.data[CONF_ADDRESS].upper()
        self.name: str = clean_name(config_entry.data.get(CONF_NAME)) or DEFAULT_NAME

        self.cooldown = timedelta(seconds=DEFAULT_COOLDOWN_SECONDS)
        self.active_window = timedelta(seconds=DEFAULT_ACTIVE_WINDOW_SECONDS)
        self.burst_gap = timedelta(seconds=DEFAULT_BURST_GAP_SECONDS)
        self.min_packets_for_activation: int = DEFAULT_MIN_PACKETS_FOR_ACTIVATION
        self.enable_gatt: bool = DEFAULT_ENABLE_GATT
        self.gatt_min_poll = timedelta(seconds=DEFAULT_GATT_MIN_POLL_SECONDS)
        self.write_time_on_connect: bool = DEFAULT_WRITE_TIME_ON_CONNECT
        self.a24_min_sync_interval = timedelta(seconds=A24_MIN_SYNC_INTERVAL_SECONDS)
        self.a24_failed_sync_retry = timedelta(seconds=A24_FAILED_SYNC_RETRY_SECONDS)
        self.a24_reconnect_guard = timedelta(seconds=A24_RECONNECT_GUARD_SECONDS)
        self.a24_sync_window = timedelta(seconds=A24_SYNC_WINDOW_SECONDS)

        self.state = TrapState(address=self.address, name=self.name)
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{self.address.replace(':', '').lower()}"
        )
        self._unsubscribe_ble: CALLBACK_TYPE | None = None
        self._gatt_lock = asyncio.Lock()
        self._gatt_task: asyncio.Task | None = None
        self._last_disconnect: datetime | None = None
        self._reload_scheduled = False
        self.model_at_setup: str = MODEL_UNKNOWN
        self.a24_quiet_before_reload = timedelta(seconds=90)

        super().__init__(
            hass,
            logger=_LOGGER,
            name=self.name,
            update_interval=COORDINATOR_UPDATE_INTERVAL,
            config_entry=config_entry,
        )

    # ------------------------------------------------------------------
    # Persistence

    async def async_load(self) -> None:
        """Restore saved trap state."""
        data = await self._store.async_load()
        if not data:
            return
        for field in PERSISTED_FIELDS:
            if field not in data or data[field] is None:
                continue
            value = data[field]
            if field in DATETIME_FIELDS:
                value = dt_util.parse_datetime(value)
            setattr(self.state, field, value)

    def _store_data(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for field in PERSISTED_FIELDS:
            value = getattr(self.state, field)
            if isinstance(value, datetime):
                value = value.isoformat()
            out[field] = value
        return out

    @callback
    def _async_save(self) -> None:
        """Schedule a save of the persisted fields."""
        self._store.async_delay_save(self._store_data, 5)

    # ------------------------------------------------------------------
    # Advertisements

    async def _async_update_data(self) -> None:
        """No polling endpoint; BLE callbacks update state in near real-time."""
        if self._unsubscribe_ble is None:
            # connectable=False: also accept packets from passive scanners and
            # proxies. A sync still needs a connectable path, resolved later.
            self._unsubscribe_ble = async_register_callback(
                self.hass,
                self._async_handle_ble,
                BluetoothCallbackMatcher(address=self.address, connectable=False),
                BluetoothScanningMode.ACTIVE,
            )

        now = dt_util.utcnow()
        if self.state.model_family == MODEL_C20 and self._should_poll_gatt(now):
            self._schedule_gatt_refresh(reason="periodic")

        # Entities are chosen by model at setup. When the model was unknown
        # then and has since been identified, reload once to drop the other
        # model's entities, after the trap has gone quiet so a sync is not cut off.
        if (
            self.model_at_setup == MODEL_UNKNOWN
            and self.state.model_family != MODEL_UNKNOWN
            and not self._reload_scheduled
            and not (self._gatt_task and not self._gatt_task.done())
            and (
                self.state.last_packet is None
                or now - self.state.last_packet > self.a24_quiet_before_reload
            )
        ):
            self._reload_scheduled = True
            await self._store.async_save(self._store_data())
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    @callback
    def _async_handle_ble(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """Handle incoming BLE advertisements for this device."""
        del change
        now = dt_util.utcnow()
        state = self.state

        self._update_model_from_advertisement(service_info)
        if state.model_family != MODEL_C20:
            self._update_from_a24_advertisement(service_info)

        state.last_seen = now
        state.last_rssi = service_info.rssi

        if state.last_packet is None or (now - state.last_packet) > self.burst_gap:
            state.burst_started = now
            state.packets_in_burst = 1
            state.activation_counted_for_burst = False
        else:
            state.packets_in_burst += 1

        state.last_packet = now

        if (
            not state.activation_counted_for_burst
            and state.packets_in_burst >= self.min_packets_for_activation
            and (
                state.last_activation is None
                or (now - state.last_activation) >= self.cooldown
            )
        ):
            state.last_activation = now
            state.estimated_activations += 1
            state.activation_counted_for_burst = True

        if state.model_family == MODEL_C20:
            if self._should_poll_gatt(now):
                self._schedule_gatt_refresh(reason="activation")
        elif self._should_sync_a24(now):
            self._schedule_a24_sync(reason="advertisement")

        self._async_save()
        self.async_set_updated_data(None)

    @callback
    def _update_from_a24_advertisement(
        self, service_info: BluetoothServiceInfoBleak
    ) -> None:
        """Decode A24 status from raw advertisement bytes and the iBeacon."""
        state = self.state
        raw = getattr(service_info, "raw", None)
        adv = parse_a24_advertisement(raw)
        if adv is not None:
            self._set_model_family(MODEL_A24, source="a24_advertisement")
            state.serial_number = adv.serial_number or state.serial_number
            state.adv_flags = adv.flags
            state.unread_kills = adv.unread_kills
            state.low_battery = adv.low_battery
            state.new_error = adv.new_error
            if adv.battery_raw:
                state.battery_volts = adv.battery_volts
                state.battery_internal_resistance = adv.battery_internal_resistance
            self._apply_kill_count(
                self._expand_low8(adv.kill_count_low8), source="advertisement"
            )
            if adv.unread_kills and state.kill_displayed is not None:
                # Unread kills on the trap: request a sync even inside the
                # minimum interval.
                state.sync_requested = True
            return

        apple = (service_info.manufacturer_data or {}).get(APPLE_COMPANY_ID)
        serial = parse_goodnature_ibeacon(apple)
        if serial is not None:
            self._set_model_family(MODEL_A24, source="ibeacon")
            state.serial_number = state.serial_number or serial

    def _expand_low8(self, low8: int) -> int:
        """Expand the advertised low 8 bits of the kill count using the known count."""
        known = self.state.kill_displayed
        if known is None:
            return low8
        candidate = (known & ~0xFF) | low8
        if candidate < known:
            candidate += 0x100
        return candidate

    @callback
    def _apply_kill_count(self, count: int | None, *, source: str) -> None:
        """Record a kill count; raise the kill alert when it goes up.

        The first count ever seen is the baseline and does not raise the alert.
        """
        if count is None:
            return
        previous = self.state.kill_displayed
        if previous is not None and count > previous:
            self.state.kill_alert = True
            self.logger.info(
                "%s: kill count went from %s to %s (%s)",
                self.name,
                previous,
                count,
                source,
            )
        if previous is None or count >= previous:
            self.state.kill_displayed = count

    def _update_model_from_advertisement(
        self, service_info: BluetoothServiceInfoBleak
    ) -> None:
        """Infer model family from BLE advertisement shape."""
        service_uuids = _normalized_service_uuids(service_info)
        has_a24_markers = gn_uuid("D00D") in service_uuids or gn_uuid("D2ED") in service_uuids

        # C20-like profile from observed service patterns and on-device captures.
        if (
            gn_uuid("E010") in service_uuids
            or NORDIC_UART_SERVICE_UUID in service_uuids
            or MEMFAULT_DIAG_SERVICE_UUID in service_uuids
        ):
            self._set_model_family(MODEL_C20, source="service_profile")
            return

        # Only mark A24 when A24-specific channels are visible.
        if has_a24_markers:
            self._set_model_family(MODEL_A24, source="service_profile")
            return

        # Heuristic fallback: C20-like payload shape seen on GN advertisements.
        manufacturer_data = service_info.manufacturer_data or {}
        mfg_payload = manufacturer_data.get(MFG_GOODNATURE)
        c20_advertisement = (
            parse_c20_advertisement(mfg_payload)
            if "0000600d-0000-1000-8000-00805f9b34fb" in service_uuids
            else None
        )
        if c20_advertisement is not None:
            self.state.serial_number = c20_advertisement.serial_number
            self.state.c20_device_type = c20_advertisement.device_type
            self.state.c20_device_state = (
                "ACTIVATED" if c20_advertisement.activated else "DEACTIVATED"
            )
            self.state.c20_kill_state = (
                "DETECTED" if c20_advertisement.strikes_available else "CLEARED"
            )
            self.state.c20_battery_state = (
                "LOW" if c20_advertisement.low_battery else "NORMAL"
            )
            self.state.c20_charge_state = (
                "CHARGING" if c20_advertisement.charging else "NOT_CHARGING"
            )
            self.state.c20_usb_state = (
                "CONNECTED" if c20_advertisement.usb_connected else "DISCONNECTED"
            )
            self.state.c20_discoverable = c20_advertisement.discoverable
            self.state.c20_strike_count = c20_advertisement.strike_count
            self.state.c20_battery_percent = c20_advertisement.battery_percent

        if (
            mfg_payload
            and len(mfg_payload) == 9
            and not has_a24_markers
            and "0000600d-0000-1000-8000-00805f9b34fb" in service_uuids
        ):
            self._set_model_family(MODEL_C20, source="manufacturer_shape")
            return

        # Keep unknown unless explicit A24/C20 markers are observed.

    def _set_model_family(self, family: str, *, source: str) -> None:
        """Set model family with conservative precedence.

        We only overwrite unknown with a known family. Once known, keep stable.
        """
        if self.state.model_family == family:
            self.state.model_source = source
            return

        if self.state.model_family != MODEL_UNKNOWN:
            return

        self.state.model_family = family
        self.state.model_source = source

    # ------------------------------------------------------------------
    # A24 sync

    def _should_sync_a24(self, now: datetime) -> bool:
        """Decide whether to connect to an awake A24 now.

        An A24 only accepts connections while it is awake, which is when it
        advertises. Sync at most every A24_MIN_SYNC_INTERVAL_SECONDS unless
        the trap reports unread kills or a sync was requested, retry a failed
        sync after A24_FAILED_SYNC_RETRY_SECONDS, and never reconnect within
        A24_RECONNECT_GUARD_SECONDS of the last disconnect (as the app does).
        """
        state = self.state
        if not self.enable_gatt or BleakClient is None:
            return False
        if self._gatt_lock.locked() or (self._gatt_task and not self._gatt_task.done()):
            return False
        if self._last_disconnect and now - self._last_disconnect < self.a24_reconnect_guard:
            return False
        if state.last_sync_attempt and now - state.last_sync_attempt < self.a24_failed_sync_retry:
            return False
        if state.sync_requested or state.last_sync is None:
            return True
        return now - state.last_sync >= self.a24_min_sync_interval

    @callback
    def _schedule_a24_sync(self, reason: str) -> None:
        if self._gatt_task and not self._gatt_task.done():
            return
        self._gatt_task = self.hass.async_create_task(self._async_sync_a24(reason))

    async def _async_sync_a24(self, reason: str) -> bool:
        """Sync an A24 the way the Goodnature app does, then acknowledge.

        Order: read D20D, DE12, DE15, D60D, FAD1, FAD2; if there is a kill
        record, select it (write D20D) and read it (D30D); mark it read (D60D);
        write the time (F1AF); write 0x02 to DE13 ("sync done") last and
        disconnect. The trap often hangs up right after 0x02, so a failure of
        that final write is the normal end of a session. If anything before the
        acknowledgement fails, F1AF and 0x02 are skipped, as in the app.
        """
        if BleakClient is None:
            return False

        state = self.state
        async with self._gatt_lock:
            state.last_sync_attempt = dt_util.utcnow()
            ble_device = self._async_resolve_ble_device(connectable_only=True)
            if ble_device is None:
                state.last_gatt_error = "No connectable Bluetooth path to the trap"
                self.async_set_updated_data(None)
                return False

            client: BleakClient | None = None
            try:
                client = await self._async_connect_client(ble_device)
                ops = A24Session(client)
                result = await ops.run(
                    write_time=self.write_time_on_connect,
                    now=dt_util.utcnow(),
                )
                self._apply_a24_session(result)
                state.last_sync = dt_util.utcnow()
                state.last_gatt_poll = state.last_sync
                state.last_gatt_error = None
                state.sync_requested = False
                self.logger.debug(
                    "A24 sync for %s (%s): %s", self.address, reason, result
                )
                return True
            except Exception as err:  # noqa: BLE001
                state.gatt_failures += 1
                state.last_gatt_error = str(err) or type(err).__name__
                self.logger.debug(
                    "A24 sync failed for %s (%s): %s", self.address, reason, err
                )
                return False
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                self._last_disconnect = dt_util.utcnow()
                self._async_save()
                self.async_set_updated_data(None)

    @callback
    def _apply_a24_session(self, result: dict[str, Any]) -> None:
        state = self.state
        self._set_model_family(MODEL_A24, source="gatt_sync")
        if result.get("serial"):
            state.serial_number = result["serial"]
        if result.get("firmware"):
            state.firmware_version = result["firmware"]
        if result.get("battery_raw"):
            state.battery_volts = a24_battery_volts(result["battery_raw"])
        if result.get("battery_ir") is not None:
            state.battery_internal_resistance = result["battery_ir"]
        record = result.get("record")
        if record is not None:
            state.d30d_raw = record.raw_hex
            state.last_strike_id = record.record_number
            state.last_strike_at = record.strike_at
            state.strike_temperature_c = record.temperature_c
            state.last_activation = record.strike_at or state.last_activation
        self._apply_kill_count(result.get("kill_displayed"), source="gatt")
        if result.get("kill_read") is not None:
            state.kill_read = result["kill_read"]
        if result.get("acknowledged"):
            state.unread_kills = 0

    async def async_manual_poll(self) -> bool:
        """Sync now if the trap is awake, otherwise on its next wake-up."""
        if self.state.model_family == MODEL_C20:
            if BleakClient is None:
                return False
            await self._async_refresh_gatt("manual")
            return self.state.last_gatt_error is None

        self.state.sync_requested = True
        now = dt_util.utcnow()
        if self.state.last_seen and now - self.state.last_seen <= self.a24_sync_window:
            self.state.last_sync_attempt = None
            return await self._async_sync_a24("manual")
        self.state.last_gatt_error = "Trap is asleep; it will sync when it next wakes"
        self.async_set_updated_data(None)
        return False

    async def async_clear_kill_alert(self) -> bool:
        """Clear the kill alert (A24: local latch; C20: trap CLEAR command)."""
        if self.state.model_family == MODEL_C20:
            return await self._async_c20_set_command(C20_COMMAND_CLEAR, action="clear")
        self.state.kill_alert = False
        self._async_save()
        self.async_set_updated_data(None)
        return True

    # ------------------------------------------------------------------
    # C20

    def _should_poll_gatt(self, now: datetime) -> bool:
        """Return True when we should refresh C20 GATT values."""
        if not self.enable_gatt or BleakClient is None:
            return False

        if self._gatt_lock.locked():
            return False

        if self.state.last_seen is None:
            return False

        # Prefer polling while device is actively advertising.
        if now - self.state.last_seen > self.active_window:
            return False

        if self.state.last_gatt_poll is None:
            return True

        return now - self.state.last_gatt_poll >= self.gatt_min_poll

    @callback
    def _schedule_gatt_refresh(self, reason: str) -> None:
        """Start a GATT refresh task if one is not already running."""
        if self._gatt_task and not self._gatt_task.done():
            return

        self._gatt_task = self.hass.async_create_task(self._async_refresh_gatt(reason))

    async def _async_refresh_gatt(self, reason: str) -> None:
        """Connect to a C20 and refresh it over the Nordic UART."""
        if BleakClient is None:
            return

        async with self._gatt_lock:
            ble_device = self._async_resolve_ble_device()
            if ble_device is None:
                self.state.gatt_failures += 1
                self.state.last_gatt_error = "BLE device not available"
                self.async_set_updated_data(None)
                return

            client: BleakClient | None = None
            try:
                client = await self._async_connect_client(ble_device)
                await self._async_refresh_c20_uart(client)
                self.state.last_gatt_poll = dt_util.utcnow()
                self.state.last_gatt_error = None
                self.logger.debug("GATT refresh successful for %s (%s)", self.address, reason)
            except Exception as err:  # noqa: BLE001
                self.state.gatt_failures += 1
                self.state.last_gatt_error = str(err)
                self.logger.debug(
                    "GATT refresh failed for %s (%s): %s",
                    self.address,
                    reason,
                    err,
                )
            finally:
                if client and client.is_connected:
                    try:
                        await client.disconnect()
                    except Exception as err:  # noqa: BLE001
                        self.logger.debug(
                            "Disconnect failed for %s (%s): %s",
                            self.address,
                            reason,
                            err,
                        )
                self.async_set_updated_data(None)

    async def async_test_fire(self) -> bool:
        """Test fire a C20 (A24 has no BLE test fire)."""
        if self.state.model_family != MODEL_C20:
            self.state.last_gatt_error = "Test fire is only supported on the C20"
            self.async_set_updated_data(None)
            return False
        return await self._async_c20_set_command(C20_COMMAND_FIRE, action="test_fire")

    async def _async_c20_set_command(self, command: int, *, action: str) -> bool:
        """Send a C20 SetCommandRequest using the app protocol."""
        if BleakClient is None:
            return False

        async with self._gatt_lock:
            ble_device = self._async_resolve_ble_device()
            if ble_device is None:
                self.state.gatt_failures += 1
                self.state.last_gatt_error = "BLE device not available"
                self.async_set_updated_data(None)
                return False

            client: BleakClient | None = None
            try:
                client = await self._async_connect_client(ble_device)
                queue: asyncio.Queue[bytes] = asyncio.Queue()

                def _on_notify(_: int, data: bytearray) -> None:
                    queue.put_nowait(bytes(data))

                await client.start_notify(NORDIC_UART_TX_UUID, _on_notify)
                command_response = await self._async_c20_request_response(
                    client,
                    queue,
                    C20_TYPE_SET_COMMAND,
                    payload=command.to_bytes(4, byteorder="little", signed=False),
                    request_subtype=C20_SET_TIME_SUBTYPE,
                )
                ok = command_response is not None

                if not ok:
                    self.state.gatt_failures += 1
                    self.state.last_gatt_error = f"C20 {action} command failed"
                    self.async_set_updated_data(None)
                    return False

                # Allow immediate async event notifications (state/striker event) to land.
                end_at = self.hass.loop.time() + 2.0
                while True:
                    remaining = end_at - self.hass.loop.time()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(queue.get(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    decoded = self._decode_c20_frame(raw)
                    if decoded is None:
                        continue
                    message_type, subtype, payload = decoded
                    if subtype != C20_RESPONSE_SUBTYPE:
                        continue
                    if message_type == C20_TYPE_DEVICE_STATE:
                        self._apply_c20_device_state(payload)
                    elif message_type == C20_TYPE_STRIKER_EVENT:
                        self._apply_c20_striker_event(payload)

                self.state.last_gatt_poll = dt_util.utcnow()
                self.state.last_gatt_error = None
                self.async_set_updated_data(None)
                return True
            except Exception as err:  # noqa: BLE001
                self.state.gatt_failures += 1
                self.state.last_gatt_error = str(err)
                self.async_set_updated_data(None)
                return False
            finally:
                if client and client.is_connected:
                    try:
                        await client.stop_notify(NORDIC_UART_TX_UUID)
                    except Exception:  # noqa: BLE001
                        pass
                if client and client.is_connected:
                    try:
                        await client.disconnect()
                    except Exception as err:  # noqa: BLE001
                        self.logger.debug(
                            "Disconnect failed for %s (%s): %s",
                            self.address,
                            action,
                            err,
                        )


    def _decode_c20_frame(self, frame: bytes) -> tuple[int, int, bytes] | None:
        """Decode a single C20 UART frame into (message_type, subtype, payload)."""
        if len(frame) < 6 or frame[0] != 0xB0 or frame[-1] != 0xB1:
            return None

        escaped = frame[1:-1]
        body = bytearray()
        index = 0
        while index < len(escaped):
            value = escaped[index]
            if value == 0xB2:
                index += 1
                if index >= len(escaped):
                    return None
                body.append(escaped[index] ^ 0x04)
            else:
                body.append(value)
            index += 1

        if len(body) < 4:
            return None

        payload_and_header = bytes(body[:-2])
        crc_expected = int.from_bytes(body[-2:], byteorder="little", signed=False)
        crc_calculated = self._crc16_ccitt(payload_and_header)
        if crc_expected != crc_calculated:
            return None

        message_type = payload_and_header[0]
        subtype = payload_and_header[1]
        payload = payload_and_header[2:]
        return message_type, subtype, payload

    def _encode_c20_message(self, message_type: int, subtype: int, payload: bytes = b"") -> bytes:
        """Encode a C20 UART message frame."""
        body = bytes([message_type, subtype]) + payload
        crc = self._crc16_ccitt(body).to_bytes(2, byteorder="little", signed=False)
        escaped = bytearray()
        for value in body + crc:
            if value in (0xB0, 0xB1):
                escaped.append(0xB2)
                escaped.append(value ^ 0x04)
            else:
                escaped.append(value)
        return bytes([0xB0]) + bytes(escaped) + bytes([0xB1])

    async def _async_c20_request_response(
        self,
        client: BleakClient,
        queue: asyncio.Queue[bytes],
        message_type: int,
        *,
        payload: bytes = b"",
        request_subtype: int = C20_REQUEST_SUBTYPE,
        timeout: float = C20_UART_TIMEOUT_SECONDS,
    ) -> bytes | None:
        """Send one C20 UART request and wait for matching response payload."""
        request = self._encode_c20_message(message_type, request_subtype, payload)
        if not await self._async_write_char(client, NORDIC_UART_RX_UUID, request):
            return None

        end_at = self.hass.loop.time() + timeout
        while True:
            remaining = end_at - self.hass.loop.time()
            if remaining <= 0:
                return None
            try:
                raw = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

            decoded = self._decode_c20_frame(raw)
            if decoded is None:
                continue
            response_type, response_subtype, response_payload = decoded
            if response_type == message_type and response_subtype == C20_RESPONSE_SUBTYPE:
                return response_payload

            # Keep state in sync when asynchronous striker events arrive.
            if response_type == C20_TYPE_DEVICE_STATE and response_subtype == C20_RESPONSE_SUBTYPE:
                self._apply_c20_device_state(response_payload)
            elif response_type == C20_TYPE_STRIKER_EVENT and response_subtype == C20_RESPONSE_SUBTYPE:
                self._apply_c20_striker_event(response_payload)

    def _enum_at(self, values: tuple[str, ...], index: int) -> str:
        """Return enum label for index with NO_DATA fallback."""
        if 0 <= index < len(values):
            return values[index]
        return values[-1]

    def _parse_u32_le(self, payload: bytes, offset: int) -> int | None:
        """Parse little-endian uint32 from payload offset."""
        end = offset + 4
        if len(payload) < end:
            return None
        return int.from_bytes(payload[offset:end], byteorder="little", signed=False)

    def _u32_to_datetime(self, value: int | None) -> datetime | None:
        """Convert unix seconds to UTC datetime."""
        if value is None or value <= 0:
            return None
        return dt_util.utc_from_timestamp(value)

    def _apply_c20_device_state(self, payload: bytes) -> None:
        """Apply DeviceStateResponse payload (type 0x10)."""
        if len(payload) < 14:
            return
        state_time = self._parse_u32_le(payload, 0)
        self.state.c20_device_time = self._u32_to_datetime(state_time)
        self.state.c20_device_state = self._enum_at(C20_DEVICE_STATE, payload[4])
        self.state.c20_kill_state = self._enum_at(C20_KILL_STATE, payload[5])
        self.state.c20_tray_state = self._enum_at(C20_TRAY_STATE, payload[6])
        self.state.c20_battery_state = self._enum_at(C20_BATTERY_STATE, payload[7])
        self.state.c20_charge_state = self._enum_at(C20_CHARGE_STATE, payload[8])
        self.state.c20_usb_state = self._enum_at(C20_USB_STATE, payload[9])
        self.state.c20_strike_count = self._parse_u32_le(payload, 10)

    def _apply_c20_battery_level(self, payload: bytes) -> None:
        """Apply BatteryLevelResponse payload (type 0x11)."""
        if len(payload) < 5:
            return
        state_time = self._parse_u32_le(payload, 0)
        self.state.c20_device_time = self._u32_to_datetime(state_time)
        self.state.c20_battery_percent = payload[4]

    def _apply_c20_striker_event(self, payload: bytes) -> None:
        """Apply StrikerEvent payload (type 0x31)."""
        if len(payload) < 21:
            return
        event_time = self._parse_u32_le(payload, 0)
        strike_count = self._parse_u32_le(payload, 4)
        source_idx = payload[8]
        trigger_number = self._parse_u32_le(payload, 9)
        fire_time_ms = self._parse_u32_le(payload, 13)
        rewind_time_ms = self._parse_u32_le(payload, 17)
        backdrive_time_ms = self._parse_u32_le(payload, 25) if len(payload) >= 29 else None

        event_dt = self._u32_to_datetime(event_time)
        self.state.c20_device_time = event_dt or self.state.c20_device_time
        self.state.c20_strike_count = strike_count or self.state.c20_strike_count
        self.state.c20_last_kill_at = event_dt or self.state.c20_last_kill_at
        self.state.last_activation = event_dt or self.state.last_activation
        self.state.last_strike_at = event_dt or self.state.last_strike_at
        self.state.c20_last_striker_source = self._enum_at(C20_STRIKER_SOURCE, source_idx)
        self.state.c20_last_trigger_number = trigger_number
        self.state.c20_last_fire_time_ms = fire_time_ms
        self.state.c20_last_rewind_time_ms = rewind_time_ms
        self.state.c20_last_backdrive_time_ms = backdrive_time_ms

    async def _async_refresh_c20_uart(self, client: BleakClient) -> None:
        """Refresh C20 telemetry using the observed UART request/response flow."""
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def _on_notify(_: int, data: bytearray) -> None:
            queue.put_nowait(bytes(data))

        await client.start_notify(NORDIC_UART_TX_UUID, _on_notify)
        try:
            if self.write_time_on_connect:
                now_seconds = int(dt_util.utcnow().timestamp())
                await self._async_c20_request_response(
                    client,
                    queue,
                    C20_TYPE_SET_TIME,
                    payload=now_seconds.to_bytes(4, byteorder="little", signed=False),
                    request_subtype=C20_SET_TIME_SUBTYPE,
                )

            firmware = await self._async_c20_request_response(client, queue, C20_TYPE_FIRMWARE)
            if firmware is not None:
                self.state.firmware_version = firmware.split(b"\x00", 1)[0].decode(
                    "utf-8",
                    errors="ignore",
                ) or self.state.firmware_version

            battery = await self._async_c20_request_response(client, queue, C20_TYPE_BATTERY_LEVEL)
            if battery is not None:
                self._apply_c20_battery_level(battery)

            device_state = await self._async_c20_request_response(client, queue, C20_TYPE_DEVICE_STATE)
            if device_state is not None:
                self._apply_c20_device_state(device_state)

            await self._async_refresh_c20_history(client, queue)
        finally:
            try:
                await client.stop_notify(NORDIC_UART_TX_UUID)
            except Exception:  # noqa: BLE001
                pass

    async def _async_refresh_c20_history(
        self, client: BleakClient, queue: asyncio.Queue[bytes]
    ) -> None:
        """Fetch C20 striker history using the app's LogsRequest flow."""
        end_seconds = int(dt_util.utcnow().timestamp())
        start_seconds = (
            int(self.state.last_gatt_poll.timestamp())
            if self.state.last_gatt_poll is not None
            else max(0, end_seconds - 86400)
        )
        # FLAG_STRIKER_EVENTS is bit 1 in the app.
        payload = (
            start_seconds.to_bytes(4, "little", signed=False)
            + end_seconds.to_bytes(4, "little", signed=False)
            + (1 << 1).to_bytes(4, "little", signed=False)
        )
        request = self._encode_c20_message(C20_TYPE_LOGS, C20_REQUEST_SUBTYPE, payload)
        if not await self._async_write_char(client, NORDIC_UART_RX_UUID, request):
            return

        history_frames: list[str] = []
        end_at = self.hass.loop.time() + C20_UART_TIMEOUT_SECONDS
        while True:
            remaining = end_at - self.hass.loop.time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            decoded = self._decode_c20_frame(raw)
            if decoded is None:
                continue
            message_type, subtype, response_payload = decoded
            if subtype != C20_RESPONSE_SUBTYPE:
                continue
            if message_type == C20_TYPE_LOGS:
                # LogsResponse is the terminator after the streamed LogEvents.
                break
            elif message_type == C20_TYPE_LOG_EVENT:
                nested = self._decode_c20_frame(response_payload)
                if nested is None:
                    continue
                nested_type, nested_subtype, nested_payload = nested
                if (
                    nested_type == C20_TYPE_STRIKER_EVENT
                    and nested_subtype == C20_RESPONSE_SUBTYPE
                ):
                    self._apply_c20_striker_event(nested_payload)
                    history_frames.append(nested_payload.hex())

        if history_frames:
            self.state.c20_kill_history_raw = ",".join(history_frames)

    async def _async_write_char(self, client: BleakClient, uuid: str, value: bytes) -> bool:
        """Write a characteristic safely."""
        try:
            await client.write_gatt_char(uuid, value, response=True)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _encode_c20_set_command(self, command_u32: int) -> bytes:
        """Encode C20 Nordic UART SetCommandRequest frame from the current protocol."""
        return self._encode_c20_message(
            0x12,
            0x02,
            command_u32.to_bytes(4, byteorder="little", signed=False),
        )

    def _crc16_ccitt(self, data: bytes) -> int:
        """CRC16-CCITT (poly 0x1021, init 0xFFFF)."""
        crc = 0xFFFF
        for value in data:
            work = (value & 0xFF) << 8
            for _ in range(8):
                bit = (crc ^ work) & 0x8000
                crc = (crc << 1) & 0xFFFF
                if bit:
                    crc ^= 0x1021
                work = (work << 1) & 0xFFFF
        return crc & 0xFFFF

    async def _async_connect_client(self, ble_device):
        """Establish a BLE connection using HA-recommended retry connector."""
        if BleakClient is None:
            raise RuntimeError("bleak is not available")

        if establish_connection is not None:
            return await establish_connection(
                BleakClient,
                ble_device,
                self.name,
                max_attempts=2,
            )

        client = BleakClient(ble_device, timeout=10.0)
        await client.connect()
        return client

    async def _async_read_char(self, client: BleakClient, uuid: str) -> bytes | None:
        """Read a characteristic safely."""
        try:
            return await client.read_gatt_char(uuid)
        except Exception:  # noqa: BLE001
            return None

    def _async_resolve_ble_device(self, connectable_only: bool = False):
        """Resolve current BLE device record by address."""
        ble_device = async_ble_device_from_address(
            self.hass,
            self.address,
            connectable=True,
        )
        if ble_device is not None or connectable_only:
            return ble_device

        return async_ble_device_from_address(
            self.hass,
            self.address,
            connectable=False,
        )

    @property
    def ble_device_name(self) -> str | None:
        """Try to resolve the live BLE device name."""
        device = async_ble_device_from_address(self.hass, self.address, connectable=True)
        if device and device.name:
            return clean_name(device.name)
        return None

    @property
    def model_name(self) -> str:
        """User-facing model label."""
        if self.state.model_family == MODEL_C20:
            return "C20 Mouse Trap"
        if self.state.model_family == MODEL_A24:
            return "A24"
        return "Unknown"

    def async_shutdown(self) -> None:
        """Unregister callbacks."""
        if self._unsubscribe_ble:
            self._unsubscribe_ble()
            self._unsubscribe_ble = None

        if self._gatt_task and not self._gatt_task.done():
            self._gatt_task.cancel()
            self._gatt_task = None


class A24Session:
    """One A24 GATT session in the Goodnature app's order.

    Kept separate from the coordinator so it can be tested with a fake client.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.log: list[tuple[str, str, bytes | None]] = []

    async def _read(self, short_id: str) -> bytes | None:
        try:
            value = await self._client.read_gatt_char(U[short_id])
        except Exception:  # noqa: BLE001
            value = None
        self.log.append(("read", short_id, None if value is None else bytes(value)))
        return None if value is None else bytes(value)

    async def _write(self, short_id: str, value: bytes) -> None:
        self.log.append(("write", short_id, value))
        await self._client.write_gatt_char(U[short_id], value, response=True)

    async def run(self, *, write_time: bool, now: datetime) -> dict[str, Any]:
        """Run the session and return what was read.

        Raises if the kill count cannot be read or a kill record cannot be
        fetched; in that case nothing has been acknowledged.
        """
        result: dict[str, Any] = {"acknowledged": False}

        d20d = await self._read("D20D")
        kill_displayed = parse_u16_le(d20d)
        if kill_displayed is None:
            raise RuntimeError("Could not read the kill count (D20D)")
        result["kill_displayed"] = kill_displayed

        result["serial"] = serial_from_bytes(await self._read("DE12"))
        result["firmware"] = firmware_from_bytes(await self._read("DE15"))
        result["kill_read"] = parse_u16_le(await self._read("D60D"))
        result["battery_raw"] = parse_u8(await self._read("FAD1"))
        result["battery_ir"] = parse_u8(await self._read("FAD2"))

        if kill_displayed > 0:
            selector = kill_displayed.to_bytes(2, "little")
            await self._write("D20D", selector)
            record = parse_d30d(await self._read("D30D"))
            if record is None:
                raise RuntimeError("Could not read the newest kill record (D30D)")
            result["record"] = record
            if result["kill_read"] != kill_displayed:
                await self._write("D60D", selector)
                result["kill_read"] = kill_displayed

        if write_time:
            await self._write("F1AF", encode_minutes_now(now))

        # "Sync done". The trap often hangs up before confirming this write,
        # which is the normal end of a session, not a failure.
        try:
            await self._write("DE13", bytes([A24_CONTROL_SYNC_DONE]))
        except Exception:  # noqa: BLE001
            pass
        result["acknowledged"] = True
        return result
