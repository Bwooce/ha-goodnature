"""Protocol parser tests.

Expected values come from sources independent of this code:
- "kedube" = worked examples in kedube/ha-goodnature-gateway docs/ble-protocol.md
  (captures from a real A24 Smart Cap, firmware 1.3.0, with their stated decodes).
- "live" = a capture from an A24 Chirp at D3:58:7F:15:99:0B on 2026-10-23, whose
  kill count (26) was confirmed independently by a GATT read of D20D.
"""

from datetime import UTC, datetime

from custom_components.goodnature_ble.protocol import (
    a24_battery_status,
    a24_battery_volts,
    clean_name,
    firmware_from_bytes,
    iter_ad_structures,
    parse_a24_advertisement,
    parse_a24_slots,
    parse_d30d,
    parse_goodnature_ibeacon,
    serial_from_bytes,
)

# kedube: one complete advertising payload (battery not yet measured, no strikes).
KEDUBE_ADV_FIRST = bytes.fromhex(
    "020106" "0d03442326972600341200000000" "0509474e0000"
)
# kedube: second packet's slots [2..5] = 26 00 34 12 FD D8 00 01, same serial.
KEDUBE_SLOTS_SECOND = bytes.fromhex("44232697" "26003412fdd80001")
# kedube: D30D example "82 44 23 26 97 12 F7 60 C7 01 01 00".
KEDUBE_D30D = bytes.fromhex("8244232697" "12" "f760c701" "0100")

# live: advertisement (iBeacon) + scan response ("GN" status slots), as relayed
# by an ESPHome Bluetooth proxy.
LIVE_RAW = bytes.fromhex(
    "0201061aff4c000215b0b0eee7b9b04bc5b5e4f5cb610eb7008fc93be336"
    "0d038fc93be33600341201d9001a0509474e0000"
)
LIVE_IBEACON = bytes.fromhex("0215b0b0eee7b9b04bc5b5e4f5cb610eb7008fc93be336")


def test_kedube_first_advertisement() -> None:
    adv = parse_a24_advertisement(KEDUBE_ADV_FIRST)
    assert adv is not None
    assert adv.serial_number == "97262344"
    assert adv.flags == 0x26
    assert adv.activated and adv.moving and adv.new_event
    assert not adv.strikes_available and not adv.low_battery and not adv.new_error
    # "0x0000 before the battery has been measured"
    assert adv.battery_raw == 0
    assert adv.battery_volts is None
    assert adv.kill_count_low8 == 0
    assert adv.unread_kills == 0


def test_kedube_second_packet_slots() -> None:
    adv = parse_a24_slots(KEDUBE_SLOTS_SECOND)
    assert adv is not None
    # "battery 3.05 V with internal resistance 253, one kill and none unread"
    assert adv.battery_volts == 3.05
    assert adv.battery_internal_resistance == 253
    assert adv.kill_count_low8 == 1
    assert adv.unread_kills == 0


def test_slots_need_marker_and_length() -> None:
    assert parse_a24_slots(bytes.fromhex("442326972600341200")) is None
    assert parse_a24_slots(bytes.fromhex("44232697260035120000" "0000")) is None


def test_kedube_d30d_record() -> None:
    record = parse_d30d(KEDUBE_D30D)
    assert record is not None
    assert record.record_number == 1
    assert record.strike_at == datetime(2026, 9, 28, 19, 3, tzinfo=UTC)
    assert record.temperature_c == 18
    assert record.peak_level == 0x82
    assert record.serial_number == "97262344"


def test_d30d_ascii_hex_and_empty() -> None:
    assert parse_d30d(KEDUBE_D30D.hex().encode()).record_number == 1
    assert parse_d30d(bytes(12)) is None
    assert parse_d30d(b"") is None
    assert parse_d30d(bytes.fromhex("8244232697")) is None


def test_d30d_negative_temperature() -> None:
    record = parse_d30d(bytes.fromhex("8244232697" "fb" "f760c701" "0200"))
    assert record.temperature_c == -5
    assert record.record_number == 2


def test_battery_formula_examples() -> None:
    # kedube: "216 -> 3.05 V, 227 -> 3.20 V"
    assert a24_battery_volts(216) == 3.05
    assert a24_battery_volts(227) == 3.2
    assert a24_battery_volts(0) is None


def test_battery_status_thresholds() -> None:
    # kedube (app rating): under 56 Full, under 200 Medium, under 230 Low, else Critical
    assert a24_battery_status(55) == "full"
    assert a24_battery_status(56) == "medium"
    assert a24_battery_status(199) == "medium"
    assert a24_battery_status(200) == "low"
    assert a24_battery_status(229) == "low"
    assert a24_battery_status(253) == "critical"
    assert a24_battery_status(None) is None


def test_identity_formatting() -> None:
    # kedube: DE12 "44 23 26 97 -> 97262344"; DE15 "01 03 00 -> 1.3.0"
    assert serial_from_bytes(bytes.fromhex("44232697")) == "97262344"
    assert firmware_from_bytes(bytes.fromhex("010300")) == "1.3.0"


def test_live_capture_kill_count() -> None:
    adv = parse_a24_advertisement(LIVE_RAW)
    assert adv is not None
    assert adv.kill_count_low8 == 26  # confirmed by GATT D20D = 26
    assert adv.unread_kills == 0
    # Derived with the same formula/layout (not independently confirmed):
    assert adv.serial_number == "E33BC98F"
    assert adv.battery_volts == 3.06


def test_live_ibeacon_serial_matches_slots() -> None:
    assert parse_goodnature_ibeacon(LIVE_IBEACON) == "E33BC98F"
    other_beacon = bytearray(LIVE_IBEACON)
    other_beacon[5] ^= 0xFF
    assert parse_goodnature_ibeacon(bytes(other_beacon)) is None
    assert parse_goodnature_ibeacon(None) is None


def test_iter_ad_structures_handles_padding_and_truncation() -> None:
    assert iter_ad_structures(b"") == []
    assert iter_ad_structures(bytes.fromhex("020106" "00" "0309474e")) == [
        (0x01, b"\x06"),
        (0x09, b"GN"),
    ]
    assert iter_ad_structures(bytes.fromhex("020106" "0509474e")) == [(0x01, b"\x06")]


def test_clean_name() -> None:
    assert clean_name("GN\x00\x00") == "GN"
    assert clean_name("\x00") is None
    assert clean_name(None) is None
