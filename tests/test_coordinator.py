"""Coordinator behaviour with Home Assistant running."""

from datetime import timedelta
from unittest.mock import patch

from bleak.backends.device import BLEDevice
from homeassistant.components.bluetooth import BluetoothChange, BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.goodnature_ble.const import DOMAIN, MODEL_A24
from custom_components.goodnature_ble.coordinator import GoodnatureCoordinator

ADDRESS = "D3:58:7F:15:99:0B"
IBEACON = bytes.fromhex("0215b0b0eee7b9b04bc5b5e4f5cb610eb7008fc93be336")
ADV = bytes.fromhex("0201061aff4c000215b0b0eee7b9b04bc5b5e4f5cb610eb7008fc93be336")


def scan_rsp(kills: int, unread: int = 0) -> bytes:
    return bytes.fromhex("0d038fc93be336003412" "01d9") + bytes([unread, kills]) + bytes.fromhex(
        "0509474e0000"
    )


def service_info(raw: bytes | None) -> BluetoothServiceInfoBleak:
    return BluetoothServiceInfoBleak(
        name="GN",
        address=ADDRESS,
        rssi=-75,
        manufacturer_data={76: IBEACON},
        service_data={},
        service_uuids=[],
        source="3C:71:BF:F1:C5:DA",
        device=BLEDevice(ADDRESS, "GN", {}),
        advertisement=None,
        connectable=True,
        time=0,
        tx_power=None,
        raw=raw,
    )


def make_coordinator(hass: HomeAssistant, name: str = "Trap") -> GoodnatureCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS, data={"address": ADDRESS, "name": name}
    )
    entry.add_to_hass(hass)
    coordinator = GoodnatureCoordinator(hass, entry)
    coordinator.enable_gatt = False  # no connections in these tests
    return coordinator


