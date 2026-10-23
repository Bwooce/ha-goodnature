"""Buttons for Goodnature BLE."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GoodnatureRuntimeData
from .entity import C20_ONLY, GoodnatureEntity, model_matches, remove_stale_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Goodnature button entities."""
    runtime: GoodnatureRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator
    model = coordinator.state.model_family

    entities = [
        GoodnatureActionButton(
            coordinator,
            key="poll_now",
            name="Poll Now",
            icon="mdi:refresh",
            action=coordinator.async_manual_poll,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        GoodnatureActionButton(
            coordinator,
            key="clear_kill",
            name="Clear Kill Alert",
            icon="mdi:notification-clear-all",
            action=coordinator.async_clear_kill_alert,
            entity_category=None,
        ),
    ]
    # The A24 has no BLE test fire (a test fire is a poke on the trigger).
    if model_matches(C20_ONLY, model):
        entities.append(
            GoodnatureActionButton(
                coordinator,
                key="test_fire",
                name="Test Fire",
                icon="mdi:flash",
                action=coordinator.async_test_fire,
            )
        )

    remove_stale_entities(hass, entry, "button", (e.unique_id for e in entities))
    async_add_entities(entities)


class GoodnatureActionButton(GoodnatureEntity, ButtonEntity):
    """A button that executes a coordinator action."""

    def __init__(
        self,
        coordinator,
        *,
        key: str,
        name: str,
        icon: str,
        action: Callable[[], Awaitable[bool]],
        entity_category: EntityCategory | None = EntityCategory.CONFIG,
    ) -> None:
        super().__init__(coordinator)
        self._action = action
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_entity_category = entity_category

    async def async_press(self) -> None:
        """Handle the button press."""
        await self._action()
