"""Mouse Trap ("C20") behaviour.

No Mouse Trap was available; expected values come from the kedube notes
(kedube/ha-goodnature-gateway docs/ble-protocol.md), which include captures
from two Mouse Traps on firmware 0.3.730:
  477fd0b2 00 04 0e00 04 -> serial B2D07F47, 14 strikes, battery state 4 (NORMAL)
  8479d0b2 00 04 0300 04 -> serial B2D07984, 3 strikes
"""

from datetime import timedelta

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.components.bluetooth import BluetoothChange, BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.goodnature_ble.const import DOMAIN, MODEL_C20
from custom_components.goodnature_ble.coordinator import (
    C20_COMMAND_FIRE,
    C20_COMMAND_WAKE,
    NORDIC_UART_RX_UUID,
    GoodnatureCoordinator,
)
from custom_components.goodnature_ble.protocol import parse_c20_advertisement

ADDRESS = "E1:E8:20:C2:C6:9A"
MFG = bytes.fromhex("477fd0b2" "00" "04" "0e00" "04")


def c20_info(mfg: bytes = MFG) -> BluetoothServiceInfoBleak:
    return BluetoothServiceInfoBleak(
        name="GN",
        address=ADDRESS,
        rssi=-60,
        manufacturer_data={0x0059: mfg},
        service_data={},
        service_uuids=["0000600d-0000-1000-8000-00805f9b34fb"],
        source="3C:71:BF:F1:C5:DA",
        device=BLEDevice(ADDRESS, "GN", {}),
        advertisement=None,
        connectable=True,
        time=0,
        tx_power=None,
        raw=None,
    )


def make(hass: HomeAssistant) -> GoodnatureCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, data={"address": ADDRESS, "name": "Mouse"})
    entry.add_to_hass(hass)
    coordinator = GoodnatureCoordinator(hass, entry)
    coordinator.enable_gatt = False
    return coordinator


def test_kedube_mouse_trap_adverts() -> None:
    a = parse_c20_advertisement(MFG)
    assert a.serial_number == "B2D07F47"
    assert a.strike_count == 14
    assert a.battery_state_raw == 4
    assert a.strikes_available is False  # flags 0x04: no kill alert
    b = parse_c20_advertisement(bytes.fromhex("8479d0b2" "00" "04" "0300" "04"))
    assert b.serial_number == "B2D07984"
    assert b.strike_count == 3


async def test_advert_sets_lifetime_count_and_battery_state_not_percent(hass: HomeAssistant) -> None:
    coordinator = make(hass)
    coordinator._async_handle_ble(c20_info(), BluetoothChange.ADVERTISEMENT)
    state = coordinator.state
    assert state.model_family == MODEL_C20
    assert state.c20_strike_count == 14
    assert state.c20_battery_state == "NORMAL"
    assert state.c20_battery_percent is None  # only from the UART poll
    assert state.c20_device_state is None  # armed traps send 0x04, not 0x01
    assert state.c20_kill_state == "CLEARED"


async def test_device_state_does_not_overwrite_strike_count(hass: HomeAssistant) -> None:
    coordinator = make(hass)
    coordinator.state.c20_strike_count = 14
    # time, ACTIVATED, CLEARED, CLOSED, NORMAL, NOT_CHARGING, DISCONNECTED, count 0
    payload = bytes(4) + bytes([1, 0, 1, 4, 0, 0]) + (0).to_bytes(4, "little")
    coordinator._apply_c20_device_state(payload)
    assert coordinator.state.c20_strike_count == 14
    assert coordinator.state.c20_device_state == "ACTIVATED"
    assert coordinator.state.c20_tray_state == "CLOSED"


def striker_event(source: int, count: int = 8) -> bytes:
    t = int(dt_util.utcnow().timestamp()).to_bytes(4, "little")
    return t + count.to_bytes(4, "little") + bytes([source]) + bytes(20)


async def test_only_sensor_strikes_are_kills(hass: HomeAssistant) -> None:
    coordinator = make(hass)
    coordinator.state.c20_strike_count = 14
    for source, label in ((2, "USER"), (3, "BACKDRIVE_TEST")):
        coordinator._apply_c20_striker_event(striker_event(source))
        assert coordinator.state.c20_last_striker_source == label
        assert coordinator.state.c20_last_kill_at is None
    coordinator._apply_c20_striker_event(striker_event(0))
    assert coordinator.state.c20_last_striker_source == "TRIGGER"
    assert coordinator.state.c20_last_kill_at is not None
    assert coordinator.state.c20_strike_count == 14  # [4..7] is not the total


