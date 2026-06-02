"""
Moki Assistant API client — voice music assistant via api.mokikids.de.
"""
import logging
import socket
import time
from pathlib import Path
from typing import Optional, Tuple, Union

import requests

from ..config import (
    ANALYTICS_DISTINCT_ID,
    MOKI_ASSISTANT_HEALTH_URL,
    MOKI_ASSISTANT_TIMEOUT,
    MOKI_ASSISTANT_URL,
    MOKIBOT_CONTEXT_APP,
    MOKIBOT_TIMEZONE,
)
from ..models import AssistantResponse, SearchResult

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {502, 503, 504}
DEFAULT_RETRIES = 2
_session = requests.Session()


def get_device_id() -> str:
    """Device id for X-Moki-Device-Id header."""
    if ANALYTICS_DISTINCT_ID:
        return ANALYTICS_DISTINCT_ID
    return socket.gethostname().split('.')[0]


def _parse_play_object(data: dict) -> Optional[SearchResult]:
    if not isinstance(data, dict):
        return None
    uri = (data.get('uri') or '').strip()
    name = (data.get('name') or '').strip()
    if not uri or not name:
        return None
    item_type = (data.get('type') or 'playlist').strip() or 'playlist'
    return SearchResult(
        uri=uri,
        name=name,
        artist=(data.get('artist') or '').strip(),
        type=item_type,
        image_url=data.get('image_url') or data.get('image'),
        is_playable=bool(data.get('is_playable', True)),
    )


def parse_assistant_response(data: dict) -> AssistantResponse:
    """Parse raw /assistant JSON into AssistantResponse."""
    if not isinstance(data, dict):
        raise ValueError('Assistant response must be a JSON object')

    action = (data.get('action') or 'error').strip().lower()
    play_raw = data.get('play')
    play = _parse_play_object(play_raw) if isinstance(play_raw, dict) else None

    return AssistantResponse(
        action=action,
        session_id=data.get('session_id'),
        transcript=(data.get('transcript') or '').strip(),
        reply_text=(data.get('reply_text') or '').strip(),
        reply_audio_url=data.get('reply_audio_url'),
        reply_audio_mime=data.get('reply_audio_mime'),
        play=play,
    )


def probe_assistant_health(timeout: Optional[Tuple[int, int]] = None) -> bool:
    """Quick check that /assistant/health reports ok."""
    req_timeout = timeout or (3, 5)
    try:
        response = _session.get(MOKI_ASSISTANT_HEALTH_URL, timeout=req_timeout)
        response.raise_for_status()
        payload = response.json()
        return isinstance(payload, dict) and payload.get('status') == 'ok'
    except (requests.RequestException, ValueError) as e:
        logger.info(f'Assistant health probe failed: {e}')
        return False


def assistant_request(
    text: str,
    *,
    session_id: Optional[str] = None,
    device_id: Optional[str] = None,
    timeout: Optional[Union[int, Tuple[int, int]]] = None,
    retries: int = DEFAULT_RETRIES,
) -> AssistantResponse:
    """Send transcribed text to POST /assistant."""
    query = (text or '').strip()
    if not query:
        raise ValueError('Assistant request text is empty')

    req_timeout = timeout if timeout is not None else MOKI_ASSISTANT_TIMEOUT
    device = device_id or get_device_id()
    body = {
        'text': query,
        'context': {
            'app': MOKIBOT_CONTEXT_APP,
            'timezone': MOKIBOT_TIMEZONE,
        },
    }
    if session_id:
        body['session_id'] = session_id

    headers = {'X-Moki-Device-Id': device}
    logger.info(
        f'Moki assistant: text="{query[:60]}" device={device} session={session_id or "new"}'
    )

    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        started = time.time()
        try:
            response = _session.post(
                MOKI_ASSISTANT_URL,
                json=body,
                headers=headers,
                timeout=req_timeout,
            )
            response.raise_for_status()
            parsed = parse_assistant_response(response.json())
            elapsed = time.time() - started
            logger.info(
                f'Moki assistant done in {elapsed:.1f}s action={parsed.action} '
                f'reply_len={len(parsed.reply_text)}'
            )
            return parsed
        except requests.HTTPError as e:
            last_error = e
            status = e.response.status_code if e.response is not None else None
            if status in RETRYABLE_STATUS_CODES and attempt < retries:
                wait = 1.0 * (attempt + 1)
                logger.warning(
                    f'Moki assistant HTTP {status}, retry {attempt + 1}/{retries} in {wait:.0f}s'
                )
                time.sleep(wait)
                continue
            raise
        except requests.RequestException as e:
            last_error = e
            if attempt < retries:
                wait = 1.0 * (attempt + 1)
                logger.warning(
                    f'Moki assistant network error, retry {attempt + 1}/{retries} in {wait:.0f}s: {e}'
                )
                time.sleep(wait)
                continue
            raise

    if last_error:
        raise last_error
    raise RuntimeError('Moki assistant request failed without error')


def download_tts(
    url: str,
    dest_path: Path,
    timeout: Optional[Union[int, Tuple[int, int]]] = None,
) -> Path:
    """Download TTS MP3 from reply_audio_url to dest_path."""
    if not url:
        raise ValueError('TTS download URL is empty')

    path = Path(dest_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    req_timeout = timeout if timeout is not None else MOKI_ASSISTANT_TIMEOUT
    logger.info(f'Moki assistant TTS download: {url} -> {path.name}')

    response = _session.get(url, timeout=req_timeout, stream=True)
    response.raise_for_status()

    tmp_path = path.with_suffix(path.suffix + '.part')
    with tmp_path.open('wb') as handle:
        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                handle.write(chunk)
    tmp_path.replace(path)

    size_kb = path.stat().st_size / 1024
    logger.info(f'Moki assistant TTS saved ({size_kb:.1f} KB)')
    return path
