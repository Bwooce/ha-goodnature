"""Constants for the Goodnature BLE integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "goodnature_ble"

PLATFORMS = ["binary_sensor", "sensor", "button"]

CONF_ADDRESS = "address"
CONF_NAME = "name"

DEFAULT_NAME = "Goodnature Trap"
DEFAULT_COOLDOWN_SECONDS = 23
DEFAULT_ACTIVE_WINDOW_SECONDS = 120
DEFAULT_MIN_PACKETS_FOR_ACTIVATION = 3
DEFAULT_BURST_GAP_SECONDS = 8
DEFAULT_ENABLE_GATT = True
DEFAULT_GATT_MIN_POLL_SECONDS = 120
DEFAULT_WRITE_TIME_ON_CONNECT = True

# A24 sync policy. The trap only accepts a connection while it is awake.
A24_MIN_SYNC_INTERVAL_SECONDS = 15 * 60
A24_FAILED_SYNC_RETRY_SECONDS = 60
A24_MAX_SYNC_BACKOFF_SECONDS = 30 * 60
A24_RECONNECT_GUARD_SECONDS = 6
A24_SYNC_WINDOW_SECONDS = 60

MODEL_UNKNOWN = "unknown"
MODEL_A24 = "a24"
MODEL_C20 = "c20"

STORAGE_VERSION = 1

COORDINATOR_UPDATE_INTERVAL = timedelta(seconds=30)

ATTR_ESTIMATED_ACTIVATIONS = "estimated_activations"
