"""Binary sensors for Goodnature BLE."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GoodnatureRuntimeData
from .coordinator import TrapState
from .entity import (
    A24_ONLY,
    C20_ONLY,
    GoodnatureEntity,
    model_matches,
    remove_stale_entities,
)


def _c20_enum(attr: str, on: tuple[str, ...], unknown: tuple[str, ...] = ("NO_DATA",)):
    def _fn(state: TrapState) -> bool | None:
        raw = getattr(state, attr)
        if raw is None or raw in unknown:
            return None
        return raw in on

    return _fn


@dataclass(frozen=True, slots=True)
class GoodnatureBinarySpec:
    """Describes a coordinator-backed binary sensor."""

    key: str
    name: str
    device_class: BinarySensorDeviceClass | None
    is_on_fn: Callable[[TrapState], bool | None]
    raw_attr: str | None = None
    entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC
    models: frozenset[str] | None = None
    icon: str | None = None


SPECS: tuple[GoodnatureBinarySpec, ...] = (
    # A24
    GoodnatureBinarySpec(
        key="kill_alert",
        name="Kill Alert",
        device_class=None,
        is_on_fn=lambda s: s.kill_alert,
        entity_category=None,
        models=A24_ONLY,
        icon="mdi:rodent",
    ),
    GoodnatureBinarySpec(
        key="a24_low_battery",
        name="Battery Low",
        device_class=BinarySensorDeviceClass.BATTERY,
        is_on_fn=lambda s: s.low_battery,
        models=A24_ONLY,
    ),
    GoodnatureBinarySpec(
        key="a24_new_error",
        name="Trap Error Flag",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda s: s.new_error,
        models=A24_ONLY,
    ),
    # C20
    GoodnatureBinarySpec(
        key="charge_state",
        name="Charge",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        is_on_fn=_c20_enum("c20_charge_state", ("CHARGING",)),
        raw_attr="c20_charge_state",
        models=C20_ONLY,
    ),
    GoodnatureBinarySpec(
        key="battery_state_problem",
        name="Battery",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=_c20_enum("c20_battery_state", ("CRITICAL", "LOW", "NOT_CONNECTED")),
        raw_attr="c20_battery_state",
        models=C20_ONLY,
    ),
    GoodnatureBinarySpec(
        key="kill_state",
        name="Trap Occupancy",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        is_on_fn=_c20_enum("c20_kill_state", ("DETECTED",)),
        raw_attr="c20_kill_state",
        entity_category=None,
        models=C20_ONLY,
    ),
    GoodnatureBinarySpec(
        key="usb_state",
        name="USB",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        is_on_fn=_c20_enum("c20_usb_state", ("CONNECTED",)),
        raw_attr="c20_usb_state",
        models=C20_ONLY,
    ),
    GoodnatureBinarySpec(
        key="tray_state",
        name="Tray",
        device_class=BinarySensorDeviceClass.OPENING,
        is_on_fn=_c20_enum("c20_tray_state", ("OPEN",), ("NO_DATA", "UNKNOWN")),
        raw_attr="c20_tray_state",
        models=C20_ONLY,
    ),
    GoodnatureBinarySpec(
        key="discoverable",
        name="Discoverable",
        device_class=None,
        is_on_fn=lambda s: s.c20_discoverable,
        raw_attr="c20_discoverable",
        models=C20_ONLY,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Goodnature binary sensors."""
    runtime: GoodnatureRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator
    model = coordinator.state.model_family

    entities = [
        GoodnatureBinarySensor(coordinator, spec)
        for spec in SPECS
        if model_matches(spec.models, model)
    ]
    remove_stale_entities(
        hass, entry, "binary_sensor", (e.unique_id for e in entities)
    )
    async_add_entities(entities)


class GoodnatureBinarySensor(GoodnatureEntity, BinarySensorEntity):
    """Binary sensor backed by coordinator trap state."""

    def __init__(self, coordinator, spec: GoodnatureBinarySpec) -> None:
        super().__init__(coordinator)
        self._spec = spec
        self._attr_unique_id = f"{coordinator.address}_{spec.key}"
        self._attr_name = spec.name
        self._attr_device_class = spec.device_class
        self._attr_entity_category = spec.entity_category
        if spec.icon:
            self._attr_icon = spec.icon

    @property
    def is_on(self) -> bool | None:
        return self._spec.is_on_fn(self.coordinator.state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._spec.raw_attr is None:
            return None
        return {"raw_state": getattr(self.coordinator.state, self._spec.raw_attr)}
