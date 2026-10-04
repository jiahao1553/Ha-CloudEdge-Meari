"""Binary sensor platform for CloudEdge / Meari — motion, awake & online state."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import CloudEdgeMeariCoordinator
from .entity import CloudEdgeMeariEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up CloudEdge / Meari binary sensors from a config entry."""
    coord: CloudEdgeMeariCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        CloudEdgeMeariMotionSensor(coord, entry),
        CloudEdgeMeariOnlineSensor(coord, entry),
    ]
    if coord.is_battery_camera:
        entities.append(CloudEdgeMeariAwakeSensor(coord, entry))
        entities.append(CloudEdgeMeariChargingSensor(coord, entry))
    async_add_entities(entities)


class CloudEdgeMeariMotionSensor(CloudEdgeMeariEntity, BinarySensorEntity):
    """Binary sensor for motion detection."""

    _attr_name = "Motion"
    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_motion"
        self._unsub_motion: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_motion = self._coordinator.register_motion_callback(
            self._handle_update
        )

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        if self._unsub_motion:
            self._unsub_motion()

    @property
    def is_on(self) -> bool:
        return self._coordinator.motion_detected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        if self._coordinator.motion_type:
            attrs["motion_type"] = self._coordinator.motion_type
        return attrs


class CloudEdgeMeariAwakeSensor(CloudEdgeMeariEntity, BinarySensorEntity):
    """Binary sensor for camera awake state."""

    _attr_name = "Camera Awake"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:eye"

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_awake"

    @property
    def is_on(self) -> bool:
        return self._coordinator.camera_awake


class CloudEdgeMeariChargingSensor(CloudEdgeMeariEntity, BinarySensorEntity):
    """Binary sensor for camera charging state."""

    _attr_name = "Charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_charging"

    @property
    def is_on(self) -> bool | None:
        if self._coordinator.battery_percent is None:
            return None
        return self._coordinator.battery_charging


class CloudEdgeMeariOnlineSensor(CloudEdgeMeariEntity, BinarySensorEntity):
    """Whether the camera is reachable (cloud presence + live video evidence).

    On for ``streaming`` / ``online`` / ``dormant``; off for ``offline`` /
    ``unreachable``; unknown until the first cloud poll completes. It stays
    available while the integration itself is struggling, so an "offline"
    automation still fires instead of the entity going unavailable.
    """

    _attr_name = "Online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_online"

    @property
    def available(self) -> bool:
        return (
            self._coordinator.connection_status != "unknown"
            or self._coordinator.available
        )

    @property
    def is_on(self) -> bool | None:
        return self._coordinator.camera_online

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last_seen = self._coordinator.last_seen
        return {
            "connection_status": self._coordinator.connection_status,
            "cloud_status": self._coordinator.cloud_status,
            "last_seen": (
                datetime.fromtimestamp(last_seen, tz=timezone.utc).isoformat()
                if last_seen
                else None
            ),
        }
