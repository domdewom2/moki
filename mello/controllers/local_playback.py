"""
Local MP3 playback via mpv IPC (parallel to go-librespot).
"""
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from ..config import MPV_SOCKET_PATH, WM8960_SINK

MPV_AUDIO_DEVICE = f'pipewire/{WM8960_SINK}'

logger = logging.getLogger(__name__)


class LocalPlaybackController:
    """Play cached MP3 files through mpv with JSON IPC."""

    def __init__(
        self,
        mock_mode: bool = False,
        on_state_changed: Optional[Callable[[], None]] = None,
        on_stopped: Optional[Callable[[str, int, int, str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ):
        self.mock_mode = mock_mode
        self._on_state_changed = on_state_changed or (lambda: None)
        self._on_stopped = on_stopped or (lambda uri, pos, dur, name: None)
        self._on_error = on_error or (lambda msg: None)
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop = threading.Event()
        self._playing = False
        self._paused = False
        self._context_uri: Optional[str] = None
        self._track_name: str = ''
        self._position_ms: int = 0
        self._duration_ms: int = 0
        self._mock_timer_start: float = 0.0
        self._mpv_path: Optional[str] = None if mock_mode else shutil.which('mpv')
        self._mpv_unavailable: bool = False
        self._mpv_lock = threading.Lock()
        self._warm_in_progress: bool = False

    def warm_up(self):
        """Start mpv in background so play is instant when user taps."""
        if self.mock_mode or self._mpv_unavailable or not self._mpv_path:
            return
        with self._mpv_lock:
            if self._ipc_ready() or self._warm_in_progress:
                return
            self._warm_in_progress = True

        def _do():
            try:
                ok = self._ensure_mpv()
                logger.info(f'mpv warm-up {"ok" if ok else "failed"}')
            finally:
                self._warm_in_progress = False

        threading.Thread(target=_do, daemon=True, name='mpv-warmup').start()

    @property
    def player_available(self) -> bool:
        return self.mock_mode or (self._mpv_path is not None and not self._mpv_unavailable)

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._playing or self._paused

    @property
    def context_uri(self) -> Optional[str]:
        with self._lock:
            return self._context_uri

    def get_state(self) -> tuple[bool, bool, int, int, Optional[str], str]:
        """Return playing, paused, position_ms, duration_ms, context_uri, track_name."""
        with self._lock:
            return (
                self._playing,
                self._paused,
                self._position_ms,
                self._duration_ms,
                self._context_uri,
                self._track_name,
            )

    def play(
        self,
        path: Path,
        context_uri: str,
        track_name: str,
        start_position_ms: int = 0,
        duration_ms: int = 0,
    ) -> bool:
        if self.mock_mode:
            return self._mock_play(context_uri, track_name, start_position_ms, duration_ms)

        if not path.exists():
            logger.warning(f'Local play missing file: {path}')
            self._on_error('Datei nicht gefunden')
            return False

        if not self._ensure_mpv():
            return False

        self.stop(save_progress=False)
        seek_sec = max(0.0, start_position_ms / 1000.0)
        ok = self._send_command(['loadfile', str(path), 'replace'])
        if not ok:
            self._on_error('Wiedergabe fehlgeschlagen')
            return False
        if seek_sec > 0:
            self._send_command(['seek', seek_sec, 'absolute'])
        self._send_command(['set_property', 'pause', False])
        with self._lock:
            self._playing = True
            self._paused = False
            self._context_uri = context_uri
            self._track_name = track_name
            self._position_ms = start_position_ms
            self._duration_ms = duration_ms
        self._start_poll_thread()
        self._on_state_changed()
        logger.info(f'Local play started: {track_name} @ {seek_sec:.0f}s')
        return True

    def pause(self):
        if self.mock_mode:
            with self._lock:
                if self._playing:
                    self._playing = False
                    self._paused = True
            self._on_state_changed()
            return
        self._send_command(['set_property', 'pause', True])
        with self._lock:
            self._playing = False
            self._paused = True
        self._on_state_changed()

    def resume(self):
        if self.mock_mode:
            with self._lock:
                if self._paused:
                    self._playing = True
                    self._paused = False
                    self._mock_timer_start = time.time() - (self._position_ms / 1000.0)
            self._on_state_changed()
            return
        self._send_command(['set_property', 'pause', False])
        with self._lock:
            self._playing = True
            self._paused = False
        self._on_state_changed()

    def toggle_play(self) -> bool:
        playing, paused, _, _, _, _ = self.get_state()
        if playing:
            self.pause()
            return False
        if paused:
            self.resume()
            return True
        return False

    def seek_relative(self, delta_seconds: int) -> bool:
        """Seek by delta_seconds relative to current position."""
        if self.mock_mode:
            with self._lock:
                if not (self._playing or self._paused):
                    return False
                new_ms = self._position_ms + delta_seconds * 1000
                if self._duration_ms > 0:
                    new_ms = max(0, min(new_ms, self._duration_ms))
                else:
                    new_ms = max(0, new_ms)
                self._position_ms = new_ms
            self._on_state_changed()
            return True
        if not self.is_active:
            return False
        ok = self._send_command(['seek', delta_seconds, 'relative'])
        if ok:
            self._on_state_changed()
        return ok

    def stop(self, save_progress: bool = True):
        playing, paused, position_ms, duration_ms, context_uri, track_name = self.get_state()
        if save_progress and context_uri and (playing or paused) and position_ms > 0:
            self._on_stopped(context_uri, position_ms, duration_ms, track_name)

        self._poll_stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=1.0)
        self._poll_thread = None
        self._poll_stop.clear()

        if not self.mock_mode and self._process and self._process.poll() is None:
            self._send_command(['stop'])
        with self._lock:
            self._playing = False
            self._paused = False
            self._context_uri = None
            self._track_name = ''
            self._position_ms = 0
            self._duration_ms = 0
        self._on_state_changed()

    def shutdown(self):
        self.stop(save_progress=True)
        self._kill_mpv()

    def _mock_play(
        self,
        context_uri: str,
        track_name: str,
        start_position_ms: int,
        duration_ms: int,
    ) -> bool:
        with self._lock:
            self._playing = True
            self._paused = False
            self._context_uri = context_uri
            self._track_name = track_name
            self._position_ms = start_position_ms
            self._duration_ms = duration_ms or 60000
            self._mock_timer_start = time.time() - (start_position_ms / 1000.0)
        self._start_poll_thread()
        self._on_state_changed()
        return True

    def _ipc_ready(self) -> bool:
        if not os.path.exists(MPV_SOCKET_PATH):
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(MPV_SOCKET_PATH)
            sock.close()
            return True
        except OSError:
            return False

    def _kill_mpv(self):
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
                self._process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._process.kill()
                except OSError:
                    pass
        self._process = None
        if os.path.exists(MPV_SOCKET_PATH):
            try:
                os.unlink(MPV_SOCKET_PATH)
            except OSError:
                pass

    def _ensure_mpv(self) -> bool:
        if self.mock_mode:
            return True
        if self._mpv_unavailable or not self._mpv_path:
            if not self._mpv_unavailable:
                self._mpv_unavailable = True
                logger.error('mpv not installed — CheckPod playback unavailable')
                self._on_error('Player fehlt — bitte Update ausführen')
            return False

        with self._mpv_lock:
            if self._process and self._process.poll() is None:
                if self._ipc_ready():
                    return True
                logger.warning('mpv running but IPC not ready — restarting')
                self._kill_mpv()

            if os.path.exists(MPV_SOCKET_PATH):
                try:
                    os.unlink(MPV_SOCKET_PATH)
                except OSError:
                    pass

            cmd = [
                self._mpv_path,
                '--no-video',
                '--idle=yes',
                '--keep-open=yes',
                '--pause',
                f'--input-ipc-server={MPV_SOCKET_PATH}',
                '--audio-display=no',
                '--ao=pipewire',
                f'--audio-device={MPV_AUDIO_DEVICE}',
                '--audio-client-name=mello-checkpod',
            ]
            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                deadline = time.time() + 15.0
                while time.time() < deadline:
                    if self._process.poll() is not None:
                        err = (self._process.stderr.read() or b'').decode('utf-8', errors='replace').strip()
                        logger.error(f'mpv exited early: {err[:500]}')
                        self._process = None
                        self._on_error('Player startet nicht')
                        return False
                    if self._ipc_ready():
                        logger.info(f'mpv IPC ready (audio={MPV_AUDIO_DEVICE})')
                        return True
                    time.sleep(0.1)
                err = ''
                if self._process.stderr:
                    err = self._process.stderr.read().decode('utf-8', errors='replace').strip()
                logger.warning(f'mpv IPC socket not ready after 15s | stderr={err[:500]}')
                self._kill_mpv()
                self._on_error('Player startet nicht')
                return False
            except OSError as e:
                logger.error(f'Failed to start mpv: {e}')
                self._mpv_unavailable = True
                self._on_error('Player fehlt — bitte Update ausführen')
                self._process = None
                return False

    def _send_command(self, command: list) -> bool:
        if self.mock_mode:
            return True
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(MPV_SOCKET_PATH)
            payload = json.dumps({'command': command}) + '\n'
            sock.sendall(payload.encode('utf-8'))
            sock.close()
            return True
        except OSError as e:
            logger.warning(f'mpv IPC command failed {command}: {e}')
            return False

    def _query_property(self, name: str):
        if self.mock_mode:
            return None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(MPV_SOCKET_PATH)
            sock.sendall(json.dumps({'command': ['get_property', name]}).encode('utf-8') + b'\n')
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b'\n' in chunk:
                    break
            sock.close()
            line = b''.join(chunks).split(b'\n', 1)[0]
            data = json.loads(line.decode('utf-8'))
            return data.get('data')
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.debug(f'mpv query {name} failed: {e}')
            return None

    def _start_poll_thread(self):
        self._poll_stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=0.5)
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def _poll_loop(self):
        while not self._poll_stop.is_set():
            if self.mock_mode:
                with self._lock:
                    if self._playing and self._duration_ms > 0:
                        elapsed = time.time() - self._mock_timer_start
                        self._position_ms = min(int(elapsed * 1000), self._duration_ms)
                        if self._position_ms >= self._duration_ms:
                            self._playing = False
                            self._paused = False
                self._on_state_changed()
                time.sleep(0.5)
                continue

            idle_active = self._query_property('idle-active')
            if idle_active is True:
                playing, paused, position_ms, duration_ms, context_uri, track_name = self.get_state()
                if context_uri and (playing or paused) and position_ms > 0:
                    self._on_stopped(context_uri, position_ms, duration_ms, track_name)
                with self._lock:
                    had_context = self._context_uri is not None
                    self._playing = False
                    self._paused = False
                    self._context_uri = None
                    self._track_name = ''
                    self._position_ms = 0
                    self._duration_ms = 0
                if had_context:
                    logger.info('Local playback ended (mpv idle)')
                self._on_state_changed()
                time.sleep(0.5)
                continue

            paused = self._query_property('pause')
            time_pos = self._query_property('time-pos')
            duration = self._query_property('duration')
            with self._lock:
                if paused is not None:
                    self._paused = bool(paused)
                    self._playing = not self._paused and self._context_uri is not None
                if time_pos is not None:
                    self._position_ms = max(0, int(float(time_pos) * 1000))
                if duration is not None and float(duration) > 0:
                    self._duration_ms = int(float(duration) * 1000)
            self._on_state_changed()
            time.sleep(0.5)