async def test_first_count_is_baseline_then_increase_raises_alert(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    coordinator._async_handle_ble(service_info(ADV + scan_rsp(26)), BluetoothChange.ADVERTISEMENT)
    state = coordinator.state
    assert state.model_family == MODEL_A24
    assert state.kill_displayed == 26
    assert state.kill_alert is False
    assert state.serial_number == "E33BC98F"
    assert state.battery_volts == 3.06
    assert state.battery_status == "full"

    coordinator._async_handle_ble(service_info(ADV + scan_rsp(27, 1)), BluetoothChange.ADVERTISEMENT)
    assert state.kill_displayed == 27
    assert state.kill_alert is True
    assert state.unread_kills == 1

    await coordinator.async_clear_kill_alert()
    assert state.kill_alert is False


async def test_ibeacon_alone_identifies_a24(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    coordinator._async_handle_ble(service_info(ADV), BluetoothChange.ADVERTISEMENT)
    assert coordinator.state.model_family == MODEL_A24
    assert coordinator.state.serial_number == "E33BC98F"
    assert coordinator.state.kill_displayed is None
    assert coordinator.state.last_seen is not None


async def test_low8_kill_count_wraps(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    coordinator.state.kill_displayed = 255
    assert coordinator._expand_low8(1) == 257
    assert coordinator._expand_low8(255) == 255
    coordinator.state.kill_displayed = 300
    assert coordinator._expand_low8(300 & 0xFF) == 300


async def test_sync_policy(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    coordinator.enable_gatt = True
    now = dt_util.utcnow()
    state = coordinator.state

    assert coordinator._should_sync_a24(now)  # never synced

    state.last_sync = now - timedelta(minutes=5)
    assert not coordinator._should_sync_a24(now)  # inside 15 min interval
    state.sync_requested = True
    assert coordinator._should_sync_a24(now)  # unread kills / Poll Now

    coordinator._last_disconnect = now - timedelta(seconds=3)
    assert not coordinator._should_sync_a24(now)  # app's 6 s reconnect guard
    coordinator._last_disconnect = None

    state.consecutive_sync_failures = 1
    state.last_sync_attempt = now - timedelta(seconds=30)
    assert not coordinator._should_sync_a24(now)  # failed-sync backoff
    state.last_sync_attempt = now - timedelta(seconds=61)
    assert coordinator._should_sync_a24(now)


async def test_state_persists_across_restart(hass: HomeAssistant, hass_storage) -> None:
    coordinator = make_coordinator(hass)
    coordinator._async_handle_ble(service_info(ADV + scan_rsp(26)), BluetoothChange.ADVERTISEMENT)
    coordinator.state.kill_alert = True
    coordinator.state.last_sync = dt_util.utcnow()
    coordinator._async_save()
    await coordinator._store._async_handle_write_data()

    restored = make_coordinator(hass)
    await restored.async_load()
    assert restored.state.model_family == MODEL_A24
    assert restored.state.kill_displayed == 26
    assert restored.state.kill_alert is True
    assert restored.state.battery_volts == 3.06
    assert restored.state.last_sync == coordinator.state.last_sync


async def test_setup_cleans_name_and_drops_stale_entities(
    hass: HomeAssistant, enable_bluetooth, hass_storage
) -> None:
    hass_storage[f"{DOMAIN}.{ADDRESS.replace(':', '').lower()}"] = {
        "version": 1,
        "key": f"{DOMAIN}.{ADDRESS.replace(':', '').lower()}",
        "data": {"model_family": "a24", "kill_displayed": 26},
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        data={"address": ADDRESS, "name": "GN\x00\x00"},
        title="Goodnature Trap 1",
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "button", DOMAIN, f"{ADDRESS}_test_fire", config_entry=entry
    )
    stale_sensor = registry.async_get_or_create(
        "sensor", DOMAIN, f"{ADDRESS}_a24_battery_raw", config_entry=entry
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.data["name"] == "Goodnature Trap 1"
    assert registry.async_get(stale.entity_id) is None
    assert registry.async_get(stale_sensor.entity_id) is None
    unique_ids = {
        e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert f"{ADDRESS}_kill_alert" in unique_ids
    assert f"{ADDRESS}_a24_battery_voltage" in unique_ids
    volts = registry.async_get(
        registry.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_a24_battery_voltage")
    )
    assert volts.options["sensor"]["suggested_display_precision"] == 2
    assert f"{ADDRESS}_charge_state" not in unique_ids  # C20-only
    strikes = hass.states.get(
        registry.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_estimated_activations")
    )
    assert strikes.state == "26"

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reloads_once_after_model_identified_and_trap_quiet(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    coordinator.model_at_setup = "unknown"
    coordinator._async_handle_ble(service_info(ADV + scan_rsp(26)), BluetoothChange.ADVERTISEMENT)
    assert coordinator.state.model_family == MODEL_A24

    coordinator._unsubscribe_ble = lambda: None  # no Bluetooth stack in this test
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await coordinator._async_update_data()  # trap still awake: wait
        reload.assert_not_called()

        coordinator.state.last_packet = dt_util.utcnow() - timedelta(seconds=91)
        await coordinator._async_update_data()
        reload.assert_called_once_with(coordinator.config_entry.entry_id)

        await coordinator._async_update_data()
        reload.assert_called_once()  # only once
    coordinator.async_shutdown()


async def test_no_reload_when_model_known_at_setup(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    coordinator.state.model_family = MODEL_A24
    coordinator.model_at_setup = MODEL_A24
    coordinator._unsubscribe_ble = lambda: None  # no Bluetooth stack in this test
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await coordinator._async_update_data()
        reload.assert_not_called()
    coordinator.async_shutdown()


async def test_lower_advert_kill_byte_is_ignored(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    coordinator.state.kill_displayed = 26
    assert coordinator._expand_low8(25) is None
    assert coordinator._expand_low8(0) is None
    assert coordinator._expand_low8(27) == 27
    coordinator._async_handle_ble(service_info(ADV + scan_rsp(25)), BluetoothChange.ADVERTISEMENT)
    assert coordinator.state.kill_displayed == 26
    assert coordinator.state.kill_alert is False


async def test_gatt_count_can_go_down(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    coordinator._apply_kill_count(30, source="gatt")
    coordinator._apply_kill_count(26, source="gatt")
    assert coordinator.state.kill_displayed == 26
    assert coordinator.state.kill_alert is False


async def test_advert_only_baseline_takes_full_gatt_count_silently(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    # A trap at 282 kills first heard by advert: low 8 bits = 26.
    coordinator._async_handle_ble(service_info(ADV + scan_rsp(26)), BluetoothChange.ADVERTISEMENT)
    assert coordinator.state.kill_baseline_from_advert is True
    coordinator._apply_kill_count(282, source="gatt")
    assert coordinator.state.kill_displayed == 282
    assert coordinator.state.kill_alert is False
    coordinator._apply_kill_count(283, source="gatt")
    assert coordinator.state.kill_alert is True


async def test_unread_kills_alert_on_first_contact(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    coordinator._async_handle_ble(service_info(ADV + scan_rsp(26, 2)), BluetoothChange.ADVERTISEMENT)
    assert coordinator.state.kill_alert is True
    assert coordinator.state.sync_requested is True


async def test_unacknowledged_kills_in_gatt_raise_alert(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    coordinator._apply_a24_session(
        {"kill_displayed": 26, "kill_read_before": 24, "kill_read": 26, "acknowledged": True}
    )
    assert coordinator.state.kill_alert is True


async def test_failed_sync_backoff_doubles_to_30_minutes(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass)
    coordinator.enable_gatt = True
    now = dt_util.utcnow()
    state = coordinator.state
    state.last_sync_attempt = now
    for failures, wait in ((1, 60), (2, 120), (3, 240), (6, 1800), (10, 1800)):
        state.consecutive_sync_failures = failures
        assert not coordinator._should_sync_a24(now + timedelta(seconds=wait - 1))
        assert coordinator._should_sync_a24(now + timedelta(seconds=wait + 1))


async def test_implausible_kill_record_is_ignored(hass: HomeAssistant) -> None:
    from custom_components.goodnature_ble.protocol import parse_d30d

    coordinator = make_coordinator(hass)
    good = parse_d30d(bytes.fromhex("c58fc93be30b00f4c6011a00"))
    unset_clock = parse_d30d(bytes.fromhex("c58fc93be30b" "10000000" "1a00"))  # 1970
    coordinator._apply_a24_session({"kill_displayed": 27, "record": good})
    assert coordinator.state.last_strike_at is None  # not the newest record
    coordinator._apply_a24_session({"kill_displayed": 26, "record": unset_clock})
    assert coordinator.state.last_strike_at is None
    coordinator._apply_a24_session({"kill_displayed": 26, "record": good})
    assert coordinator.state.last_strike_id == 26
    assert coordinator.state.strike_temperature_c == 11
