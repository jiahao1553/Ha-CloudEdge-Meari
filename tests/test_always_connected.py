"""Offline tests for the always-connected mains-powered stream mode."""

from __future__ import annotations

import asyncio
import importlib
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from debug_tools.bootstrap import _bootstrap_integration_modules

MODULES = _bootstrap_integration_modules()
COORDINATOR = MODULES["coordinator"].CloudEdgeMeariCoordinator
CONST = importlib.import_module("custom_components.cloudplus.const")
INITIAL = CONST.ALWAYS_CONNECTED_RETRY_INITIAL_S
MAXIMUM = CONST.ALWAYS_CONNECTED_RETRY_MAX_S


def coordinator(*, battery: bool = False, clients: int = 0) -> COORDINATOR:
    """Build just enough coordinator state, without threads or cloud traffic."""
    coord = COORDINATOR.__new__(COORDINATOR)
    coord._is_snap = battery
    coord._always_connected = not battery
    coord._ipc_retry_s = INITIAL
    coord._ipc_next_start_at = 0.0
    coord._ipc_session_frames_at_start = 0
    coord._p2p_video_frames = 0
    coord._live_deadline = 0.0
    coord._sn_num = "synthetic-camera"
    coord._device_name = "Synthetic camera"
    coord._device_category = "ipc"
    coord._has_lamp = False
    coord._stream_server = SimpleNamespace(client_count=clients)
    coord._fire_update = Mock()
    return coord


class ShouldStreamTests(unittest.TestCase):
    """Decide when the mains-powered P2P session must stay open."""

    def test_mains_camera_streams_without_viewers_by_default(self):
        self.assertTrue(coordinator()._ipc_should_stream(100.0))

    def test_disabled_mode_matches_upstream_on_demand_behaviour(self):
        coord = coordinator()
        coord.set_always_connected(False)
        self.assertFalse(coord._ipc_should_stream(100.0))
        coord._stream_server.client_count = 1
        self.assertTrue(coord._ipc_should_stream(100.0))

    def test_wake_window_still_streams_when_disabled(self):
        coord = coordinator()
        coord.set_always_connected(False)
        coord._live_deadline = 150.0
        self.assertTrue(coord._ipc_should_stream(100.0))

    def test_battery_camera_is_never_forced_on(self):
        coord = coordinator(battery=True)
        coord._always_connected = True
        self.assertFalse(coord._ipc_should_stream(100.0))


class BackoffTests(unittest.TestCase):
    """Failed sessions back off; working sessions reconnect promptly."""

    def test_failed_sessions_back_off_exponentially_up_to_cap(self):
        coord = coordinator()
        delays = []
        for _ in range(8):
            coord._ipc_session_ended(1000.0)
            delays.append(coord._ipc_next_start_at - 1000.0)
        self.assertEqual(delays[:4], [INITIAL, INITIAL * 2, INITIAL * 4, INITIAL * 8])
        self.assertEqual(max(delays), MAXIMUM)

    def test_session_with_video_resets_backoff(self):
        coord = coordinator()
        for _ in range(4):
            coord._ipc_session_ended(1000.0)
        coord._ipc_session_frames_at_start = 10
        coord._p2p_video_frames = 250
        coord._ipc_session_ended(2000.0)
        self.assertEqual(coord._ipc_retry_s, INITIAL)
        self.assertEqual(coord._ipc_next_start_at, 2000.0 + INITIAL)

    def test_viewer_bypasses_backoff(self):
        coord = coordinator()
        coord._ipc_next_start_at = 5000.0
        coord._stream_server.client_count = 1
        self.assertTrue(coord._ipc_ready_to_start(1000.0))
        self.assertEqual(coord._ipc_next_start_at, 0.0)

    def test_unattended_session_waits_for_backoff(self):
        coord = coordinator()
        coord._ipc_next_start_at = 1000.5
        with patch("time.sleep") as sleep:
            self.assertFalse(coord._ipc_ready_to_start(1000.0))
        sleep.assert_called_once()
        coord._p2p_video_frames = 42
        self.assertTrue(coord._ipc_ready_to_start(1001.0))
        self.assertEqual(coord._ipc_session_frames_at_start, 42)

    def test_init_defaults(self):
        for battery in (False, True):
            with self.subTest(battery=battery):
                coord = COORDINATOR.__new__(COORDINATOR)
                coord._is_snap = battery
                coord._init_always_connected()
                self.assertEqual(coord.always_connected, not battery)

    def test_toggle_clears_backoff_and_notifies(self):
        coord = coordinator()
        for _ in range(4):
            coord._ipc_session_ended(1000.0)
        coord.set_always_connected(False)
        coord.set_always_connected(True)
        self.assertEqual(coord._ipc_retry_s, INITIAL)
        self.assertEqual(coord._ipc_next_start_at, 0.0)
        self.assertEqual(coord._fire_update.call_count, 2)

    def test_redundant_toggle_is_a_no_op(self):
        coord = coordinator()
        coord.set_always_connected(True)
        coord._fire_update.assert_not_called()


