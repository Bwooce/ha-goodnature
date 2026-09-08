"""Goodnature BLE protocol helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.util import dt as dt_util

BASE_SUFFIX = "-1212-efde-1523-785fef13d123"


def gn_uuid(short_id: str) -> str:
    """Build a full Goodnature UUID from a short identifier."""
    return f"0000{short_id.lower()}{BASE_SUFFIX}"


# Grouped short IDs based on observed device behavior.
SHORT_IDS_DEVICE = (
    "DE11",
    "DE12",
    "DE13",
    "DE14",
    "DE15",
    "DE16",
    "DE18",
)
SHORT_IDS_BATTERY_TELEMETRY = (
    "FADE",
    "FAD1",
    "FAD2",
    "FAD3",
)
SHORT_IDS_TIME = (
    "F1AE",
    "F1AF",
)
SHORT_IDS_KILL = (
    "D00D",
    "D20D",
    "D30D",
    "D50D",
    "D60D",
)
SHORT_IDS_EVENT = (
    "D2ED",
    "DEED",
    "D3ED",
)
SHORT_IDS_UNKNOWN = (
    "E010",
    "E771",
    "E772",
    "E773",
)

# Canonical lookup map for future parser expansion.
UUIDS: dict[str, str] = {
    short_id: gn_uuid(short_id)
    for short_id in (
        *SHORT_IDS_DEVICE,
        *SHORT_IDS_BATTERY_TELEMETRY,
        *SHORT_IDS_TIME,
        *SHORT_IDS_KILL,
        *SHORT_IDS_EVENT,
        *SHORT_IDS_UNKNOWN,
    )
}

# Additional advertised service UUIDs seen in observed client/device behavior.
LEGACY_DISCOVERY_UUIDS = {
    "00001234-0000-1000-8000-00805f9b34fb",
    "0000600d-0000-1000-8000-00805f9b34fb",
}

GOODNATURE_DISCOVERY_UUIDS = {
    UUIDS["D00D"],
    UUIDS["DE11"],
    UUIDS["FADE"],
    UUIDS["E010"],
    *LEGACY_DISCOVERY_UUIDS,
}


@dataclass(frozen=True, slots=True)
class C20Advertisement:
    """Decoded Goodnature C20 manufacturer data."""

    serial_number: str
    device_type: int
    activated: bool
    strikes_available: bool
    low_battery: bool
    charging: bool
    usb_connected: bool
    discoverable: bool
    strike_count: int
    battery_percent: int


def parse_c20_advertisement(value: bytes | None) -> C20Advertisement | None:
    """Parse the 9-byte manufacturer payload used by the C20 app scanner."""
    if value is None or len(value) != 9:
        return None

    flags = value[5]
    return C20Advertisement(
        serial_number=f"{int.from_bytes(value[0:4], 'little'):08X}",
        device_type=value[4],
        activated=bool(flags & 0x01),
        strikes_available=bool(flags & 0x02),
        low_battery=bool(flags & 0x08),
        charging=bool(flags & 0x10),
        usb_connected=bool(flags & 0x20),
        discoverable=bool(flags & 0x40),
        strike_count=int.from_bytes(value[6:8], "little"),
        battery_percent=value[8],
    )


def decode_text(value: bytes | None) -> str | None:
    """Decode a characteristic value to text where possible."""
    if not value:
        return None

    stripped = value.rstrip(b"\x00")
    if not stripped:
        return ""

    try:
        text = stripped.decode("utf-8").strip()
        if text:
            return text
    except UnicodeDecodeError:
        pass

    return stripped.hex()


def parse_u16_le(value: bytes | None) -> int | None:
    """Parse first 2 bytes as little-endian uint16."""
    if not value or len(value) < 2:
        return None
    return int.from_bytes(value[:2], byteorder="little", signed=False)


def parse_u8(value: bytes | None) -> int | None:
    """Parse first byte as uint8."""
    if not value:
        return None
    return value[0]


def parse_d30d(value: bytes | None) -> tuple[str | None, int | None, datetime | None, int | None]:
    """Parse A24 strike payload characteristic D30D.

    Returns: raw_hex, strike_id, strike_at, strike_flags
    """
    if not value:
        return None, None, None, None

    data = value
    # Some tools report D30D as an ASCII hex string.
    if all(chr(b) in "0123456789abcdefABCDEF" for b in value) and len(value) % 2 == 0:
        try:
            data = bytes.fromhex(value.decode("ascii"))
        except ValueError:
            data = value

    raw_hex = data.hex()
    if len(data) < 12:
        return raw_hex, None, None, None

    strike_flags = data[5]
    minutes_since_epoch = int.from_bytes(data[6:10], byteorder="little", signed=False)
    strike_id = int.from_bytes(data[10:12], byteorder="little", signed=False)
    strike_at = dt_util.utc_from_timestamp(minutes_since_epoch * 60)

    return raw_hex, strike_id, strike_at, strike_flags
