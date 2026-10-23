"""A24 GATT session order, checked against the Goodnature app's sync.

The app (per kedube/ha-goodnature-gateway docs/ble-protocol.md, "How the app
syncs an A24"): read D20D ...; for each new record write n to D20D, read D30D,
write n to D60D; then write F1AF, write 0x02 to DE13, and disconnect. If
anything fails it skips F1AF and 0x02.
"""

from datetime import UTC, datetime

import pytest

from custom_components.goodnature_ble.coordinator import A24Session
from custom_components.goodnature_ble.protocol import UUIDS

SHORT = {v: k for k, v in UUIDS.items()}
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
RECORD_26 = bytes.fromhex("82" "8fc93be3" "12" "f760c701" "1a00")


class FakeClient:
    """Minimal stand-in for BleakClient."""

    def __init__(self, values: dict[str, bytes | None], fail_writes=()) -> None:
        self.values = dict(values)
        self.fail_writes = set(fail_writes)
        self.ops: list[tuple[str, str, bytes | None]] = []

    async def read_gatt_char(self, uuid: str) -> bytearray:
        short = SHORT[uuid]
        self.ops.append(("read", short, None))
        value = self.values.get(short)
        if value is None:
            raise RuntimeError(f"read {short} failed")
        return bytearray(value)

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool) -> None:
        short = SHORT[uuid]
        self.ops.append(("write", short, bytes(data)))
        if short in self.fail_writes:
            raise RuntimeError(f"write {short} failed")
        if short == "D20D":
            # Selecting a record makes D30D return it.
            self.values["D30D"] = RECORD_26 if data == b"\x1a\x00" else bytes(12)

    def writes(self) -> list[tuple[str, bytes]]:
        return [(s, v) for op, s, v in self.ops if op == "write"]


def base_values(**overrides) -> dict[str, bytes | None]:
    values = {
        "D20D": b"\x1a\x00",  # 26 kills
        "DE12": bytes.fromhex("8fc93be3"),
        "DE15": bytes.fromhex("010300"),
        "D60D": b"\x19\x00",  # 25 read
        "FAD1": b"\xd9",
        "FAD2": b"\x01",
        "D30D": bytes(12),
    }
    values.update(overrides)
    return values


async def test_sync_reads_newest_record_then_acknowledges_last() -> None:
    client = FakeClient(base_values())
    result = await A24Session(client).run(write_time=True, now=NOW)

    minutes = int(NOW.timestamp() // 60).to_bytes(4, "little")
    assert client.writes() == [
        ("D20D", b"\x1a\x00"),
        ("D60D", b"\x1a\x00"),
        ("F1AF", minutes),
        ("DE13", b"\x02"),
    ]
    # Every read happens before the first write except D30D, which follows
    # the D20D selection.
    first_write = next(i for i, op in enumerate(client.ops) if op[0] == "write")
    reads_after = [s for op, s, _ in client.ops[first_write:] if op == "read"]
    assert reads_after == ["D30D"]
    assert client.ops[-1] == ("write", "DE13", b"\x02")

    assert result["acknowledged"] is True
    assert result["kill_displayed"] == 26
    assert result["kill_read"] == 26
    assert result["serial"] == "E33BC98F"
    assert result["firmware"] == "1.3.0"
    assert result["battery_raw"] == 0xD9
    assert result["battery_ir"] == 1
    assert result["record"].record_number == 26
    assert result["record"].temperature_c == 18


async def test_trap_hanging_up_on_ack_is_not_a_failure() -> None:
    client = FakeClient(base_values(), fail_writes={"DE13"})
    session = A24Session(client)
    result = await session.run(write_time=True, now=NOW)
    assert result["acknowledged"] is True
    assert result["ack_confirmed"] is False
    assert client.ops[-1] == ("write", "DE13", b"\x02")
    assert session.steps[-1].startswith("write DE13 02 failed")


async def test_no_acknowledgement_if_record_read_fails() -> None:
    client = FakeClient(base_values())
    client.write_gatt_char_orig = client.write_gatt_char

    async def write_without_record(uuid, data, response):
        await client.write_gatt_char_orig(uuid, data, response)
        client.values["D30D"] = None  # record read will fail

    client.write_gatt_char = write_without_record
    with pytest.raises(RuntimeError, match="D30D"):
        await A24Session(client).run(write_time=True, now=NOW)
    assert [s for s, _ in client.writes()] == ["D20D"]


async def test_no_writes_if_kill_count_unreadable() -> None:
    client = FakeClient(base_values(D20D=None))
    with pytest.raises(RuntimeError, match="D20D"):
        await A24Session(client).run(write_time=True, now=NOW)
    assert client.writes() == []


async def test_already_read_record_is_not_rewritten() -> None:
    client = FakeClient(base_values(D60D=b"\x1a\x00"))
    await A24Session(client).run(write_time=False, now=NOW)
    assert client.writes() == [("D20D", b"\x1a\x00"), ("DE13", b"\x02")]


async def test_no_kills_skips_record_selection() -> None:
    client = FakeClient(base_values(D20D=b"\x00\x00", D60D=b"\x00\x00"))
    result = await A24Session(client).run(write_time=True, now=NOW)
    assert [s for s, _ in client.writes()] == ["F1AF", "DE13"]
    assert "record" not in result


async def test_never_writes_unsafe_control_values() -> None:
    client = FakeClient(base_values())
    await A24Session(client).run(write_time=True, now=NOW)
    control_writes = [v for s, v in client.writes() if s == "DE13"]
    assert control_writes == [b"\x02"]
