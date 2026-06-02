"""
Tests for Moki Transcribe API client.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from moki.api.moki_transcribe import parse_transcribe_response, transcribe


class TestParseTranscribeResponse:
    def test_plain_string(self):
        assert parse_transcribe_response('stitch hörspiel') == 'stitch hörspiel'

    def test_json_string_with_quotes(self):
        assert parse_transcribe_response('"stitch hörspiel"') == 'stitch hörspiel'

    def test_dict_text_field(self):
        assert parse_transcribe_response({'text': 'hello world'}) == 'hello world'

    def test_dict_transcript_field(self):
        assert parse_transcribe_response({'transcript': 'foo bar'}) == 'foo bar'

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_transcribe_response('')

    def test_empty_dict_raises(self):
        with pytest.raises(ValueError):
            parse_transcribe_response({})


class TestTranscribeFunction:
    def test_missing_file_raises(self, tmp_path):
        missing = tmp_path / 'missing.mp3'
        with pytest.raises(FileNotFoundError):
            transcribe(missing)

    def test_empty_file_raises(self, tmp_path):
        empty = tmp_path / 'empty.mp3'
        empty.write_bytes(b'')
        with pytest.raises(FileNotFoundError):
            transcribe(empty)

    @patch('moki.api.moki_transcribe.requests.post')
    def test_uploads_mp3(self, mock_post, tmp_path):
        mp3 = tmp_path / 'last.mp3'
        mp3.write_bytes(b'fake-mp3-data')
        mock_resp = MagicMock()
        mock_resp.json.return_value = 'stitch hörspiel'
        mock_post.return_value = mock_resp

        result = transcribe(mp3, language='de', timeout=10)
        assert result == 'stitch hörspiel'
        mock_post.assert_called_once()
        kwargs = mock_post.call_args[1]
        assert kwargs['data']['language'] == 'de'
        assert kwargs['timeout'] == 10
        assert kwargs['files']['file'][0] == 'last.mp3'

    @patch('moki.api.moki_transcribe.requests.post')
    def test_dict_response(self, mock_post, tmp_path):
        mp3 = tmp_path / 'last.mp3'
        mp3.write_bytes(b'fake-mp3-data')
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'text': 'conni hörspiel'}
        mock_post.return_value = mock_resp

        assert transcribe(mp3) == 'conni hörspiel'

    @patch('moki.api.moki_transcribe.requests.post')
    def test_http_error_propagates(self, mock_post, tmp_path):
        mp3 = tmp_path / 'last.mp3'
        mp3.write_bytes(b'fake-mp3-data')
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.HTTPError('422')
        mock_post.return_value = mock_resp

        with pytest.raises(requests.HTTPError):
            transcribe(mp3, retries=0)

    @patch('moki.api.moki_transcribe.time.sleep')
    @patch('moki.api.moki_transcribe.requests.post')
    def test_retries_on_503(self, mock_post, mock_sleep, tmp_path):
        mp3 = tmp_path / 'last.mp3'
        mp3.write_bytes(b'fake-mp3-data')
        fail_resp = MagicMock()
        fail_resp.raise_for_status.side_effect = requests.HTTPError(
            response=MagicMock(status_code=503),
        )
        ok_resp = MagicMock()
        ok_resp.json.return_value = 'stitch hörspiel'
        mock_post.side_effect = [fail_resp, ok_resp]

        assert transcribe(mp3, retries=1) == 'stitch hörspiel'
        assert mock_post.call_count == 2
        mock_sleep.assert_called_once()
