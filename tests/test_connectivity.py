"""Offline tests for camera online/offline tracking."""

from __future__ import annotations

import asyncio
import importlib
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from debug_tools.bootstrap import _bootstrap_integration_modules

MODULES = _bootstrap_integration_modules()
COORDINATOR = MODULES["coordinator"].CloudEdgeMeariCoordinator
CONST = importlib.import_module("custom_components.cloudplus.const")
POLL = CONST.CONNECTIVITY_POLL_INTERVAL_S
LIVE = CONST.CONNECTIVITY_LIVE_EVIDENCE_S
UNREACHABLE = CONST.CONNECTIVITY_UNREACHABLE_AFTER_S


def coordinator(*, battery: bool = False, status: str = "online") -> COORDINATOR:
    """Build connectivity state only; no threads, no cloud traffic."""
    coord = COORDINATOR.__new__(COORDINATOR)
    coord._is_snap = battery
    coord._sn_num = "synthetic-camera"
    coord._device_name = "Synthetic camera"
    coord._device_category = "ipc"
    coord._has_lamp = False
    coord._available = True
    coord._last_video_time = 0.0
    coord._live_deadline = 0.0
    coord._stream_server = SimpleNamespace(client_count=0)
    coord._init_always_connected()
    coord._init_connectivity()
    coord._fire_update = Mock()
    coord._reauthenticate_api = Mock(return_value=True)
    coord._api = SimpleNamespace(
        openapi_server="https://openapi.example",
        get_device_status=Mock(return_value=status),
    )
    return coord


class DeriveStatusTests(unittest.TestCase):
    """Combine cloud presence with live P2P evidence."""

    def test_cloud_states_map_through(self):
        for raw, expected, online in (
            ("online", "online", True),
            ("dormancy", "dormant", True),
            ("offline", "offline", False),
            ("weird", "unknown", None),
        ):
            with self.subTest(raw=raw):
                coord = coordinator(battery=True, status=raw)
                coord._connectivity_tick(1000.0)
                self.assertEqual(coord.connection_status, expected)
                self.assertEqual(coord.camera_online, online)

    def test_recent_video_means_streaming_even_if_cloud_disagrees(self):
        coord = coordinator(status="offline")
        coord._last_video_time = 995.0
        coord._connectivity_tick(1000.0)
        self.assertEqual(coord.connection_status, "streaming")
        self.assertTrue(coord.camera_online)

    def test_stale_video_falls_back_to_cloud(self):
        coord = coordinator(status="online")
        coord._connectivity_tick(1000.0)
        coord._last_video_time = 1000.0
        coord._refresh_connection_status(1000.0 + LIVE + 1)
        self.assertEqual(coord.connection_status, "online")

    def test_always_connected_without_video_becomes_unreachable(self):
        coord = coordinator(status="online")
        coord._connectivity_tick(1000.0)
        coord._refresh_connection_status(1000.0 + UNREACHABLE - 1)
        self.assertEqual(coord.connection_status, "online")
        coord._refresh_connection_status(1000.0 + UNREACHABLE + 1)
        self.assertEqual(coord.connection_status, "unreachable")
        self.assertFalse(coord.camera_online)

    def test_unreachable_clock_runs_from_last_video(self):
        coord = coordinator(status="online")
        coord._connectivity_tick(1000.0)
        coord._last_video_time = 2000.0
        coord._refresh_connection_status(2000.0 + UNREACHABLE - 1)
        self.assertEqual(coord.connection_status, "online")
        coord._refresh_connection_status(2000.0 + UNREACHABLE + 1)
        self.assertEqual(coord.connection_status, "unreachable")

    def test_on_demand_mode_never_reports_unreachable(self):
        coord = coordinator(status="online")
        coord.set_always_connected(False)
        coord._connectivity_tick(1000.0)
        coord._refresh_connection_status(1000.0 + UNREACHABLE * 10)
        self.assertEqual(coord.connection_status, "online")

    def test_battery_camera_never_reports_unreachable(self):
        coord = coordinator(battery=True, status="dormancy")
        coord._connectivity_tick(1000.0)
        coord._refresh_connection_status(1000.0 + UNREACHABLE * 10)
        self.assertEqual(coord.connection_status, "dormant")


class PublishTests(unittest.TestCase):
    """Updates fire only on change; polling respects its cadence."""

    def test_update_fires_only_on_transition(self):
        coord = coordinator(battery=True)
        coord._connectivity_tick(1000.0)
        coord._connectivity_tick(1001.0)
        coord._connectivity_tick(1002.0)
        self.assertEqual(coord._fire_update.call_count, 1)

    def test_cloud_polled_once_per_interval(self):
        coord = coordinator(battery=True)
        for second in range(int(POLL) + 5):
            coord._connectivity_tick(1000.0 + second)
        self.assertEqual(coord._api.get_device_status.call_count, 2)

    def test_unknown_triggers_one_reauth_and_retry(self):
        coord = coordinator(battery=True)
        coord._api.get_device_status.side_effect = ["unknown", "online"]
        coord._connectivity_tick(1000.0)
        coord._reauthenticate_api.assert_called_once()
        self.assertEqual(coord.cloud_status, "online")

    def test_no_api_leaves_status_unknown(self):
        coord = coordinator(battery=True)
        coord._api = None
        coord._connectivity_tick(1000.0)
        self.assertEqual(coord.connection_status, "unknown")
        self.assertIsNone(coord.camera_online)

    def test_last_seen_tracks_reachable_states_only(self):
        coord = coordinator(battery=True, status="offline")
        coord._connectivity_tick(1000.0)
        self.assertIsNone(coord.last_seen)
        coord._last_video_time = 1001.0
        with patch("time.time", return_value=1_700_000_000.0):
            coord._refresh_connection_status(1001.0)
        self.assertEqual(coord.last_seen, 1_700_000_000.0)


