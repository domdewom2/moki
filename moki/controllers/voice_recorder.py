"""
Voice message test recorder — arecord to WAV, then lame to MP3.
"""
import logging
import math
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from ..config import (
    VOICE_TEST_LAST_PATH,
    VOICE_TEST_MAX_SECONDS,
    VOICE_TEST_MP3_BITRATE,
    VOICE_TEST_LAME_QUALITY,
    VOICE_TEST_SAMPLE_RATE,
)
from ..utils import (
    get_wm8960_capture_device,
    mute_wm8960_output,
    prepare_wm8960_capture,
    unmute_wm8960_output,
)

logger = logging.getLogger(__name__)

CAPTURE_SETTLE_SECONDS = 0.6
ARECORD_BUSY_RETRIES = 3


class VoiceRecorderController:
    """Record short voice clips to last.mp3 via arecord + lame."""

    def __init__(
        self,
        mock_mode: bool = False,
        output_path: Path = VOICE_TEST_LAST_PATH,
        on_state_changed: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        get_speaker_level: Optional[Callable[[], int]] = None,
        on_before_record: Optional[Callable[[], None]] = None,
        should_restore_output: Optional[Callable[[], bool]] = None,
        on_recording_complete: Optional[Callable[[], None]] = None,
        on_capture_ready: Optional[Callable[[], None]] = None,
        auto_start_capture: bool = True,
        max_seconds: int = VOICE_TEST_MAX_SECONDS,
        mp3_bitrate: int = VOICE_TEST_MP3_BITRATE,
    ):
        self.mock_mode = mock_mode
        self.output_path = output_path
        self._max_seconds = max_seconds
        self._mp3_bitrate = mp3_bitrate
        self._auto_start_capture = auto_start_capture
        self._on_capture_ready = on_capture_ready or (lambda: None)
        self._on_state_changed = on_state_changed or (lambda: None)
        self._on_error = on_error or (lambda msg: None)
        self._get_speaker_level = get_speaker_level or (lambda: 88)
        self._on_before_record = on_before_record or (lambda: None)
        self._should_restore_output = should_restore_output or (lambda: True)
        self._on_recording_complete = on_recording_complete or (lambda: None)
        self._lock = threading.Lock()
        self._arecord_proc: Optional[subprocess.Popen] = None
        self._recording = False
        self._encoding = False
        self._preparing = False
        self._recording_started_at: float = 0.0
        self._encode_thread: Optional[threading.Thread] = None
        self._start_thread: Optional[threading.Thread] = None
        self._speaker_level_saved: int = 88
        self._awaiting_capture = False
        self._pending_temp_wav: Optional[Path] = None
        self._pending_device: Optional[str] = None

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    @property
    def is_encoding(self) -> bool:
        with self._lock:
            return self._encoding

    @property
    def is_preparing(self) -> bool:
        with self._lock:
            return self._preparing

    @property
    def recording_elapsed(self) -> int:
        with self._lock:
            if not self._recording:
                return 0
            return int(time.time() - self._recording_started_at)

    def has_recording(self) -> bool:
        try:
            return self.output_path.is_file() and self.output_path.stat().st_size > 0
        except OSError:
            return False

    def tick(self):
        """Auto-stop when max duration reached."""
        if not self.is_recording:
            return
        if self.recording_elapsed >= self._max_seconds:
            logger.info('Voice test: max duration reached, stopping')
            self.stop_recording()

    def toggle_recording(self) -> bool:
        if self.is_recording:
            return self.stop_recording()
        if self.is_encoding or self.is_preparing:
            return False
        return self.begin_recording()

    def begin_recording(self) -> bool:
        """Show preparing state immediately, run capture setup in background."""
        if self.mock_mode:
            if not self._auto_start_capture:
                with self._lock:
                    self._preparing = False
                    self._awaiting_capture = True
                self._notify()
                self._on_capture_ready()
                return True
            with self._lock:
                self._recording = True
                self._recording_started_at = time.time()
            self._notify()
            return True

        with self._lock:
            if self._preparing or self._recording or self._encoding:
                return False
            self._preparing = True
        logger.info('Voice test preparing')
        self._notify()

        def _start_worker():
            try:
                if not self._start_recording_sync():
                    with self._lock:
                        self._preparing = False
                    self._notify()
            except Exception as e:
                logger.warning(f'Voice test prepare failed: {e}', exc_info=True)
                with self._lock:
                    self._preparing = False
                self._on_error('Aufnahme fehlgeschlagen')
                self._notify()

        self._start_thread = threading.Thread(target=_start_worker, daemon=True, name='voice-start')
        self._start_thread.start()
        return True

    def _start_recording_sync(self) -> bool:
        if sys.platform != 'linux':
            self._on_error('Aufnahme nur auf dem Pi')
            return False
        if not shutil.which('arecord') or not shutil.which('lame'):
            self._on_error('arecord oder lame fehlt')
            return False

        self._wait_for_encode_thread()
        self._cleanup_stale_processes()
        self._on_before_record()

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_wav = self.output_path.with_suffix('.wav.tmp')
        temp_out = self.output_path.with_suffix('.mp3.tmp')
        for path in (temp_wav, temp_out):
            if path.exists():
                path.unlink()

        self._speaker_level_saved = self._get_speaker_level()
        mute_wm8960_output()
        prepare_wm8960_capture()
        time.sleep(CAPTURE_SETTLE_SECONDS)

        device = get_wm8960_capture_device()

        if not self._auto_start_capture:
            with self._lock:
                self._preparing = False
                self._awaiting_capture = True
                self._pending_temp_wav = temp_wav
                self._pending_device = device
            logger.info(f'Voice capture ready (device={device}), awaiting countdown')
            self._notify()
            self._on_capture_ready()
            return True

        if not self._launch_arecord(temp_wav, device):
            if self._should_restore_output():
                unmute_wm8960_output(self._speaker_level_saved)
            return False
        return True

    def start_capture(self) -> bool:
        """Start arecord after mic is prepared (used for voice-search countdown)."""
        with self._lock:
            if not self._awaiting_capture or self._recording:
                return False
            temp_wav = self._pending_temp_wav
            device = self._pending_device
            self._awaiting_capture = False
            self._pending_temp_wav = None
            self._pending_device = None

        if self.mock_mode:
            with self._lock:
                self._recording = True
                self._recording_started_at = time.time()
            logger.info('Voice test recording started (mock)')
            self._notify()
            return True

        if not temp_wav or not device:
            self._on_error('Aufnahme fehlgeschlagen')
            return False
        return self._launch_arecord(temp_wav, device)

    def _launch_arecord(self, temp_wav: Path, device: str) -> bool:
        arecord = None
        last_err = ''
        for attempt in range(ARECORD_BUSY_RETRIES):
            try:
                arecord = subprocess.Popen(
                    [
                        'arecord',
                        '-D', device,
                        '-f', 'S16_LE',
                        '-r', str(VOICE_TEST_SAMPLE_RATE),
                        '-c', '1',
                        '-t', 'wav',
                        str(temp_wav),
                    ],
                    stderr=subprocess.PIPE,
                )
                break
            except OSError as e:
                last_err = str(e)
                logger.warning(f'Voice test arecord attempt {attempt + 1} failed: {e}')
                time.sleep(0.3)
        if arecord is None:
            logger.warning(f'Voice test start failed: {last_err}', exc_info=True)
            self._on_error('Aufnahme fehlgeschlagen')
            return False

        with self._lock:
            self._arecord_proc = arecord
            self._preparing = False
            self._recording = True
            self._encoding = False
            self._recording_started_at = time.time()
        logger.info(f'Voice test recording started (device={device})')
        self._notify()
        return True

    def stop_recording(self) -> bool:
        with self._lock:
            if not self._recording:
                return False
            arecord = self._arecord_proc
            self._arecord_proc = None
            self._recording = False
            self._encoding = True

        if self.mock_mode:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_bytes(b'mock-mp3')
            with self._lock:
                self._encoding = False
            self._notify()
            self._on_recording_complete()
            return True

        logger.info('Voice test stopping')
        self._notify()

        def _finish():
            temp_wav = self.output_path.with_suffix('.wav.tmp')
            temp_out = self.output_path.with_suffix('.mp3.tmp')
            try:
                self._stop_arecord(arecord)
                if not temp_wav.exists() or temp_wav.stat().st_size < 1000:
                    err = ''
                    if arecord and arecord.stderr:
                        err = arecord.stderr.read().decode(errors='replace').strip()
                    logger.warning(f'Voice test WAV missing/too small: {err}')
                    self._on_error('Aufnahme leer oder fehlgeschlagen')
                    return

                rms, peak = _wav_stats(temp_wav)
                logger.info(f'Voice test WAV stats: rms={rms:.0f} peak={peak}')
                if peak > 28000 or rms > 8000:
                    logger.warning(
                        f'Voice test WAV looks noisy (rms={rms:.0f}, peak={peak}) — '
                        'possible speaker feedback during capture'
                    )

                result = subprocess.run(
                    [
                        'lame',
                        '-b', str(self._mp3_bitrate),
                        '-q', str(VOICE_TEST_LAME_QUALITY),
                        '--noreplaygain',
                        str(temp_wav),
                        str(temp_out),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    logger.warning(
                        f'lame exited {result.returncode}: '
                        f'{result.stderr.strip() or result.stdout.strip()}'
                    )
                    self._on_error('Speichern fehlgeschlagen')
                    return

                if temp_out.exists() and temp_out.stat().st_size > 0:
                    temp_out.replace(self.output_path)
                    logger.info(f'Voice test saved {self.output_path}')
                    self._on_recording_complete()
                else:
                    self._on_error('Aufnahme leer oder fehlgeschlagen')
            except (OSError, subprocess.SubprocessError) as e:
                logger.warning(f'Voice test encode failed: {e}', exc_info=True)
                self._on_error('Speichern fehlgeschlagen')
            finally:
                temp_wav.unlink(missing_ok=True)
                if temp_out.exists() and not self.has_recording():
                    temp_out.unlink(missing_ok=True)
                if self._should_restore_output():
                    unmute_wm8960_output(self._speaker_level_saved)
                with self._lock:
                    self._encoding = False
                self._notify()

        self._encode_thread = threading.Thread(target=_finish, daemon=True, name='voice-encode')
        self._encode_thread.start()
        return True

    def cancel(self):
        """Stop recording without keeping partial file."""
        with self._lock:
            arecord = self._arecord_proc
            self._arecord_proc = None
            self._recording = False
            self._encoding = False
            self._preparing = False
            self._awaiting_capture = False
            self._pending_temp_wav = None
            self._pending_device = None
        temp_wav = self.output_path.with_suffix('.wav.tmp')
        temp_out = self.output_path.with_suffix('.mp3.tmp')
        self._stop_arecord(arecord, force=True)
        temp_wav.unlink(missing_ok=True)
        temp_out.unlink(missing_ok=True)
        if self._should_restore_output():
            unmute_wm8960_output(self._speaker_level_saved)
        self._notify()

    def _stop_arecord(self, arecord: Optional[subprocess.Popen], force: bool = False):
        if not arecord or arecord.poll() is not None:
            return
        try:
            if force:
                arecord.kill()
            else:
                arecord.send_signal(signal.SIGINT)
            arecord.wait(timeout=3)
        except subprocess.TimeoutExpired:
            arecord.kill()
            arecord.wait(timeout=1)

    def _cleanup_stale_processes(self):
        with self._lock:
            arecord = self._arecord_proc
            self._arecord_proc = None
            self._recording = False
        self._stop_arecord(arecord, force=True)
        self.output_path.with_suffix('.wav.tmp').unlink(missing_ok=True)
        self.output_path.with_suffix('.mp3.tmp').unlink(missing_ok=True)

    def _wait_for_encode_thread(self, timeout: float = 15.0):
        thread = self._encode_thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def _notify(self):
        self._on_state_changed()


def _wav_stats(path: Path) -> tuple[float, int]:
    try:
        with path.open('rb') as handle:
            handle.read(44)
            data = handle.read()
        if len(data) < 2:
            return 0.0, 0
        samples = struct.unpack('<' + 'h' * (len(data) // 2), data)
        if not samples:
            return 0.0, 0
        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
        peak = max(abs(sample) for sample in samples)
        return rms, peak
    except (OSError, struct.error):
        return 0.0, 0
