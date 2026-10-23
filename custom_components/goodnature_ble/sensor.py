"""Sensors for Goodnature BLE."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import GoodnatureRuntimeData
from .const import ATTR_ESTIMATED_ACTIVATIONS
from .coordinator import TrapState
from .entity import (
    A24_ONLY,
    C20_ONLY,
    GoodnatureEntity,
    model_matches,
    remove_stale_entities,
)

ACTIVATIONS_DESCRIPTION = SensorEntityDescription(
    key="estimated_activations",
    name="Strikes",
    icon="mdi:counter",
    state_class=SensorStateClass.TOTAL,
)

RSSI_DESCRIPTION = SensorEntityDescription(
    key="rssi",
    name="Signal Strength",
    native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    device_class=SensorDeviceClass.SIGNAL_STRENGTH,
    entity_category=EntityCategory.DIAGNOSTIC,
)


@dataclass(frozen=True, slots=True)
class GoodnatureSensorSpec:
    """Describes a coordinator-backed sensor."""

    key: str
    name: str
    value_fn: Callable[[TrapState], Any]
    icon: str | None = None
    device_class: SensorDeviceClass | None = None
    entity_category: EntityCategory | None = None
    native_unit_of_measurement: str | None = None
    enabled_by_default: bool = True
    models: frozenset[str] | None = None
    options: list[str] | None = None
    state_class: SensorStateClass | None = None


SENSOR_SPECS: tuple[GoodnatureSensorSpec, ...] = (
    # A24
    GoodnatureSensorSpec(
        key="a24_last_strike_at",
        name="Last Strike",
        value_fn=lambda s: s.last_strike_at,
        device_class=SensorDeviceClass.TIMESTAMP,
        models=A24_ONLY,
    ),
    GoodnatureSensorSpec(
        key="a24_strike_temperature",
        name="Last Strike Temperature",
        value_fn=lambda s: s.strike_temperature_c,
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_category=EntityCategory.DIAGNOSTIC,
        models=A24_ONLY,
    ),
    GoodnatureSensorSpec(
        key="a24_last_strike_id",
        name="Last Strike Record",
        value_fn=lambda s: s.last_strike_id,
        icon="mdi:identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        enabled_by_default=False,
        models=A24_ONLY,
    ),
    GoodnatureSensorSpec(
        key="a24_battery_voltage",
        name="Battery Voltage",
        value_fn=lambda s: s.battery_volts,
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        models=A24_ONLY,
    ),
    GoodnatureSensorSpec(
        key="a24_battery_status",
        name="Battery Status",
        value_fn=lambda s: s.battery_status,
        device_class=SensorDeviceClass.ENUM,
        options=["full", "medium", "low", "critical"],
        icon="mdi:battery-heart-variant",
        entity_category=EntityCategory.DIAGNOSTIC,
        models=A24_ONLY,
    ),
    GoodnatureSensorSpec(
        key="a24_battery_internal_resistance",
        name="Battery Internal Resistance",
        value_fn=lambda s: s.battery_internal_resistance,
        icon="mdi:omega",
        entity_category=EntityCategory.DIAGNOSTIC,
        enabled_by_default=False,
        models=A24_ONLY,
    ),
    GoodnatureSensorSpec(
        key="a24_unread_kills",
        name="Unread Kills",
        value_fn=lambda s: s.unread_kills,
        icon="mdi:email-alert-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        models=A24_ONLY,
    ),
    GoodnatureSensorSpec(
        key="last_seen",
        name="Last Seen",
        value_fn=lambda s: s.last_seen,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GoodnatureSensorSpec(
        key="a24_last_sync",
        name="Last Sync",
        value_fn=lambda s: s.last_sync,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        models=A24_ONLY,
    ),
    GoodnatureSensorSpec(
        key="last_sync_error",
        name="Last Sync Error",
        value_fn=lambda s: s.last_gatt_error,
        icon="mdi:alert-circle-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        enabled_by_default=False,
    ),
    # C20
    GoodnatureSensorSpec(
        key="c20_battery_percent",
        name="Battery",
        value_fn=lambda s: s.c20_battery_percent,
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        entity_category=EntityCategory.DIAGNOSTIC,
        models=C20_ONLY,
    ),
    GoodnatureSensorSpec(
        key="c20_device_type",
        name="Device Type",
        value_fn=lambda s: s.c20_device_type,
        icon="mdi:identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        enabled_by_default=False,
        models=C20_ONLY,
    ),
    GoodnatureSensorSpec(
        key="c20_last_kill_at",
        name="Last Kill At",
        value_fn=lambda s: s.c20_last_kill_at,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        models=C20_ONLY,
    ),
    GoodnatureSensorSpec(
        key="c20_device_state",
        name="Device",
        value_fn=lambda s: s.c20_device_state,
        entity_category=EntityCategory.DIAGNOSTIC,
        models=C20_ONLY,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Goodnature sensors."""
    runtime: GoodnatureRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator
    model = coordinator.state.model_family

    entities: list[SensorEntity] = [
        GoodnatureStrikesSensor(coordinator),
        GoodnatureRssiSensor(coordinator),
        *(
            GoodnatureStateSensor(coordinator, spec)
            for spec in SENSOR_SPECS
            if model_matches(spec.models, model)
        ),
    ]
    remove_stale_entities(hass, entry, "sensor", (e.unique_id for e in entities))
    async_add_entities(entities)


class GoodnatureStrikesSensor(GoodnatureEntity, RestoreEntity, SensorEntity):
    """Strike count, preferring trap-reported values over inferred bursts."""

    entity_description = ACTIVATIONS_DESCRIPTION

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        # Unique ID kept from earlier versions so history carries over.
        self._attr_unique_id = f"{coordinator.address}_estimated_activations"
        self._restored_base = 0

    async def async_added_to_hass(self) -> None:
        """Restore previous total after Home Assistant restart."""
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            try:
                self._restored_base = int(float(last_state.state))
            except (TypeError, ValueError):
                self._restored_base = 0

    @property
    def native_value(self) -> int:
        state = self.coordinator.state

        if state.c20_strike_count is not None:
            return state.c20_strike_count

        if state.kill_displayed is not None:
            return state.kill_displayed

        # Fallback for passive/BLE-only mode.
        return self._restored_base + state.estimated_activations

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self.coordinator.state
        source = (
            "c20_strike_count"
            if state.c20_strike_count is not None
            else "kill_displayed"
            if state.kill_displayed is not None
            else "inferred_ble_burst"
        )
        return {
            ATTR_ESTIMATED_ACTIVATIONS: self.native_value,
            "source": source,
        }


class GoodnatureRssiSensor(GoodnatureEntity, SensorEntity):
    """RSSI from the most recent packet."""

    entity_description = RSSI_DESCRIPTION
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_rssi"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.state.last_rssi


class GoodnatureStateSensor(GoodnatureEntity, SensorEntity):
    """Generic sensor backed by coordinator trap state."""

    def __init__(self, coordinator, spec: GoodnatureSensorSpec) -> None:
        super().__init__(coordinator)
        self._spec = spec
        self._attr_unique_id = f"{coordinator.address}_{spec.key}"
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._attr_device_class = spec.device_class
        self._attr_entity_category = spec.entity_category
        self._attr_native_unit_of_measurement = spec.native_unit_of_measurement
        self._attr_entity_registry_enabled_default = spec.enabled_by_default
        self._attr_state_class = spec.state_class
        if spec.options is not None:
            self._attr_options = spec.options

    @property
    def native_value(self) -> Any:
        return self._spec.value_fn(self.coordinator.state)
