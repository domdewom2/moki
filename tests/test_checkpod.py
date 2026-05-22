"""
Tests for CheckPod (Checker Tobi) integration.
"""
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pygame_stub = types.ModuleType('pygame')
pygame_stub.Surface = object
pygame_stub.Rect = object
pygame_stub.font = SimpleNamespace(Font=object)
sys.modules.setdefault('pygame', pygame_stub)
sys.modules.setdefault('pygame.gfxdraw', types.ModuleType('pygame.gfxdraw'))

from mello.api.ard_audiothek import parse_episodes
from mello.app import Mello
from mello.models import AppScreen, CatalogItem, NowPlaying
from mello.managers.checkpod_manager import (
    CheckPodManager,
    _fit_cover_to_square,
    _fit_width_letterbox,
    _prepare_checkpod_cover,
)
from mello.controllers.local_playback import LocalPlaybackController
from PIL import Image


SAMPLE_GRAPHQL_RESPONSE = {
    'data': {
        'result': {
            'title': 'CheckPod - Der Podcast mit Checker Tobi',
            'items': {
                'nodes': [
                    {
                        'id': '16407475',
                        'title': 'Reis | Von langen und runden Körnern',
                        'duration': 1519,
                        'publishDate': '2026-05-01',
                        'image': {'url': 'https://example.com/cover?w={width}'},
                        'audios': [
                            {'url': 'https://cdn.example.com/ep1_1.mp4'},
                            {'url': 'https://cdn.example.com/ep1_2.mp3'},
                        ],
                    }
                ]
            },
        }
    }
}


def test_fit_cover_to_square_preserves_full_image():
    from mello.config import COLORS
    wide = Image.new('RGBA', (400, 225), (255, 0, 0, 255))
    result = _fit_cover_to_square(wide, 410)
    assert result.size == (410, 410)
    # Letterbox: top/bottom corners are background for landscape art
    assert result.getpixel((205, 5))[:3] == COLORS['bg_primary']
    assert result.getpixel((205, 205))[0] == 255


def test_fit_width_letterbox_preserves_full_width():
    from mello.config import COLORS
    wide = Image.new('RGBA', (448, 252), (255, 0, 0, 255))
    result = _fit_width_letterbox(wide, 410)
    assert result.size == (410, 410)
    assert result.getpixel((5, 205))[0] == 255
    assert result.getpixel((405, 205))[0] == 255
    assert result.getpixel((205, 5))[:3] == COLORS['bg_primary']
    assert result.getpixel((205, 404))[:3] == COLORS['bg_primary']


def test_prepare_checkpod_cover_letterbox_not_stretched():
    from mello.config import COLORS
    wide = Image.new('RGBA', (448, 252), (255, 0, 0, 255))
    result = _prepare_checkpod_cover(wide, 410)
    assert result.size == (410, 410)
    # Rotated letterbox tile — background visible on two opposite edges
    bg = COLORS['bg_primary']
    assert result.getpixel((5, 5))[:3] == bg
    assert result.getpixel((405, 405))[:3] == bg
    assert result.getpixel((205, 205))[0] == 255


def test_parse_episodes_picks_mp3_and_normalizes_image():
    episodes = parse_episodes(SAMPLE_GRAPHQL_RESPONSE)
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.id == '16407475'
    assert ep.title.startswith('Reis')
    assert ep.audio_url.endswith('.mp3')
    assert ep.image_url == 'https://example.com/cover?w=800'
    assert ep.duration_ms == 1519 * 1000
    assert ep.uri == 'urn:ard:episode:16407475'


def test_checkpod_manager_cache_on_play(tmp_path, monkeypatch):
    cache_dir = tmp_path / 'checkpod'
    catalog_path = cache_dir / 'catalog.json'
    progress_path = cache_dir / 'progress.json'
    images_dir = cache_dir / 'images'
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_CACHE_DIR', cache_dir)
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_CATALOG_PATH', catalog_path)
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_PROGRESS_PATH', progress_path)
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_IMAGES_DIR', images_dir)

    manager = CheckPodManager()
    episode_id = '123'
    mp3_path = manager.cached_mp3_path(episode_id)
    mp3_path.write_bytes(b'fake-mp3')

    with patch('mello.managers.checkpod_manager.requests.get') as mock_get:
        path = manager.ensure_cached(episode_id, 'https://example.com/ep.mp3')
        mock_get.assert_not_called()
    assert path == mp3_path


