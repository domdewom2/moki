"""
Librespot API Client - Direct REST API for go-librespot.
"""
import logging
import time
from typing import Callable, Optional, Protocol, runtime_checkable

import requests

logger = logging.getLogger(__name__)


@runtime_checkable
class LibrespotAPIProtocol(Protocol):
    """Interface that both real and mock API must implement."""

    def status(self) -> Optional[dict]: ...
    def play(self, uri: str, skip_to_uri: str = None, paused: bool = False) -> Optional[bool]: ...
    def pause(self) -> bool: ...
    def resume(self) -> bool: ...
    def next(self) -> bool: ...
    def prev(self) -> bool: ...
    def seek(self, position: int) -> bool: ...
    def set_volume(self, level: int) -> bool: ...
    def set_repeat_context(self, enabled: bool) -> bool: ...
    def is_connected(self) -> bool: ...
    def metrics_snapshot(self) -> dict: ...


class LibrespotAPI:
    """Direct REST API client for go-librespot."""
    
    def __init__(
        self,
        base_url: str,
        on_transport_failure: Optional[Callable[[str], None]] = None,
    ):
        self.base_url = base_url
        self._on_transport_failure = on_transport_failure
        self.session = requests.Session()
        self.session.headers['Content-Type'] = 'application/json'
        self._next_allowed_at = {
            'play': 0.0,
            'pause': 0.0,
            'resume': 0.0,
            'next': 0.0,
            'prev': 0.0,
            'volume': 0.0,
            'seek': 0.0,
        }
        self._backoff_s = {k: 0.0 for k in self._next_allowed_at}
        self._last_backoff_log = {k: 0.0 for k in self._next_allowed_at}
        self._suppressed_count = {k: 0 for k in self._next_allowed_at}
        self._failure_count = {k: 0 for k in self._next_allowed_at}

    def _recycle_session(self):
        try:
            self.session.close()
        except Exception:
            pass
        self.session = requests.Session()
        self.session.headers['Content-Type'] = 'application/json'
        logger.info('API session recycled after transport error')

    def _handle_transport_error(self, command: str, error: Exception):
        if isinstance(error, (requests.Timeout, requests.ReadTimeout)):
            self._recycle_session()
        self._record_result(command, False)
        if self._on_transport_failure:
            self._on_transport_failure(command)

    def _allow_request(self, command: str) -> bool:
        now = time.time()
        next_allowed = self._next_allowed_at.get(command, 0.0)
        if now < next_allowed:
            self._suppressed_count[command] = self._suppressed_count.get(command, 0) + 1
            if now - self._last_backoff_log.get(command, 0.0) > 2.0:
                logger.warning(
                    f'API {command} suppressed by backoff | wait={next_allowed - now:.2f}s | '
                    f'suppressed={self._suppressed_count[command]}'
                )
                self._last_backoff_log[command] = now
            return False
        return True

    def _record_result(self, command: str, success: bool):
        if success:
            self._backoff_s[command] = 0.0
            self._failure_count[command] = 0
            self._next_allowed_at[command] = 0.0
            return
        self._failure_count[command] = self._failure_count.get(command, 0) + 1
        current = self._backoff_s.get(command, 0.0)
        next_backoff = min(3.0, 0.4 if current <= 0 else current * 2.0)
        self._backoff_s[command] = next_backoff
        self._next_allowed_at[command] = time.time() + next_backoff
    
    def status(self) -> Optional[dict]:
        """Get current playback status.

        Returns:
            dict: Parsed /status payload. For HTTP 204 (reachable, no active session),
                  returns a minimal "stopped" payload so callers can distinguish
                  no-session from transport errors.
            None: Transport/request error (status unknown).
        """
        try:
            resp = self.session.get(f'{self.base_url}/status', timeout=2)
            if resp.status_code == 204:
                # Explicitly represent "connected but no active session".
                return {
                    'stopped': True,
                    'paused': False,
                    'context_uri': None,
                    'track': None,
                }
            return resp.json()
        except requests.RequestException as e:
            logger.debug(f'Status request failed: {e}')
            return None
    
    def play(self, uri: str, skip_to_uri: str = None, paused: bool = False) -> Optional[bool]:
        """Play a Spotify URI (album/playlist), optionally starting at a specific track.

        Returns:
            True  - playback started successfully
            None  - librespot has no active Spotify session (user must connect via app)
            False - request failed for other reasons (librespot busy/starting up)
        """
        if not self._allow_request('play'):
            return False
        try:
            body = {'uri': uri}
            logger.info(f'API play: context={uri[:50]}...')
            if skip_to_uri:
                body['skip_to_uri'] = skip_to_uri
                logger.info(f'  skip_to_uri: {skip_to_uri}')
            if paused:
                body['paused'] = True
                logger.info('  paused: true (will seek before resume)')
            
            resp = self.session.post(
                f'{self.base_url}/player/play',
                json=body,
                timeout=10  # Longer timeout for slow Pi/network
            )
            if resp.status_code == 200:
                logger.info('Play request sent')
                self._record_result('play', True)
                return True
            elif resp.status_code == 204:
                logger.warning('Play ignored: no active Spotify session')
                self._record_result('play', False)
                return None
            else:
                logger.warning(f'Play failed: {resp.status_code} {resp.text}')
            ok = resp.ok
            self._record_result('play', ok)
            return ok
        except requests.RequestException as e:
            logger.error(f'Play error for URI {uri[:50] if uri else "None"}...: {e}', exc_info=True)
            self._handle_transport_error('play', e)
            return False
    
    def pause(self) -> bool:
        """Pause playback."""
        if not self._allow_request('pause'):
            return False
        try:
            resp = self.session.post(f'{self.base_url}/player/pause', timeout=2)
            logger.debug(f'Pause: {resp.status_code}')
            self._record_result('pause', resp.ok)
            return resp.ok
        except requests.RequestException as e:
            logger.error('Pause error', exc_info=True)
            self._handle_transport_error('pause', e)
            return False
    
    def resume(self) -> bool:
        """Resume playback."""
        if not self._allow_request('resume'):
            return False
        try:
            resp = self.session.post(f'{self.base_url}/player/resume', timeout=2)
            logger.debug(f'Resume: {resp.status_code}')
            self._record_result('resume', resp.ok)
            return resp.ok
        except requests.RequestException as e:
            logger.error('Resume error', exc_info=True)
            self._handle_transport_error('resume', e)
            return False
    
    def next(self) -> bool:
        """Skip to next track."""
        if not self._allow_request('next'):
            return False
        try:
            resp = self.session.post(f'{self.base_url}/player/next', timeout=2)
            logger.debug(f'Next: {resp.status_code}')
            self._record_result('next', resp.ok)
            return resp.ok
        except requests.RequestException as e:
            logger.error('Next error', exc_info=True)
            self._handle_transport_error('next', e)
            return False

    def prev(self) -> bool:
        """Skip to previous track."""
        if not self._allow_request('prev'):
            return False
        try:
            resp = self.session.post(f'{self.base_url}/player/prev', timeout=2)
            logger.debug(f'Prev: {resp.status_code}')
            self._record_result('prev', resp.ok)
            return resp.ok
        except requests.RequestException as e:
            logger.error('Prev error', exc_info=True)
            self._handle_transport_error('prev', e)
            return False
    
    def seek(self, position: int) -> bool:
        """Seek to position in milliseconds."""
        if not self._allow_request('seek'):
            return False
        try:
            resp = self.session.post(
                f'{self.base_url}/player/seek',
                json={'position': position},
                timeout=2
            )
            logger.debug(f'Seek to {position}ms: {resp.status_code}')
            ok = resp.ok
            self._record_result('seek', ok)
            return ok
        except requests.RequestException as e:
            logger.error(f'Seek error to position {position}ms', exc_info=True)
            self._handle_transport_error('seek', e)
            return False
    
    def set_volume(self, level: int) -> bool:
        """Set volume level (0-100)."""
        if not self._allow_request('volume'):
            return False
        try:
            resp = self.session.post(
                f'{self.base_url}/player/volume',
                json={'volume': level},
                timeout=2
            )
            logger.debug(f'Volume {level}%: {resp.status_code}')
            self._record_result('volume', resp.ok)
            return resp.ok
        except requests.RequestException as e:
            logger.error(f'Volume error setting level {level}%', exc_info=True)
            self._handle_transport_error('volume', e)
            return False

    def set_repeat_context(self, enabled: bool) -> bool:
        """Set Spotify repeat mode for the current context (album/playlist)."""
        try:
            resp = self.session.post(
                f'{self.base_url}/player/repeat_context',
                json={'repeat_context': enabled},
                timeout=2,
            )
            logger.info(f'Repeat context {enabled}: {resp.status_code}')
            if not resp.ok:
                logger.warning(f'Repeat context failed: {resp.status_code} {resp.text}')
            return resp.ok
        except requests.RequestException as e:
            logger.warning(f'Repeat context error (enabled={enabled}): {e}')
            return False
    
    def is_connected(self) -> bool:
        """Check if librespot is reachable (may or may not have an active session)."""
        try:
            resp = self.session.get(f'{self.base_url}/status', timeout=1)
            return resp.status_code in (200, 204)
        except requests.RequestException:
            return False

    def metrics_snapshot(self) -> dict:
        """Return lightweight counters for operational diagnostics."""
        return {
            'suppressed': dict(self._suppressed_count),
            'failures': dict(self._failure_count),
        }


class NullLibrespotAPI:
    """
    Null object API for mock/test mode.
    
    All methods return success but do nothing.
    Use this instead of if mock_mode checks throughout the code.
    """
    
    def status(self) -> Optional[dict]:
        return None
    
    def play(self, uri: str, skip_to_uri: str = None, paused: bool = False) -> Optional[bool]:
        return True
    
    def pause(self) -> bool:
        return True
    
    def resume(self) -> bool:
        return True
    
    def next(self) -> bool:
        return True
    
    def prev(self) -> bool:
        return True
    
    def seek(self, position: int) -> bool:
        return True
    
    def set_volume(self, level: int) -> bool:
        return True

    def set_repeat_context(self, enabled: bool) -> bool:
        return True
    
    def is_connected(self) -> bool:
        return True

    def metrics_snapshot(self) -> dict:
        return {'suppressed': {}, 'failures': {}}
