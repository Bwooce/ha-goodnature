"""Goodnature BLE protocol helpers.

Pure functions only (no Home Assistant imports) so they can be unit tested.

A24 layouts follow the protocol notes in kedube/ha-goodnature-gateway
(docs/ble-protocol.md), which were derived from the Goodnature Android app
and captures from real traps. The iBeacon serial mapping was observed on an
A24 Chirp in this fork's testing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

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

# A24 control values written to DE13.
A24_CONTROL_SYNC_DONE = 0x02

# A24 advertisement flag bits (low byte of slot 2).
A24_FLAG_STRIKES_AVAILABLE = 0x01
A24_FLAG_ACTIVATED = 0x02
A24_FLAG_MOVING = 0x04
A24_FLAG_LOW_BATTERY = 0x08
A24_FLAG_OUTPUT_POWER = 0x10
A24_FLAG_NEW_EVENT = 0x20
A24_FLAG_NEW_ERROR = 0x40

A24_MARKER = 0x1234

APPLE_COMPANY_ID = 0x004C
# iBeacon proximity UUID broadcast by an A24 Chirp (observed), whose
# major/minor carry the trap's serial bytes.
GOODNATURE_IBEACON_UUID = bytes.fromhex("b0b0eee7b9b04bc5b5e4f5cb610eb700")

AD_TYPE_UUID16_INCOMPLETE = 0x02
AD_TYPE_UUID16_COMPLETE = 0x03
AD_TYPE_LOCAL_NAME_SHORT = 0x08
AD_TYPE_LOCAL_NAME_COMPLETE = 0x09
AD_TYPE_MANUFACTURER = 0xFF


def clean_name(value: str | None) -> str | None:
    """Strip NUL padding and whitespace from an advertised name."""
    if value is None:
        return None
    cleaned = value.replace("\x00", "").strip()
    return cleaned or None


def decode_text(value: bytes | None) -> str | None:
    """Decode a characteristic value to text where possible."""
    if not value:
        return None

    stripped = value.rstrip(b"\x00")
    if not stripped:
        return ""

    try:
        text = stripped.decode("utf-8").strip()
        if text and text.isprintable():
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


def serial_from_bytes(value: bytes | None) -> str | None:
    """Format the four A24 serial bytes as the app shows them (reversed hex)."""
    if not value or len(value) < 4:
        return None
    return value[:4][::-1].hex().upper()


def firmware_from_bytes(value: bytes | None) -> str | None:
    """Format DE15 as dotted decimal (01 03 00 -> 1.3.0)."""
    if not value:
        return None
    return ".".join(str(b) for b in value)


def a24_battery_volts(raw: int | None) -> float | None:
    """Convert the A24 battery byte to volts (V = raw / 255 * 3.6)."""
    if raw is None or raw == 0:
        return None
    return round(raw / 255 * 3.6, 2)


def a24_battery_status(internal_resistance: int | None) -> str | None:
    """Rate the battery from its internal resistance byte, as the app does."""
    if internal_resistance is None:
        return None
    if internal_resistance < 56:
        return "full"
    if internal_resistance < 200:
        return "medium"
    if internal_resistance < 230:
        return "low"
    return "critical"


def iter_ad_structures(raw: bytes | None) -> list[tuple[int, bytes]]:
    """Split raw advertisement (+ scan response) bytes into AD structures."""
    structures: list[tuple[int, bytes]] = []
    if not raw:
        return structures
    index = 0
    while index < len(raw):
        length = raw[index]
        if length == 0:
            index += 1
            continue
        end = index + 1 + length
        if end > len(raw):
            break
        structures.append((raw[index + 1], bytes(raw[index + 2 : end])))
        index = end
    return structures


@dataclass(frozen=True, slots=True)
class A24Advertisement:
    """Decoded A24 status carried in the 16-bit UUID list of its advertisement."""

    serial_number: str
    flags: int
    device_type: int
    battery_raw: int
    battery_internal_resistance: int
    kill_count_low8: int
    unread_kills: int

    @property
    def battery_volts(self) -> float | None:
        return a24_battery_volts(self.battery_raw)

    @property
    def strikes_available(self) -> bool:
        return bool(self.flags & A24_FLAG_STRIKES_AVAILABLE)

    @property
    def activated(self) -> bool:
        return bool(self.flags & A24_FLAG_ACTIVATED)

    @property
    def moving(self) -> bool:
        return bool(self.flags & A24_FLAG_MOVING)

    @property
    def low_battery(self) -> bool:
        return bool(self.flags & A24_FLAG_LOW_BATTERY)

    @property
    def new_event(self) -> bool:
        return bool(self.flags & A24_FLAG_NEW_EVENT)

    @property
    def new_error(self) -> bool:
        return bool(self.flags & A24_FLAG_NEW_ERROR)


def parse_a24_slots(data: bytes) -> A24Advertisement | None:
    """Decode the six little-endian 16-bit 'UUID' slots of an A24 advertisement."""
    if len(data) != 12:
        return None
    if int.from_bytes(data[6:8], "little") != A24_MARKER:
        return None
    return A24Advertisement(
        serial_number=serial_from_bytes(data[0:4]) or "",
        flags=data[4],
        device_type=data[5],
        battery_raw=data[9],
        battery_internal_resistance=data[8],
        kill_count_low8=data[11],
        unread_kills=data[10],
    )


def parse_a24_advertisement(raw: bytes | None) -> A24Advertisement | None:
    """Find and decode the A24 status slots in raw advertisement bytes."""
    for ad_type, data in iter_ad_structures(raw):
        if ad_type in (AD_TYPE_UUID16_COMPLETE, AD_TYPE_UUID16_INCOMPLETE):
            parsed = parse_a24_slots(data)
            if parsed is not None:
                return parsed
    return None


def parse_goodnature_ibeacon(apple_payload: bytes | None) -> str | None:
    """Return the trap serial from a Goodnature iBeacon payload, else None."""
    if not apple_payload or len(apple_payload) < 22:
        return None
    if apple_payload[0] != 0x02 or apple_payload[1] != 0x15:
        return None
    if apple_payload[2:18] != GOODNATURE_IBEACON_UUID:
        return None
    return serial_from_bytes(apple_payload[18:22])


@dataclass(frozen=True, slots=True)
class A24KillRecord:
    """A kill record read from D30D."""

    raw_hex: str
    record_number: int
    strike_at: datetime | None
    temperature_c: int
    peak_level: int
    serial_number: str | None


def parse_d30d(value: bytes | None) -> A24KillRecord | None:
    """Parse an A24 kill record (D30D).

    Layout: [0] peak level, [1..4] trap serial bytes, [5] air temperature
    (signed, deg C), [6..9] minutes since the Unix epoch (u32 LE),
    [10..11] record number (u16 LE, 0 = no record).
    """
    if not value:
        return None

    data = bytes(value)
    # Some tools report D30D as an ASCII hex string.
    if all(chr(b) in "0123456789abcdefABCDEF" for b in data) and len(data) % 2 == 0:
        try:
            data = bytes.fromhex(data.decode("ascii"))
        except ValueError:
            pass

    if len(data) < 12:
        return None

    record_number = int.from_bytes(data[10:12], "little")
    if record_number == 0:
        return None
    minutes = int.from_bytes(data[6:10], "little")
    strike_at = datetime.fromtimestamp(minutes * 60, tz=UTC) if minutes else None
    return A24KillRecord(
        raw_hex=data.hex(),
        record_number=record_number,
        strike_at=strike_at,
        temperature_c=int.from_bytes(data[5:6], "little", signed=True),
        peak_level=data[0],
        serial_number=serial_from_bytes(data[1:5]),
    )


def encode_minutes_now(now: datetime) -> bytes:
    """Encode the F1AF time value (Unix minutes, u32 LE)."""
    return int(now.timestamp() // 60).to_bytes(4, "little", signed=False)


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
    # Battery state enumeration (4 = NORMAL), not a percentage (kedube notes,
    # observed on Mouse Trap firmware 0.3.730).
    battery_state_raw: int


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
        battery_state_raw=value[8],
    )