def test_checkpod_manager_downloads_when_missing(tmp_path, monkeypatch):
    cache_dir = tmp_path / 'checkpod'
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_CACHE_DIR', cache_dir)
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_CATALOG_PATH', cache_dir / 'catalog.json')
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_PROGRESS_PATH', cache_dir / 'progress.json')
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_IMAGES_DIR', cache_dir / 'images')

    manager = CheckPodManager()
    mock_resp = MagicMock()
    mock_resp.iter_content.return_value = [b'abc', b'def']
    mock_resp.raise_for_status = MagicMock()

    with patch('mello.managers.checkpod_manager.requests.get', return_value=mock_resp):
        path = manager.ensure_cached('999', 'https://example.com/ep.mp3')

    assert path is not None
    assert path.read_bytes() == b'abcdef'


def test_local_playback_uses_wm8960_audio_device():
    from mello.config import WM8960_SINK
    from mello.controllers.local_playback import MPV_AUDIO_DEVICE

    assert MPV_AUDIO_DEVICE == f'pipewire/{WM8960_SINK}'


def test_local_playback_mock_mode():
    controller = LocalPlaybackController(mock_mode=True)
    ok = controller.play(
        Path('/tmp/nope.mp3'),
        context_uri='urn:ard:episode:1',
        track_name='Testfolge',
        start_position_ms=0,
        duration_ms=60000,
    )
    assert ok is True
    playing, paused, _, _, uri, name = controller.get_state()
    assert playing is True
    assert paused is False
    assert uri == 'urn:ard:episode:1'
    assert name == 'Testfolge'

    controller.pause()
    playing, paused, _, _, _, _ = controller.get_state()
    assert playing is False
    assert paused is True

    controller.stop()
    assert controller.is_active is False


def test_open_checkpod_screen_sets_launch_lock():
    app = Mello.__new__(Mello)
    app.app_screen = AppScreen.HOME
    app._checkpod_launch_lock = False
    app._spotify_launch_lock = False
    app._checkpod_play_in_progress = False
    app._checkpod_pending_focus_uri = 'x'
    app._checkpod_pending_focus_since = 1.0
    app.selected_index = 2
    app.carousel = SimpleNamespace(scroll_x=5.0, set_target=MagicMock())
    app.renderer = SimpleNamespace(invalidate=MagicMock())
    app._pressed_button = 'home_checker'
    app._pause_active_playback = MagicMock()
    app._set_manual_pause_lock = MagicMock()
    app._update_carousel_max_index = MagicMock()
    app.checkpod_manager = SimpleNamespace(refresh_episodes=MagicMock())
    app.local_playback = SimpleNamespace(warm_up=MagicMock())

    with patch('mello.app.run_async') as mock_async:
        Mello._open_checkpod_screen(app)

    assert app.app_screen == AppScreen.CHECKPOD
    assert app._checkpod_launch_lock is True
    assert app.selected_index == 0
    app._pause_active_playback.assert_called_once_with('checkpod_open')
    app.local_playback.warm_up.assert_called_once()
    mock_async.assert_called_once()


def test_open_home_screen_stops_local_playback():
    app = Mello.__new__(Mello)
    app.app_screen = AppScreen.CHECKPOD
    app._checkpod_launch_lock = True
    app._checkpod_play_in_progress = True
    app._checkpod_pending_focus_uri = 'x'
    app._checkpod_pending_focus_since = 1.0
    app.renderer = SimpleNamespace(invalidate=MagicMock())
    app._pressed_button = None
    app._pause_active_playback = MagicMock()
    app._set_manual_pause_lock = MagicMock()

    Mello._open_home_screen(app)

    app._pause_active_playback.assert_called_once_with('home_open')
    assert app.app_screen == AppScreen.HOME
    assert app._checkpod_launch_lock is False


