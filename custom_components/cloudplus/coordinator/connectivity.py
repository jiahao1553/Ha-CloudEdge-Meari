"""Camera connectivity tracking: is the camera online right now?

Two independent signals are combined:

* **Cloud presence** — Meari's OpenAPI reports ``online`` / ``dormancy`` /
  ``offline`` for the device. Polled every ``CONNECTIVITY_POLL_INTERVAL_S``.
* **Live evidence** — video frames arriving over P2P prove the camera is up
  regardless of what the cloud says, and an always-connected session that
  keeps failing proves it is unreachable even if the cloud still reports it
  online (e.g. Wi-Fi up but the camera's media path is dead).

The derived ``connection_status`` is one of ``CONNECTION_STATES``; the
``camera_online`` property collapses it to True / False / None (unknown).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from ..const import (
    CONNECTIVITY_LIVE_EVIDENCE_S,
    CONNECTIVITY_POLL_INTERVAL_S,
    CONNECTIVITY_UNREACHABLE_AFTER_S,
)

_LOGGER = logging.getLogger(__name__)

CONNECTION_STATES = ["streaming", "online", "dormant", "offline", "unreachable", "unknown"]
_ONLINE_STATES = frozenset({"streaming", "online", "dormant"})
_OFFLINE_STATES = frozenset({"offline", "unreachable"})
_CLOUD_STATUS_MAP = {"online": "online", "dormancy": "dormant", "offline": "offline"}


class ConnectivityMixin:
    """Track and publish whether the camera is reachable."""

    _api: Any
    _is_snap: bool
    _sn_num: str
    _last_video_time: float
    _fire_update: Callable[[], None]
    _reauthenticate_api: Callable[[Any, str], bool]
    _ipc_should_stream: Callable[[float], bool]

    def _init_connectivity(self) -> None:
        self._cloud_status = "unknown"
        self._connection_status = "unknown"
        self._connectivity_next_poll = 0.0
        self._connectivity_started_at = 0.0
        self._last_seen_epoch: float | None = None

    # ----- public surface (entities) -------------------------------------

    @property
    def connection_status(self) -> str:
        return self._connection_status

    @property
    def cloud_status(self) -> str:
        return self._cloud_status

    @property
    def camera_online(self) -> bool | None:
        if self._connection_status in _ONLINE_STATES:
            return True
        if self._connection_status in _OFFLINE_STATES:
            return False
        return None

    @property
    def last_seen(self) -> float | None:
        """Unix time the camera was last proven reachable (video or cloud)."""
        return self._last_seen_epoch

    # ----- worker-thread hooks -------------------------------------------

    def _connectivity_tick(self, now: float) -> None:
        """Poll the cloud when due and republish the derived state.

        Called from the coordinator's session loop thread (~1 Hz), so the
        blocking HTTP call never runs on the event loop.
        """
        if not self._connectivity_started_at:
            self._connectivity_started_at = now
        if now >= self._connectivity_next_poll:
            self._connectivity_next_poll = now + CONNECTIVITY_POLL_INTERVAL_S
            self._poll_cloud_status()
        self._refresh_connection_status(now)

    def _poll_cloud_status(self) -> None:
        api = self._api
        if api is None or not getattr(api, "openapi_server", None):
            return
        raw = api.get_device_status(self._sn_num)
        if raw == "unknown" and self._reauthenticate_api(api, "Connectivity poll"):
            raw = api.get_device_status(self._sn_num)
        status = _CLOUD_STATUS_MAP.get(str(raw).lower(), "unknown")
        if status != self._cloud_status:
            _LOGGER.debug(
                "Cloud status for %s: %s -> %s", self._sn_num, self._cloud_status, status
            )
        self._cloud_status = status

    def _derive_connection_status(self, now: float) -> str:
        last_video = self._last_video_time
        if last_video and now - last_video <= CONNECTIVITY_LIVE_EVIDENCE_S:
            return "streaming"
        if self._cloud_status == "offline":
            return "offline"
        # A mains camera we are actively trying to stream from, with no video
        # for a long time, is unreachable whatever the cloud claims.
        if not self._is_snap and self._ipc_should_stream(now):
            reference = max(last_video, self._connectivity_started_at)
            if reference and now - reference > CONNECTIVITY_UNREACHABLE_AFTER_S:
                return "unreachable"
        return self._cloud_status

    def _refresh_connection_status(self, now: float) -> None:
        status = self._derive_connection_status(now)
        if status in _ONLINE_STATES:
            self._last_seen_epoch = time.time()
        if status == self._connection_status:
            return
        _LOGGER.info(
            "Camera %s connectivity: %s -> %s",
            self._sn_num,
            self._connection_status,
            status,
        )
        self._connection_status = status
        self._fire_update()
