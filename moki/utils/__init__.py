"""
Moki Utilities - Shared helper functions.
"""
import sys
import atexit
import shutil
import subprocess
import logging
from concurrent.futures import ThreadPoolExecutor

from ..config import WM8960_SINK

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4)
atexit.register(_executor.shutdown, wait=False)


def run_async(fn, *args):
    """Fire-and-forget async execution in a bounded thread pool.

    Wraps function to catch and log exceptions.
    Max 4 concurrent background tasks — safe for Raspberry Pi.
    """
    def wrapper():
        try:
            fn(*args)
        except Exception as e:
            logger.warning(f'Async task {fn.__name__} failed: {e}', exc_info=True)

    _executor.submit(wrapper)


def get_runtime_version_label() -> str:
    """Return a short runtime version label from git metadata.

    Format: "<branch>@<short-hash>" (example: "main@1818997").
    Falls back to "unknown" when git metadata is unavailable.
    """
    try:
        branch_result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True, text=True, check=True, timeout=2,
        )
        branch = branch_result.stdout.strip() or 'unknown'
    except Exception:
        branch = 'unknown'

    try:
        hash_result = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, check=True, timeout=2,
        )
        short_hash = hash_result.stdout.strip() or 'unknown'
    except Exception:
        short_hash = 'unknown'

    return f'{branch}@{short_hash}'


_wm8960_card: str | None = None


def _find_wm8960_card() -> str:
    """Find the ALSA card number for the WM8960 Audio HAT."""
    global _wm8960_card
    if _wm8960_card is not None:
        return _wm8960_card
    try:
        result = subprocess.run(
            ['aplay', '-l'], capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if 'wm8960' in line.lower():
                _wm8960_card = line.split(':')[0].split()[-1]
                logger.info(f'WM8960 Audio HAT found on card {_wm8960_card}')
                return _wm8960_card
    except Exception:
        pass
    _wm8960_card = '2'
    logger.warning('WM8960 not found in aplay -l, falling back to card 2')
    return _wm8960_card


def get_wm8960_capture_device() -> str:
    """ALSA device string for WM8960 microphone capture."""
    return f'plughw:{_find_wm8960_card()},0'


def _amixer_set(card: str, *args: str) -> bool:
    try:
        subprocess.run(
            ['amixer', '-c', card, *args],
            capture_output=True,
            check=True,
            timeout=3,
        )
        return True
    except (subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug(f'amixer {" ".join(args)} failed: {e}')
        return False


_wm8960_mic_configured = False


def configure_wm8960_mic_once():
    """Enable WM8960 onboard mic routing once per process."""
    global _wm8960_mic_configured
    if _wm8960_mic_configured or sys.platform != 'linux':
        return
    card = _find_wm8960_card()
    _amixer_set(card, 'set', 'Left Input Mixer Boost', 'on')
    _amixer_set(card, 'set', 'Right Input Mixer Boost', 'on')
    _amixer_set(card, 'set', 'Left Boost Mixer LINPUT1', 'on')
    _amixer_set(card, 'set', 'Right Boost Mixer RINPUT1', 'on')
    # Softer preamp — full boost (3/3) sounds harsh on speech.
    _amixer_set(card, 'set', 'Left Input Boost Mixer LINPUT1', '2')
    _amixer_set(card, 'set', 'Right Input Boost Mixer RINPUT1', '2')
    _amixer_set(card, 'set', 'ADC High Pass Filter', 'on')
    _wm8960_mic_configured = True
    logger.info('WM8960 mic routing configured')


def prepare_wm8960_capture():
    """Tune WM8960 capture gain before voice recording."""
    if sys.platform != 'linux':
        return
    configure_wm8960_mic_once()
    card = _find_wm8960_card()
    # Loud enough but with headroom — avoids harsh clipping on consonants.
    _amixer_set(card, 'set', 'Capture', '63%')
    _amixer_set(card, 'set', 'ADC PCM', '75%')
    logger.info('WM8960 capture levels set for voice test')


def mute_wm8960_output():
    """Silence speaker output via ALSA and PipeWire before recording."""
    if sys.platform != 'linux':
        return
    card = _find_wm8960_card()
    _amixer_set(card, 'set', 'Playback', '0%')
    _amixer_set(card, 'set', 'Speaker', '0%')
    _amixer_set(card, 'set', 'Headphone', '0%')
    if shutil.which('pactl'):
        for cmd in (
            ['pactl', 'set-sink-mute', WM8960_SINK, '1'],
            ['pactl', 'set-sink-volume', WM8960_SINK, '0'],
            ['pactl', 'suspend-sink', WM8960_SINK, '1'],
        ):
            try:
                subprocess.run(cmd, capture_output=True, check=True, timeout=3)
            except (subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
                logger.debug(f'PipeWire mute step failed ({cmd[1]}): {e}')


def unmute_wm8960_output(speaker_level: int):
    """Restore speaker output after recording or voice test playback."""
    if sys.platform != 'linux':
        return
    if shutil.which('pactl'):
        for cmd in (
            ['pactl', 'suspend-sink', WM8960_SINK, '0'],
            ['pactl', 'set-sink-mute', WM8960_SINK, '0'],
            ['pactl', 'set-sink-volume', WM8960_SINK, f'{speaker_level}%'],
        ):
            try:
                subprocess.run(cmd, capture_output=True, check=True, timeout=3)
            except (subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
                logger.debug(f'PipeWire unmute step failed ({cmd[1]}): {e}')
    set_system_volume(speaker_level)


def set_system_volume(speaker_level: int):
    """Set the Pi's ALSA system volume for the speaker."""
    if sys.platform != 'linux':
        return
    try:
        card = _find_wm8960_card()
        subprocess.run(
            ['amixer', '-c', card, 'set', 'Playback', '100%'],
            capture_output=True, check=True
        )
        subprocess.run(
            ['amixer', '-c', card, 'set', 'Speaker', f'{speaker_level}%'],
            capture_output=True, check=True
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logger.debug(f'Could not set system volume: {e}')
    except Exception as e:
        logger.warning(f'Unexpected error setting system volume: {e}', exc_info=True)


def mute_speakers():
    """Silence speaker by setting volume to 0% (WM8960 has no mute switch)."""
    if sys.platform != 'linux':
        return
    try:
        card = _find_wm8960_card()
        subprocess.run(
            ['amixer', '-c', card, 'set', 'Speaker', '0%'],
            capture_output=True, check=True
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logger.debug(f'Could not mute speakers: {e}')
    except Exception as e:
        logger.warning(f'Unexpected error muting speakers: {e}', exc_info=True)


def unmute_speakers(speaker_level: int):
    """Restore speaker to given volume level."""
    if sys.platform != 'linux':
        return
    try:
        card = _find_wm8960_card()
        subprocess.run(
            ['amixer', '-c', card, 'set', 'Speaker', f'{speaker_level}%'],
            capture_output=True, check=True
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logger.debug(f'Could not unmute speakers: {e}')
    except Exception as e:
        logger.warning(f'Unexpected error unmuting speakers: {e}', exc_info=True)
