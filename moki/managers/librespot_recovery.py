"""
Librespot recovery — restart go-librespot when the HTTP API hangs but WiFi is fine.
"""
import logging
import subprocess
import threading
import time
from collections import deque
from typing import Callable, Deque, Optional

from ..config import (
    LIBRESPOT_RECOVERY_COOLDOWN_SEC,
    LIBRESPOT_RECOVERY_CONTEXT_STALL_SEC,
    LIBRESPOT_RECOVERY_HEALTH_WAIT_SEC,
    LIBRESPOT_RECOVERY_MAX_PER_HOUR,
    LIBRESPOT_RECOVERY_PLAY_TIMEOUT_COUNT,
    LIBRESPOT_RECOVERY_PLAY_TIMEOUT_WINDOW,
    LIBRESPOT_RECOVERY_STATUS_FAIL_THRESHOLD,
)
from ..api.librespot import LibrespotAPIProtocol

logger = logging.getLogger(__name__)


class LibrespotRecoveryManager:
    """Detect librespot hangs and restart the systemd service."""

    def __init__(
        self,
        api: LibrespotAPIProtocol,
        has_network_fn: Callable[[], bool],
        on_before_restart: Callable[[], None],
        on_after_restart: Callable[[bool], None],
        on_toast: Callable[[str], None],
        mock_mode: bool = False,
    ):
        self.api = api
        self._has_network = has_network_fn
        self._on_before_restart = on_before_restart
        self._on_after_restart = on_after_restart
        self._on_toast = on_toast
        self._mock_mode = mock_mode

        self._lock = threading.Lock()
        self._in_progress = False
        self._last_restart_at: float = 0.0
        self._restart_times: Deque[float] = deque()
        self._play_timeout_times: Deque[float] = deque()

    def note_transport_failure(self, command: str):
        """Record a play/seek transport failure for timeout cascade detection."""
        if command not in ('play', 'seek'):
            return
        now = time.time()
        with self._lock:
            self._play_timeout_times.append(now)
            cutoff = now - LIBRESPOT_RECOVERY_PLAY_TIMEOUT_WINDOW
            while self._play_timeout_times and self._play_timeout_times[0] < cutoff:
                self._play_timeout_times.popleft()

    def should_restart_for_connection(
        self,
        connected: bool,
        fail_count: int,
    ) -> bool:
        if self._mock_mode or connected:
            return False
        if fail_count < LIBRESPOT_RECOVERY_STATUS_FAIL_THRESHOLD:
            return False
        return self._has_network()

    def should_restart_for_context_stall(
        self,
        stall_age: float,
        spotify_ctx: Optional[str],
        waiting_for_commit: bool,
    ) -> bool:
        if self._mock_mode:
            return False
        if stall_age < LIBRESPOT_RECOVERY_CONTEXT_STALL_SEC:
            return False
        if not waiting_for_commit:
            return False
        if spotify_ctx:
            return False
        return True

    def should_restart_for_timeouts(self) -> bool:
        if self._mock_mode:
            return False
        return self._timeout_count() >= LIBRESPOT_RECOVERY_PLAY_TIMEOUT_COUNT

    def maybe_restart(self, reason: str) -> bool:
        """Attempt librespot restart if cooldown allows. Returns True if started."""
        if self._mock_mode:
            return False

        with self._lock:
            if self._in_progress:
                logger.info(f'LIBRESPOT RECOVERY skipped ({reason}): already in progress')
                return False

            now = time.time()
            if now - self._last_restart_at < LIBRESPOT_RECOVERY_COOLDOWN_SEC:
                wait = LIBRESPOT_RECOVERY_COOLDOWN_SEC - (now - self._last_restart_at)
                logger.warning(
                    f'LIBRESPOT RECOVERY skipped ({reason}): cooldown {wait:.0f}s remaining'
                )
                return False

            hour_ago = now - 3600.0
            while self._restart_times and self._restart_times[0] < hour_ago:
                self._restart_times.popleft()
            if len(self._restart_times) >= LIBRESPOT_RECOVERY_MAX_PER_HOUR:
                logger.warning(
                    f'LIBRESPOT RECOVERY skipped ({reason}): hourly limit '
                    f'({LIBRESPOT_RECOVERY_MAX_PER_HOUR}) reached'
                )
                return False

            self._in_progress = True

        logger.warning(f'LIBRESPOT RECOVERY restart triggered | reason={reason}')
        self._on_toast('Spotify wird neu verbunden…')
        self._on_before_restart()

        def _run():
            ok = False
            try:
                ok = self._restart_service()
                if ok:
                    ok = self._wait_for_health()
            except Exception as e:
                logger.error(f'LIBRESPOT RECOVERY failed: {e}', exc_info=True)
            finally:
                with self._lock:
                    self._in_progress = False
                    if ok:
                        now = time.time()
                        self._last_restart_at = now
                        self._restart_times.append(now)
                        self._play_timeout_times.clear()
                if ok:
                    logger.info('LIBRESPOT RECOVERY restart complete')
                else:
                    logger.error('LIBRESPOT RECOVERY restart failed or health wait timed out')
                self._on_after_restart(ok)

        threading.Thread(target=_run, daemon=True, name='librespot-recovery').start()
        return True

    def _timeout_count(self) -> int:
        now = time.time()
        cutoff = now - LIBRESPOT_RECOVERY_PLAY_TIMEOUT_WINDOW
        with self._lock:
            while self._play_timeout_times and self._play_timeout_times[0] < cutoff:
                self._play_timeout_times.popleft()
            return len(self._play_timeout_times)

    def _restart_service(self) -> bool:
        for cmd in (
            ['sudo', 'systemctl', 'stop', 'moki-librespot'],
            ['sudo', 'systemctl', 'start', 'moki-librespot'],
        ):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if result.returncode != 0:
                    logger.error(
                        f'LIBRESPOT RECOVERY command failed: {" ".join(cmd)} | '
                        f'stderr={result.stderr.strip()}'
                    )
                    return False
            except Exception as e:
                logger.error(f'LIBRESPOT RECOVERY command error: {" ".join(cmd)} | {e}')
                return False
        return True

    def _wait_for_health(self) -> bool:
        deadline = time.time() + LIBRESPOT_RECOVERY_HEALTH_WAIT_SEC
        while time.time() < deadline:
            if self.api.is_connected():
                status = self.api.status()
                if status is not None:
                    return True
            time.sleep(0.5)
        return False