def load_switch_platform() -> ModuleType:
    """Import switch.py with the HA modules the standalone stubs lack."""

    class RestoreEntity:  # pylint: disable=too-few-public-methods
        """Minimal stand-in; tests patch async_get_last_state."""

    stubs = {}
    for name, attributes in {
        "homeassistant.components.switch": {
            "SwitchEntity": type("SwitchEntity", (), {}),
        },
        "homeassistant.helpers.restore_state": {"RestoreEntity": RestoreEntity},
    }.items():
        module = ModuleType(name)
        vars(module).update(attributes)
        stubs[name] = module
    with patch.dict(sys.modules, stubs):
        sys.modules.pop("custom_components.cloudplus.switch", None)
        return importlib.import_module("custom_components.cloudplus.switch")


class AlwaysConnectedSwitchTests(unittest.TestCase):
    """The switch restores the user's last choice across restarts."""

    def setUp(self):
        self.switch_mod = load_switch_platform()
        self.coord = coordinator()
        self.coord.register_update_callback = Mock(return_value=Mock())
        self.entity = self.switch_mod.CloudEdgeMeariAlwaysConnectedSwitch(
            self.coord, Mock()
        )
        self.entity.async_write_ha_state = Mock()

    def restore(self, state):
        last = None if state is None else SimpleNamespace(state=state)
        self.entity.async_get_last_state = AsyncMock(return_value=last)
        asyncio.run(self.entity.async_added_to_hass())

    def test_restores_off(self):
        self.restore("off")
        self.assertFalse(self.coord.always_connected)
        self.assertFalse(self.entity.is_on)

    def test_first_install_keeps_default_on(self):
        self.restore(None)
        self.assertTrue(self.entity.is_on)

    def test_unavailable_state_is_ignored(self):
        self.restore("unavailable")
        self.assertTrue(self.entity.is_on)

    def test_turn_off_and_on(self):
        asyncio.run(self.entity.async_turn_off())
        self.assertFalse(self.coord.always_connected)
        asyncio.run(self.entity.async_turn_on())
        self.assertTrue(self.coord.always_connected)
        self.assertEqual(self.entity.async_write_ha_state.call_count, 2)

    def test_only_mains_cameras_get_the_switch(self):
        for battery, expected in ((False, True), (True, False)):
            with self.subTest(battery=battery):
                coord = coordinator(battery=battery)
                coord.supports_iot = Mock(return_value=False)
                coord.has_iot_code = Mock(return_value=False)
                hass = SimpleNamespace(data={CONST.DOMAIN: {"entry": coord}})
                added = []
                asyncio.run(
                    self.switch_mod.async_setup_entry(
                        hass, SimpleNamespace(entry_id="entry"), added.extend
                    )
                )
                kinds = {type(item).__name__ for item in added}
                self.assertEqual(
                    "CloudEdgeMeariAlwaysConnectedSwitch" in kinds, expected
                )


if __name__ == "__main__":
    unittest.main()