def test_home_checker_tap_opens_checkpod():
    app = Mello.__new__(Mello)
    app._pressed_button = 'home_checker'
    app.renderer = SimpleNamespace(
        home_checker_rect=types.SimpleNamespace(collidepoint=lambda p: p == (100, 100)),
        invalidate=MagicMock(),
    )
    app._open_checkpod_screen = MagicMock()

    app._handle_home_touch_up((100, 100))

    app._open_checkpod_screen.assert_called_once()


def test_refresh_status_skips_spotify_sync_on_checkpod():
    from mello.models import NowPlaying

    app = Mello.__new__(Mello)
    app.app_screen = AppScreen.CHECKPOD
    app.api = SimpleNamespace(status=lambda: {'playing': True, 'context_uri': 'spotify:album:1'}, is_connected=lambda: True)
    app.events = SimpleNamespace(context_uri='spotify:album:1')
    app._connection_fail_count = 0
    app._connection_grace_threshold = 3
    app._connected = True
    app._connected_lock = __import__('threading').Lock()
    app._last_status_ok_at = 0.0
    app._status_unknown = False
    app._last_restore_handled_at = 0.0
    app._restore_dedup_count = 0
    app._startup_ready = True
    app._now_playing_lock = __import__('threading').Lock()
    app._now_playing = NowPlaying(
        playing=True,
        context_uri='urn:ard:episode:16407475',
        track_name='CheckPod Episode',
    )

    Mello._refresh_status(app)

    assert app._now_playing.context_uri == 'urn:ard:episode:16407475'
    assert app._now_playing.track_name == 'CheckPod Episode'


def test_play_checkpod_item_dedupes_same_uri():
    app = Mello.__new__(Mello)
    app._checkpod_play_in_progress = True
    app._checkpod_play_target_uri = 'urn:ard:episode:1'
    app.checkpod_manager = SimpleNamespace(get_episode_id_for_uri=lambda uri: '1')
    app._show_toast = MagicMock()

    with patch('mello.app.run_async') as mock_async:
        Mello._play_checkpod_item(
            app,
            SimpleNamespace(uri='urn:ard:episode:1', name='Test'),
        )
        mock_async.assert_not_called()
    app._show_toast.assert_not_called()


def test_checkpod_paused_episode_renders_play_button():
    app = Mello.__new__(Mello)
    item = CatalogItem(
        id='16407475',
        uri='urn:ard:episode:16407475',
        name='Reis',
        type='episode',
    )
    app.app_screen = AppScreen.CHECKPOD
    app.checkpod_manager = SimpleNamespace(get_display_items=lambda: [item])
    app.selected_index = 0
    app._now_playing_lock = __import__('threading').Lock()
    app._now_playing = NowPlaying()
    app.local_playback = SimpleNamespace(
        get_state=lambda: (False, True, 1000, 60000, item.uri, item.name)
    )
    app._checkpod_play_in_progress = False
    app._checkpod_pending_focus_uri = None
    app.carousel = SimpleNamespace(scroll_x=0.0)
    app.touch = SimpleNamespace(drag_offset=0.0, dragging=False)
    app.sleep_manager = SimpleNamespace(is_sleeping=False)
    app.volume = SimpleNamespace(index=0)
    app.delete_mode_id = None
    app._pressed_button = None
    app._toast_message = None
    app._toast_time = 0
    app._toast_duration = 2.0
    app._last_play_commit_uri = None
    app._last_play_commit_at = 0.0
    app.bluetooth = SimpleNamespace(
        connected_device=None,
        paired_devices=[],
        discovered_devices=[],
        scanning=False,
        pairing_mac=None,
    )
    app._bt_audio_active = False
    app.setup_menu = SimpleNamespace(
        state=None,
        known_networks=[],
        current_network=None,
        scroll_offset=0,
        _update_checking=False,
        _update_available=False,
        _update_running=False,
        _reset_confirm_pending=False,
        _shutdown_confirm_pending=False,
    )
    app.settings = SimpleNamespace(
        auto_pause_minutes=30,
        progress_expiry_hours=96,
        get_volume_levels=lambda: [],
    )
    app.app_version_label = 'test'
    app._get_cached_network_status = lambda: True
    app.renderer = SimpleNamespace(draw=MagicMock(return_value=[]), delete_button_rect=None)

    Mello._draw(app)

    ctx = app.renderer.draw.call_args.args[0]
    assert ctx.is_playing is False