def ready_to_fire(coordinator: GoodnatureCoordinator) -> None:
    s = coordinator.state
    s.model_family = MODEL_C20
    s.c20_device_state = "ACTIVATED"
    s.c20_usb_state = "DISCONNECTED"
    s.c20_kill_state = "CLEARED"
    s.c20_tray_state = "CLOSED"
    s.last_gatt_poll = dt_util.utcnow()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("c20_device_state", "ERROR", "trap error"),
        ("c20_usb_state", "CONNECTED", "on the charger"),
        ("c20_kill_state", "DETECTED", "kill not cleared"),
        ("c20_tray_state", "OPEN", "tray open"),
        ("c20_device_state", None, "not read yet"),
        ("model_family", "a24", "only a Mouse Trap"),
    ],
)
async def test_test_fire_interlocks(hass: HomeAssistant, field, value, reason) -> None:
    coordinator = make(hass)
    ready_to_fire(coordinator)
    assert coordinator.test_fire_blocked() is None
    setattr(coordinator.state, field, value)
    assert reason in coordinator.test_fire_blocked()
    with pytest.raises(HomeAssistantError, match="blocked"):
        await coordinator.async_test_fire()


async def test_test_fire_needs_recent_state(hass: HomeAssistant) -> None:
    coordinator = make(hass)
    ready_to_fire(coordinator)
    coordinator.state.last_gatt_poll = dt_util.utcnow() - timedelta(minutes=21)
    assert "out of date" in coordinator.test_fire_blocked()


async def test_poll_interval_is_15_minutes_with_backoff(hass: HomeAssistant) -> None:
    coordinator = make(hass)
    coordinator.enable_gatt = True
    now = dt_util.utcnow()
    s = coordinator.state
    s.last_seen = now
    s.last_gatt_poll = now - timedelta(minutes=14)
    assert not coordinator._should_poll_gatt(now)
    s.last_gatt_poll = now - timedelta(minutes=15, seconds=1)
    assert coordinator._should_poll_gatt(now)
    s.consecutive_sync_failures = 3
    s.last_sync_attempt = now - timedelta(minutes=3)
    assert not coordinator._should_poll_gatt(now)  # 4 min backoff


class FakeUartClient:
    """Answers every C20 request with an empty response of the same type."""

    def __init__(self, coordinator: GoodnatureCoordinator) -> None:
        self.coordinator = coordinator
        self.requests: list[tuple[int, int, bytes]] = []
        self._notify = None
        self.is_connected = True

    async def start_notify(self, uuid, callback) -> None:
        self._notify = callback

    async def stop_notify(self, uuid) -> None:
        return None

    async def write_gatt_char(self, uuid, data, response=True) -> None:
        assert uuid == NORDIC_UART_RX_UUID
        decoded = self.coordinator._decode_c20_frame(bytes(data))
        assert decoded is not None
        self.requests.append(decoded)
        mtype = decoded[0]
        self._notify(0, bytearray(self.coordinator._encode_c20_message(mtype, 0x01, decoded[2])))

    async def disconnect(self) -> None:
        self.is_connected = False


async def test_sessions_start_with_wake(hass: HomeAssistant) -> None:
    coordinator = make(hass)
    client = FakeUartClient(coordinator)
    await coordinator._async_refresh_c20_uart(client)
    first_type, first_subtype, first_payload = client.requests[0]
    assert (first_type, first_subtype) == (0x12, 0x02)
    assert int.from_bytes(first_payload, "little") == C20_COMMAND_WAKE

    coordinator2 = make(hass)
    ready_to_fire(coordinator2)
    client2 = FakeUartClient(coordinator2)
    coordinator2._async_resolve_ble_device = lambda connectable_only=False: object()

    async def connect(_):
        return client2

    coordinator2._async_connect_client = connect
    assert await coordinator2.async_test_fire() is True
    commands = [int.from_bytes(p, "little") for t, st, p in client2.requests if t == 0x12]
    assert commands == [C20_COMMAND_WAKE, C20_COMMAND_FIRE]
