"""Entity helpers for Goodnature BLE."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODEL_A24, MODEL_C20
from .coordinator import GoodnatureCoordinator

A24_ONLY = frozenset({MODEL_A24})
C20_ONLY = frozenset({MODEL_C20})


class GoodnatureEntity(CoordinatorEntity[GoodnatureCoordinator]):
    """Base entity for Goodnature BLE."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Return shared device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, self.coordinator.address)},
            manufacturer="Goodnature",
            model=self.coordinator.model_name,
            name=self.coordinator.name,
            serial_number=self.coordinator.state.serial_number,
            sw_version=self.coordinator.state.firmware_version,
        )


def model_matches(models: frozenset[str] | None, model: str) -> bool:
    """Return True if an entity for `models` belongs on a trap of `model`.

    While the model is unknown, every entity is created.
    """
    if models is None or model not in (MODEL_A24, MODEL_C20):
        return True
    return model in models


def remove_stale_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    platform_domain: str,
    keep_unique_ids: Iterable[str],
) -> None:
    """Remove this entry's registry entries on a platform that are no longer created."""
    keep = set(keep_unique_ids)
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.domain == platform_domain and entity.unique_id not in keep:
            registry.async_remove(entity.entity_id)
