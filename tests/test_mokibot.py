"""
Tests for MokiBot assistant API client and response parsing.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from moki.api.moki_assistant import (
    parse_assistant_response,
    probe_assistant_health,
    assistant_request,
    download_tts,
    get_device_id,
)

SAMPLE_PLAY_RESPONSE = {
    "action": "play",
    "session_id": "df842c28-db62-49af-9c30-8ece06cb20bd",
    "transcript": "Spiele lustige Musik",
    "reply_text": "Hier ist eine tolle Playlist mit lustiger Kindermusik für dich!",
    "reply_audio_url": "https://api.mokikids.de/tts/cache/29aa59504a80af3f.mp3",
    "reply_audio_mime": "audio/mpeg",
    "play": {
        "uri": "spotify:playlist:4xHoTmkj5LnzAceHBf1gpW",
        "name": "Hurra Kinderlieder - Best of",
        "artist": "Hurra Kinderlieder",
        "type": "playlist",
        "image_url": "https://image-cdn-ak.spotifycdn.com/image/ab67706c0000da84",
        "is_playable": True,
    },
    "debug": None,
}

SAMPLE_REJECT_RESPONSE = {
    "action": "reject",
    "session_id": "abc-123",
    "transcript": "Erzähl mir einen Witz über Politik",
    "reply_text": "Darüber kann ich dir leider nichts erzählen.",
    "reply_audio_url": "https://api.mokikids.de/tts/cache/deadbeef.mp3",
    "reply_audio_mime": "audio/mpeg",
    "play": None,
}


class TestParseAssistantResponse:
    def test_play_action(self):
        parsed = parse_assistant_response(SAMPLE_PLAY_RESPONSE)
        assert parsed.action == 'play'
        assert parsed.session_id == SAMPLE_PLAY_RESPONSE['session_id']
        assert parsed.transcript == 'Spiele lustige Musik'
        assert parsed.reply_text.startswith('Hier ist')
        assert parsed.reply_audio_url.endswith('.mp3')
        assert parsed.play is not None
        assert parsed.play.uri.startswith('spotify:playlist:')
        assert parsed.play.name.startswith('Hurra Kinderlieder')
        assert parsed.play.type == 'playlist'

    def test_reject_action(self):
        parsed = parse_assistant_response(SAMPLE_REJECT_RESPONSE)
        assert parsed.action == 'reject'
        assert parsed.play is None

    def test_invalid_payload_raises(self):
        with pytest.raises(ValueError):
            parse_assistant_response([])


class TestAssistantRequest:
    @patch('moki.api.moki_assistant._session.post')
    def test_posts_json_with_device_header(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = SAMPLE_PLAY_RESPONSE
        mock_post.return_value = mock_resp

        result = assistant_request(
            'Spiele lustige Musik',
            session_id='sess-1',
            device_id='mello',
        )
        assert result.action == 'play'
        mock_post.assert_called_once()
        kwargs = mock_post.call_args[1]
        assert kwargs['headers']['X-Moki-Device-Id'] == 'mello'
        assert kwargs['json']['text'] == 'Spiele lustige Musik'
        assert kwargs['json']['session_id'] == 'sess-1'
        assert kwargs['json']['context']['app'] == 'mokibot'

    @patch('moki.api.moki_assistant._session.post')
    def test_empty_text_raises(self, mock_post):
        with pytest.raises(ValueError):
            assistant_request('   ')
        mock_post.assert_not_called()


class TestProbeAssistantHealth:
    @patch('moki.api.moki_assistant._session.get')
    def test_ok_status(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'status': 'ok', 'llm': True, 'search': True, 'tts': True}
        mock_get.return_value = mock_resp
        assert probe_assistant_health() is True

    @patch('moki.api.moki_assistant._session.get')
    def test_failure_returns_false(self, mock_get):
        mock_get.side_effect = requests.Timeout('timeout')
        assert probe_assistant_health() is False


class TestDownloadTts:
    @patch('moki.api.moki_assistant._session.get')
    def test_downloads_to_dest(self, mock_get, tmp_path):
        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = [b'fake-mp3-data']
        mock_get.return_value = mock_resp

        dest = tmp_path / 'tts_last.mp3'
        path = download_tts('https://api.mokikids.de/tts/cache/test.mp3', dest)
        assert path == dest
        assert dest.read_bytes() == b'fake-mp3-data'


class TestDeviceId:
    @patch('moki.api.moki_assistant.ANALYTICS_DISTINCT_ID', 'moki-livingroom')
    def test_uses_analytics_id(self):
        assert get_device_id() == 'moki-livingroom'

    @patch('moki.api.moki_assistant.ANALYTICS_DISTINCT_ID', '')
    @patch('socket.gethostname', return_value='mello.local')
    def test_falls_back_to_hostname(self, _hostname):
        assert get_device_id() == 'mello'
