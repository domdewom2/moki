"""
Moki Transcribe API client — Whisper speech-to-text via api.mokikids.de.
"""
import logging
import time
from pathlib import Path
from typing import Optional, Tuple, Union

import requests

from ..config import (
    MOKI_SEARCH_URL,
    MOKI_TRANSCRIBE_URL,
    MOKI_TRANSCRIBE_LANGUAGE,
    MOKI_TRANSCRIBE_TIMEOUT,
    VOICE_SEARCH_API_CONNECT_TIMEOUT,
    VOICE_SEARCH_API_READ_TIMEOUT,
)

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {502, 503, 504}
DEFAULT_RETRIES = 2
_session = requests.Session()


def _normalize_timeout(
    timeout: Optional[Union[int, Tuple[int, int]]],
) -> Tuple[int, int]:
    if isinstance(timeout, tuple):
        return timeout
    read = timeout if timeout is not None else MOKI_TRANSCRIBE_TIMEOUT
    return (VOICE_SEARCH_API_CONNECT_TIMEOUT, read)


def parse_transcribe_response(data: Union[str, dict, list]) -> str:
    """Parse API response into plain transcript text."""
    if isinstance(data, str):
        text = data.strip()
        if len(text) >= 2 and text[0] == text[-1] == '"':
            text = text[1:-1].strip()
    elif isinstance(data, dict):
        for key in ('text', 'transcript', 'transcription', 'result'):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                text = val.strip()
                break
        else:
            text = ''
    else:
        text = str(data).strip() if data is not None else ''

    if not text:
        raise ValueError('Empty transcription response')
    return text


def probe_api(timeout: Optional[Tuple[int, int]] = None) -> bool:
    """Quick check that api.mokikids.de responds (e.g. after wake from sleep)."""
    req_timeout = timeout or (3, 5)
    try:
        response = _session.get(
            MOKI_SEARCH_URL,
            params={'q': 'ping', 'limit': 1},
            timeout=req_timeout,
        )
        return response.status_code == 200
    except requests.RequestException as e:
        logger.info(f'API probe failed: {e}')
        return False


def wait_for_api(max_wait: float = 10.0, poll_interval: float = 0.5) -> bool:
    """Poll until API responds or max_wait elapsed."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if probe_api():
            return True
        time.sleep(poll_interval)
    return False


def transcribe(
    mp3_path: Path,
    language: Optional[str] = None,
    timeout: Optional[Union[int, Tuple[int, int]]] = None,
    retries: int = DEFAULT_RETRIES,
) -> str:
    """Upload MP3 and return transcribed text."""
    path = Path(mp3_path)
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f'MP3 not found or empty: {path}')

    lang = language if language is not None else MOKI_TRANSCRIBE_LANGUAGE
    req_timeout = _normalize_timeout(timeout)
    size_kb = path.stat().st_size / 1024
    logger.info(f'Moki transcribe: {path.name} ({size_kb:.1f} KB) lang={lang} timeout={req_timeout}')

    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        started = time.time()
        try:
            with path.open('rb') as audio_file:
                files = {'file': (path.name, audio_file, 'audio/mpeg')}
                data = {}
                if lang:
                    data['language'] = lang
                response = _session.post(
                    MOKI_TRANSCRIBE_URL,
                    files=files,
                    data=data or None,
                    timeout=req_timeout,
                )
            response.raise_for_status()

            try:
                payload = response.json()
            except ValueError:
                payload = response.text

            text = parse_transcribe_response(payload)
            elapsed = time.time() - started
            logger.info(f'Moki transcribe done in {elapsed:.1f}s ({len(text)} chars)')
            return text
        except requests.HTTPError as e:
            last_error = e
            status = e.response.status_code if e.response is not None else None
            if status in RETRYABLE_STATUS_CODES and attempt < retries:
                wait = 1.0 * (attempt + 1)
                logger.warning(
                    f'Moki transcribe HTTP {status}, retry {attempt + 1}/{retries} in {wait:.0f}s'
                )
                time.sleep(wait)
                continue
            raise
        except requests.RequestException as e:
            last_error = e
            if attempt < retries:
                wait = 1.0 * (attempt + 1)
                logger.warning(
                    f'Moki transcribe network error, retry {attempt + 1}/{retries} in {wait:.0f}s: {e}'
                )
                time.sleep(wait)
                continue
            raise

    if last_error:
        raise last_error
    raise RuntimeError('Moki transcribe failed without error')
