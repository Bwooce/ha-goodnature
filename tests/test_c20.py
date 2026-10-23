"""Mouse Trap ("C20") behaviour.

No Mouse Trap was available; expected values come from the kedube notes
(kedube/ha-goodnature-gateway docs/ble-protocol.md), which include captures
from two Mouse Traps on firmware 0.3.730:
  477fd0b2 00 04 0e00 04 -> serial B2D07F47, 14 strikes, battery state 4 (NORMAL)
  8479d0b2 00 04 0300 04 -> serial B2D07984, 3 strikes
"""

from datetime import timedelta
from unittest.mock import patch

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
from custom_components.goodnature_ble.protocol import C20FrameAssembler, parse_c20_advertisement

PRESENT = "custom_components.goodnature_ble.coordinator.async_address_present"

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
    with patch(PRESENT, return_value=True):
        s.last_gatt_poll = now - timedelta(minutes=14)
        assert not coordinator._should_poll_gatt(now)
        s.last_gatt_poll = now - timedelta(minutes=15, seconds=1)
        assert coordinator._should_poll_gatt(now)
        s.consecutive_sync_failures = 3
        s.last_sync_attempt = now - timedelta(minutes=3)
        assert not coordinator._should_poll_gatt(now)  # 4 min backoff


async def test_polls_continue_with_an_unchanging_advert(hass: HomeAssistant) -> None:
    # HA does not call back for an identical advert, so liveness must come
    # from the Bluetooth manager, not from the last callback.
    coordinator = make(hass)
    coordinator.enable_gatt = True
    s = coordinator.state
    s.model_family = MODEL_C20
    s.last_seen = dt_util.utcnow() - timedelta(hours=3)  # last callback long ago
    s.last_gatt_poll = dt_util.utcnow() - timedelta(minutes=16)
    coordinator._unsubscribe_ble = lambda: None  # no Bluetooth stack in this test
    with (
        patch(PRESENT, return_value=True),
        patch.object(coordinator, "_schedule_gatt_refresh") as schedule,
    ):
        await coordinator._async_update_data()
        schedule.assert_called_once()
    assert dt_util.utcnow() - s.last_seen < timedelta(seconds=5)
    with (
        patch(PRESENT, return_value=False),
        patch.object(coordinator, "_schedule_gatt_refresh") as schedule,
    ):
        await coordinator._async_update_data()
        schedule.assert_not_called()
    coordinator.async_shutdown()


DEVICE_STATE = bytes(4) + bytes([1, 0, 1, 4, 0, 0]) + bytes(4)


class FakeUartClient:
    """Answers C20 requests like a Mouse Trap, in 20-byte notifications."""

    def __init__(self, coordinator: GoodnatureCoordinator, *, echo=True, state=True, mtu_chunk=20) -> None:
        self.coordinator = coordinator
        self.requests: list[tuple[int, int, bytes]] = []
        self._notify = None
        self.is_connected = True
        self.echo = echo
        self.state = state
        self.chunk = mtu_chunk
        self.kill_times = [0] * 10
        self.delay_wake_echo = False
        self._held: bytes | None = None

    async def start_notify(self, uuid, callback) -> None:
        self._notify = callback

    async def stop_notify(self, uuid) -> None:
        return None

    async def write_gatt_char(self, uuid, data, response=True) -> None:
        assert uuid == NORDIC_UART_RX_UUID
        decoded = self.coordinator._decode_c20_frame(bytes(data))
        assert decoded is not None
        self.requests.append(decoded)
        mtype, _, payload = decoded
        is_wake = mtype == 0x12 and payload == C20_COMMAND_WAKE.to_bytes(4, "little")
        if self.delay_wake_echo and is_wake:
            # The WAKE echo is late: it arrives after the next request.
            self._held = self.coordinator._encode_c20_message(0x12, 0x01, payload)
            return
        if self._held is not None:
            held, self._held = self._held, None
            self._notify(0, bytearray(held))
            if mtype == 0x12:
                return  # the command itself is never echoed
        if mtype == 0x12 and not self.echo and not is_wake:
            return
        if mtype == 0x10:
            if not self.state:
                return
            payload = DEVICE_STATE
        elif mtype == 0x14:
            payload = b"".join(t.to_bytes(4, "little") for t in self.kill_times)
        elif mtype == 0x08:
            payload = b"0.3.730\x00"
        frame = self.coordinator._encode_c20_message(mtype, 0x01, payload)
        for i in range(0, len(frame), self.chunk):
            self._notify(0, bytearray(frame[i : i + self.chunk]))

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


def test_assembler_joins_split_and_separates_joined_frames() -> None:
    a = C20FrameAssembler()
    f1 = bytes.fromhex("b01202be30913e3eb2b5b1")  # FIRE request (CRC byte escaped)
    f2 = bytes.fromhex("b00800a694b1")
    assert a.feed(f1[:4]) == []
    assert a.feed(f1[4:] + f2) == [f1, f2]
    assert a.feed(b"\x00\x01" + f2[:2]) == []  # junk before a start is dropped
    assert a.feed(f2[2:]) == [f2]


