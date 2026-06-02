"""
Tests for Spotify voice search flow.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pygame_stub = SimpleNamespace(
    Surface=object,
    Rect=object,
    font=SimpleNamespace(Font=object),
)
sys.modules.setdefault('pygame', pygame_stub)
sys.modules.setdefault('pygame.gfxdraw', SimpleNamespace())

from moki.models import SearchResult, VoiceSearchPhase
from moki.ui.renderer import Renderer


class TestVoiceSearchPipeline:
    def _make_app(self):
        from moki.app import Moki

        app = Moki.__new__(Moki)
        app.catalog_manager = MagicMock()
        app.catalog_manager.items = []
        app.catalog_manager.load = MagicMock()
        app.catalog_manager.save_item = MagicMock(return_value=True)
        app.image_cache = MagicMock()
        app.image_cache.preload_catalog = MagicMock()
        app.renderer = MagicMock()
        app.renderer.invalidate = MagicMock()
        app.setup_menu = MagicMock()
        app.setup_menu.state = SimpleNamespace()
        app.setup_menu.close = MagicMock()
        app.voice_search_carousel = MagicMock()
        app.voice_search_carousel.max_index = 0
        app.voice_search_carousel.scroll_x = 0.0
        app.voice_search_carousel.set_target = MagicMock()
        app._voice_search_phase = VoiceSearchPhase.RECORDING
        app._voice_search_query = ''
        app._voice_search_results = []
        app._voice_search_error = None
        app._voice_search_generation = 0
        app._voice_search_selected_index = 0
        app._search_query = ''
        app._search_results = []
        app._search_loading = False
        app._search_error = None
        app._show_toast = MagicMock()
        app._get_cached_network_status = MagicMock(return_value=True)
        app._close_voice_search = MagicMock()
        app._open_spotify_screen = MagicMock()
        app._focus_catalog_uri_and_play = MagicMock()
        app._update_carousel_max_index = MagicMock()
        app._select_search_result = Moki._select_search_result.__get__(app, Moki)
        app._start_voice_search_pipeline = Moki._start_voice_search_pipeline.__get__(app, Moki)
        return app

    def test_pipeline_transcribe_search_results(self):
        app = self._make_app()
        sample = SearchResult(
            uri='spotify:album:abc',
            name='Hoppel Hase',
            artist='Band',
            type='album',
            preview_image='/search_cache/deadbeef.png',
        )

        with patch('moki.app.moki_transcribe.transcribe', return_value='Hoppel Hase Hans') as transcribe_mock:
            with patch('moki.app.moki_search.search', return_value=[sample]) as search_mock:
                with patch('moki.app.prefetch_covers', return_value=[sample]) as prefetch_mock:
                    with patch('moki.app.run_async', side_effect=lambda fn: fn()):
                        app._start_voice_search_pipeline()

        transcribe_mock.assert_called_once()
        search_mock.assert_called_once_with('Hoppel Hase Hans')
        prefetch_mock.assert_called_once()
        assert app._voice_search_phase == VoiceSearchPhase.RESULTS
        assert app._voice_search_query == 'Hoppel Hase Hans'
        assert len(app._voice_search_results) == 1
        assert app._voice_search_results[0].preview_image == '/search_cache/deadbeef.png'

    def test_pipeline_no_network_closes_and_toasts(self):
        app = self._make_app()
        app._get_cached_network_status = MagicMock(return_value=False)

        with patch('moki.app.run_async', side_effect=lambda fn: fn()):
            app._start_voice_search_pipeline()

        app._close_voice_search.assert_called_once()
        app._show_toast.assert_called_with('Kein Internet')

    def test_select_search_result_saves_item(self):
        app = self._make_app()
        result = SearchResult(
            uri='spotify:album:abc',
            name='Album',
            artist='Artist',
            type='album',
            image_url='https://example.com/img.png',
        )

        with patch('moki.app.run_async', side_effect=lambda fn: fn()):
            app._select_search_result(result)

        app.catalog_manager.save_item.assert_called_once()
        app._open_spotify_screen.assert_called_once()
        app._focus_catalog_uri_and_play.assert_called_once_with(result.uri)
        app._close_voice_search.assert_called_once()


class TestVoiceSearchRenderer:
    def test_controls_show_mic_on_spotify(self):
        renderer = Renderer.__new__(Renderer)
        renderer.screen = MagicMock()
        renderer.icons = {'mic': MagicMock(get_rect=MagicMock(return_value=MagicMock()))}
        renderer._draw_icon = MagicMock()
        renderer._lighten_color = lambda c, amount=0.3: c

        import moki.ui.renderer as renderer_mod
        with patch.object(renderer_mod, 'draw_aa_circle'):
            Renderer._draw_controls(
                renderer,
                is_playing=False,
                volume_index=0,
                show_mic=True,
            )

        renderer._draw_icon.assert_any_call('mic', (renderer_mod.CONTROLS_X, renderer_mod.MIC_BTN_Y))
