"""
Tests for Moki Search API client — response parsing and search behavior.
"""
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from moki.api.moki_search import parse_search_response, search, _parse_album


SAMPLE_RESPONSE = {
    "albums": {
        "items": [
            {
                "album_type": "album",
                "is_playable": True,
                "name": "Lilo & Stitch (Hörspiel zum Disney Film)",
                "uri": "spotify:album:1ndJTI6WXZDq1laxYLIdlY",
                "images": [{"url": "https://i.scdn.co/image/album1"}],
                "artists": [{"name": "Lilo & Stitch"}],
            },
            {
                "album_type": "album",
                "is_playable": False,
                "name": "Not Playable Album",
                "uri": "spotify:album:deadbeef",
                "images": [],
                "artists": [{"name": "Nobody"}],
            },
            {
                "album_type": "album",
                "is_playable": True,
                "name": "Zoomania 2 (Hörspiel zum Disney Film)",
                "uri": "spotify:album:67a3h3krPWhsRNuzLPbaBO",
                "images": [{"url": "https://i.scdn.co/image/album3"}],
                "artists": [{"name": "Zoomania"}],
            },
        ]
    },
    "playlists": {
        "items": [
            {
                "name": "Lilo & Stitch - Die Original-Hörspiele",
                "uri": "spotify:playlist:5MERucgCLoZpHYYSZ31upC",
                "images": [{"url": "https://image-cdn-fa.spotifycdn.com/image/pl1"}],
                "owner": {"display_name": "Disney Hörspiele"},
            },
            {
                "name": "Stitch Hörspiel ",
                "uri": "spotify:playlist:2GHcQFFJTiw22qdpoZRZyz",
                "images": [{"url": "https://image-cdn-ak.spotifycdn.com/image/pl2"}],
                "owner": {"display_name": "Lisa"},
            },
            {
                "name": "Conni - Alle Hörspiele",
                "uri": "spotify:playlist:5IuQJSuG8ja7jT7w3WMTz1",
                "images": [{"url": "https://image-cdn-ak.spotifycdn.com/image/pl3"}],
                "owner": {"display_name": "Conni2017"},
            },
        ]
    },
}


class TestParseSearchResponse:
    def test_parses_albums_and_playlists(self):
        results = parse_search_response(SAMPLE_RESPONSE)
        assert len(results) == 5  # 2 playable albums + 3 playlists

    def test_albums_before_playlists(self):
        results = parse_search_response(SAMPLE_RESPONSE)
        album_indices = [i for i, r in enumerate(results) if r.type == 'album']
        playlist_indices = [i for i, r in enumerate(results) if r.type == 'playlist']
        assert album_indices and playlist_indices
        assert max(album_indices) < min(playlist_indices)

    def test_album_fields(self):
        results = parse_search_response(SAMPLE_RESPONSE)
        stitch = next(r for r in results if r.uri == 'spotify:album:1ndJTI6WXZDq1laxYLIdlY')
        assert stitch.name.startswith('Lilo & Stitch')
        assert stitch.artist == 'Lilo & Stitch'
        assert stitch.type == 'album'
        assert stitch.image_url == 'https://i.scdn.co/image/album1'

    def test_playlist_uses_owner_as_artist(self):
        results = parse_search_response(SAMPLE_RESPONSE)
        pl = next(r for r in results if r.uri == 'spotify:playlist:5MERucgCLoZpHYYSZ31upC')
        assert pl.type == 'playlist'
        assert pl.artist == 'Disney Hörspiele'

    def test_filters_non_playable_albums(self):
        assert _parse_album(SAMPLE_RESPONSE['albums']['items'][1]) is None

    def test_empty_response(self):
        assert parse_search_response({}) == []
        assert parse_search_response({'albums': {'items': []}, 'playlists': {'items': []}}) == []


class TestSearchFunction:
    def test_empty_query_returns_empty(self):
        assert search('') == []
        assert search('   ') == []

    @patch('moki.api.moki_search.requests.get')
    def test_search_calls_api(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = SAMPLE_RESPONSE
        mock_get.return_value = mock_resp

        results = search('stitch hörspiel', limit=3)
        assert len(results) == 5
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args
        assert call_kwargs[1]['params']['q'] == 'stitch hörspiel'
        assert call_kwargs[1]['params']['limit'] == 3

    @patch('moki.api.moki_search.requests.get')
    def test_search_http_error_propagates(self, mock_get):
        mock_get.side_effect = requests.HTTPError('500')
        with pytest.raises(requests.HTTPError):
            search('test')


class TestMusicSearchMenu:
    def test_main_menu_has_music_search_button(self):
        from types import SimpleNamespace
        from moki.ui.renderer import Renderer

        renderer = Renderer.__new__(Renderer)
        renderer.font_medium = MagicMock(return_value=MagicMock())
        renderer.font_large = MagicMock(return_value=MagicMock())
        ctx = SimpleNamespace(
            auto_pause_minutes=30,
            progress_expiry_hours=96,
            update_running=False,
            update_checking=False,
            update_available=False,
            reboot_confirm_pending=False,
            shutdown_confirm_pending=False,
            reset_confirm_pending=False,
            app_version_label='',
        )
        items = Renderer._build_main_content(renderer, ctx)
        button_ids = [item[1] for item in items if item[0] == 'button']
        assert 'music_search' in button_ids