async def test_frames_match_kedube_bytes(hass: HomeAssistant) -> None:
    # Byte-identical to kedube's encoder (hand-checked CRC-16/CCITT-FALSE).
    c = make(hass)
    enc = c._encode_c20_message
    assert enc(0x12, 0x02, C20_COMMAND_WAKE.to_bytes(4, "little")).hex() == "b01202e4708652cd86b1"
    assert enc(0x12, 0x02, C20_COMMAND_FIRE.to_bytes(4, "little")).hex() == "b01202be30913e3eb2b5b1"
    assert enc(0x08, 0x00).hex() == "b00800a694b1"
    # A body byte B2 is escaped too, so the trap's decoder cannot misread it.
    frame = enc(0x04, 0x02, bytes.fromhex("b2000000"))
    assert frame.hex() == "b00402b2b60000008b50b1"
    assert c._decode_c20_frame(frame) == (0x04, 0x02, bytes.fromhex("b2000000"))


def test_gist_mouse_trap_advert() -> None:
    # A third trap, from the gist comments (Jan 2025): armed, flags 0x04.
    a = parse_c20_advertisement(bytes.fromhex("6084d0b20004060004"))
    assert a.serial_number == "B2D08460"
    assert a.strike_count == 6
    assert a.battery_state_raw == 4
    assert not a.activated and not a.strikes_available


async def test_poll_needs_device_state(hass: HomeAssistant) -> None:
    coordinator = make(hass)
    with pytest.raises(RuntimeError, match="0x10"):
        await coordinator._async_refresh_c20_uart(FakeUartClient(coordinator, state=False))


async def test_poll_reads_kill_history_and_advances_log_window(hass: HomeAssistant) -> None:
    coordinator = make(hass)
    client = FakeUartClient(coordinator)
    newest = int(dt_util.utcnow().timestamp()) - 3600
    client.kill_times = [newest - 86400, newest] + [0] * 8
    await coordinator._async_refresh_c20_uart(client)
    s = coordinator.state
    assert s.c20_device_state == "ACTIVATED"
    assert s.firmware_version == "0.3.730"
    assert int(s.c20_last_kill_at.timestamp()) == newest
    first_end = s.c20_log_scanned_to
    assert first_end is not None
    await coordinator._async_refresh_c20_uart(FakeUartClient(coordinator))
    logs = [p for t, st, p in client.requests if t == 0x40]
    assert len(logs) == 1
    assert int.from_bytes(logs[0][4:8], "little") == first_end


async def test_log_window_starts_with_overlap(hass: HomeAssistant) -> None:
    coordinator = make(hass)
    coordinator.state.c20_log_scanned_to = 1_800_000_000
    client = FakeUartClient(coordinator)
    await coordinator._async_refresh_c20_uart(client)
    logs = [p for t, st, p in client.requests if t == 0x40]
    assert int.from_bytes(logs[0][0:4], "little") == 1_800_000_000 - 120


def setup_fire(hass: HomeAssistant, **client_kwargs):
    coordinator = make(hass)
    ready_to_fire(coordinator)
    client = FakeUartClient(coordinator, **client_kwargs)
    coordinator._async_resolve_ble_device = lambda connectable_only=False: object()

    async def connect(_):
        return client

    coordinator._async_connect_client = connect
    return coordinator, client


async def test_fire_without_echo_is_reported_but_not_failed(hass: HomeAssistant) -> None:
    coordinator, client = setup_fire(hass, echo=False)
    assert await coordinator.async_test_fire() is True
    assert "no acknowledgement" in coordinator.state.last_gatt_error
    fires = [p for t, st, p in client.requests if t == 0x12 and int.from_bytes(p, "little") == C20_COMMAND_FIRE]
    assert len(fires) == 1  # never resent


async def test_fire_rechecked_after_waiting_for_the_lock(hass: HomeAssistant) -> None:
    coordinator, client = setup_fire(hass)
    await coordinator._gatt_lock.acquire()
    import asyncio

    task = asyncio.ensure_future(coordinator.async_test_fire())
    await asyncio.sleep(0)
    coordinator.state.c20_tray_state = "OPEN"  # changed by a poll in progress
    coordinator._gatt_lock.release()
    with pytest.raises(HomeAssistantError, match="tray open"):
        await task
    assert not any(t == 0x12 for t, st, p in client.requests)


async def test_scan_response_uuids_do_not_hide_the_advert(hass: HomeAssistant) -> None:
    coordinator = make(hass)
    info = c20_info()
    info.service_uuids = [
        "0000600d-0000-1000-8000-00805f9b34fb",
        "6e400001-b5a3-f393-e0a9-e50e24dcca9e",
    ]
    coordinator._async_handle_ble(info, BluetoothChange.ADVERTISEMENT)
    assert coordinator.state.model_family == MODEL_C20
    assert coordinator.state.c20_strike_count == 14
    assert coordinator.state.serial_number == "B2D07F47"


async def test_late_wake_echo_is_not_taken_as_fire_ack(hass: HomeAssistant) -> None:
    coordinator, client = setup_fire(hass)
    client.delay_wake_echo = True
    assert await coordinator.async_test_fire() is True
    assert "no acknowledgement" in coordinator.state.last_gatt_error
