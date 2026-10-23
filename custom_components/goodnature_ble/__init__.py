"""The Goodnature BLE integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_NAME, DEFAULT_NAME, PLATFORMS
from .coordinator import GoodnatureCoordinator
from .protocol import clean_name


@dataclass(slots=True)
class GoodnatureRuntimeData:
    """Runtime data for a config entry."""

    coordinator: GoodnatureCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Goodnature BLE from a config entry."""
    # Older versions stored the advertised name with its NUL padding ("GN\0\0").
    # Fall back to the entry title when only the bare advertised name is left.
    stored_name = entry.data.get(CONF_NAME)
    if stored_name is not None and stored_name != clean_name(stored_name):
        cleaned = clean_name(stored_name)
        if cleaned in (None, "GN"):
            cleaned = clean_name(entry.title) or DEFAULT_NAME
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_NAME: cleaned}
        )

    coordinator = GoodnatureCoordinator(hass, entry)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = GoodnatureRuntimeData(
        coordinator=coordinator,
    )
    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform(platform) for platform in PLATFORMS]
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, [Platform(platform) for platform in PLATFORMS]
    )
    if unloaded and entry.runtime_data:
        entry.runtime_data.coordinator.async_shutdown()
    return unloaded