def load_platform(name: str, bindings: dict[str, dict]) -> ModuleType:
    """Import a platform with HA modules the standalone stubs lack."""
    stubs = {}
    for module_name, attributes in bindings.items():
        module = ModuleType(module_name)
        vars(module).update(attributes)
        stubs[module_name] = module
    with patch.dict(sys.modules, stubs):
        sys.modules.pop(f"custom_components.cloudplus.{name}", None)
        return importlib.import_module(f"custom_components.cloudplus.{name}")


def setup_entities(module: ModuleType, coord: COORDINATOR) -> list:
    added: list = []
    hass = SimpleNamespace(data={CONST.DOMAIN: {"entry": coord}})
    coord.register_update_callback = Mock(return_value=Mock())
    coord.supports_iot = Mock(return_value=False)
    coord.has_iot_code = Mock(return_value=False)
    coord.iot_capability = Mock(return_value=None)
    asyncio.run(
        module.async_setup_entry(hass, SimpleNamespace(entry_id="entry"), added.extend)
    )
    return added


class EntityTests(unittest.TestCase):
    """Online binary sensor and Connection Status sensor."""

    def setUp(self):
        self.binary = load_platform(
            "binary_sensor",
            {
                "homeassistant.components.binary_sensor": {
                    "BinarySensorEntity": type("BinarySensorEntity", (), {}),
                    "BinarySensorDeviceClass": SimpleNamespace(
                        MOTION="motion",
                        RUNNING="running",
                        BATTERY_CHARGING="battery_charging",
                        CONNECTIVITY="connectivity",
                    ),
                }
            },
        )
        self.sensor = load_platform(
            "sensor",
            {
                "homeassistant.components.sensor": {
                    "SensorEntity": type("SensorEntity", (), {}),
                    "SensorDeviceClass": SimpleNamespace(
                        TEMPERATURE="temperature",
                        HUMIDITY="humidity",
                        BATTERY="battery",
                        ENUM="enum",
                    ),
                    "SensorStateClass": SimpleNamespace(MEASUREMENT="measurement"),
                },
                "homeassistant.const": {
                    "PERCENTAGE": "%",
                    "EntityCategory": SimpleNamespace(DIAGNOSTIC="diagnostic"),
                    "UnitOfTemperature": SimpleNamespace(CELSIUS="C"),
                },
            },
        )

    def online_entity(self, coord):
        entities = setup_entities(self.binary, coord)
        return next(e for e in entities if type(e).__name__ == "CloudEdgeMeariOnlineSensor")

    def test_every_camera_gets_both_entities(self):
        for battery in (False, True):
            with self.subTest(battery=battery):
                coord = coordinator(battery=battery)
                names = {type(e).__name__ for e in setup_entities(self.binary, coord)}
                names |= {type(e).__name__ for e in setup_entities(self.sensor, coord)}
                self.assertIn("CloudEdgeMeariOnlineSensor", names)
                self.assertIn("CloudEdgeMeariConnectionStatusSensor", names)

    def test_online_sensor_reports_state_and_attributes(self):
        coord = coordinator(status="offline")
        coord._connectivity_tick(1000.0)
        entity = self.online_entity(coord)
        self.assertFalse(entity.is_on)
        attrs = entity.extra_state_attributes
        self.assertEqual(attrs["connection_status"], "offline")
        self.assertEqual(attrs["cloud_status"], "offline")
        self.assertIsNone(attrs["last_seen"])

    def test_online_sensor_stays_available_when_integration_degrades(self):
        coord = coordinator(status="offline")
        coord._connectivity_tick(1000.0)
        coord._available = False
        self.assertTrue(self.online_entity(coord).available)

    def test_online_sensor_unavailable_when_nothing_is_known(self):
        coord = coordinator()
        coord._available = False
        self.assertFalse(self.online_entity(coord).available)

    def test_connection_status_sensor_value(self):
        coord = coordinator()
        coord._last_video_time = 999.0
        coord._connectivity_tick(1000.0)
        entity = next(
            e
            for e in setup_entities(self.sensor, coord)
            if type(e).__name__ == "CloudEdgeMeariConnectionStatusSensor"
        )
        self.assertEqual(entity.native_value, "streaming")
        self.assertIn("unreachable", entity._attr_options)


if __name__ == "__main__":
    unittest.main()
