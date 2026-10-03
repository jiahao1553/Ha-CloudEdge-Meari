"""Always-connected stream policy for mains-powered (IPC) cameras.

Upstream opens the P2P session only while a consumer is attached, so every
dashboard view pays for signaling, relay setup and the first keyframe. With
always-connected on, the session stays open permanently: the stream server
always holds a fresh bootstrap (PAT/PMT + keyframe) and a new viewer joins
instantly. Failed sessions back off exponentially while nobody is watching,
so a camera that is offline does not hammer the Meari signaling servers.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from ..const import ALWAYS_CONNECTED_RETRY_INITIAL_S, ALWAYS_CONNECTED_RETRY_MAX_S

_LOGGER = logging.getLogger(__name__)


class AlwaysConnectedMixin:
    """Decide when a mains-powered camera's P2P session should be open."""

    _always_connected: bool
    _is_snap: bool
    _ipc_retry_s: float
    _ipc_next_start_at: float
    _ipc_session_frames_at_start: int
    _p2p_video_frames: int
    _live_deadline: float
    _sn_num: str
    _stream_server: Any
    _fire_update: Callable[[], None]

    def _init_always_connected(self) -> None:
        """Mains-powered cameras default to always-connected; battery never."""
        self._always_connected = not self._is_snap
        self._ipc_retry_s = ALWAYS_CONNECTED_RETRY_INITIAL_S
        self._ipc_next_start_at = 0.0
        self._ipc_session_frames_at_start = 0

    @property
    def always_connected(self) -> bool:
        return self._always_connected

    def set_always_connected(self, enabled: bool) -> None:
        """Keep (or stop keeping) the mains-powered P2P session open."""
        enabled = bool(enabled)
        if enabled == self._always_connected:
            return
        self._always_connected = enabled
        # A user toggle is a fresh start: forget any failure backoff.
        self._ipc_retry_s = ALWAYS_CONNECTED_RETRY_INITIAL_S
        self._ipc_next_start_at = 0.0
        _LOGGER.info(
            "Always-connected stream %s for %s",
            "enabled" if enabled else "disabled",
            self._sn_num,
        )
        self._fire_update()

    def _ipc_has_viewer(self, now: float) -> bool:
        """Return True when a real consumer or a wake window wants video."""
        return self._stream_server.client_count > 0 or now < self._live_deadline

    def _ipc_should_stream(self, now: float) -> bool:
        """Return True when the mains-powered P2P session should be open."""
        if self._always_connected and not self._is_snap:
            return True
        return self._ipc_has_viewer(now)

    def _ipc_ready_to_start(self, now: float) -> bool:
        """Gate a new P2P session on the backoff timer.

        A real viewer (or wake window) always starts immediately; only the
        unattended always-connected session waits out the backoff.
        """
        if self._ipc_has_viewer(now):
            self._ipc_next_start_at = 0.0
        elif now < self._ipc_next_start_at:
            time.sleep(min(1.0, self._ipc_next_start_at - now))
            return False
        self._ipc_session_frames_at_start = self._p2p_video_frames
        return True

    def _ipc_session_ended(self, now: float) -> None:
        """Schedule the next always-connected attempt after a session exits."""
        if self._p2p_video_frames > self._ipc_session_frames_at_start:
            # The session delivered video, so the path works: retry promptly.
            self._ipc_retry_s = ALWAYS_CONNECTED_RETRY_INITIAL_S
            self._ipc_next_start_at = now + ALWAYS_CONNECTED_RETRY_INITIAL_S
            return
        delay = self._ipc_retry_s
        self._ipc_next_start_at = now + delay
        self._ipc_retry_s = min(ALWAYS_CONNECTED_RETRY_MAX_S, delay * 2.0)
        if self._always_connected and not self._ipc_has_viewer(now):
            _LOGGER.warning(
                "Always-connected P2P session for %s ended without video; "
                "retrying in %.0fs",
                self._sn_num,
                delay,
            )
