"""
Tests for CheckPod (Checker Tobi) integration.
"""
import json
import sys
import types
from datetime import datetime
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


def test_local_playback_seek_relative_mock_mode():
    controller = LocalPlaybackController(mock_mode=True)
    controller.play(
        Path('/tmp/nope.mp3'),
        context_uri='urn:ard:episode:1',
        track_name='Testfolge',
        start_position_ms=10000,
        duration_ms=60000,
    )
    assert controller.seek_relative(30) is True
    _, _, position_ms, _, _, _ = controller.get_state()
    assert position_ms == 40000

    assert controller.seek_relative(-30) is True
    _, _, position_ms, _, _, _ = controller.get_state()
    assert position_ms == 10000

    assert controller.seek_relative(-99999) is True
    _, _, position_ms, _, _, _ = controller.get_state()
    assert position_ms == 0

    assert controller.seek_relative(99999) is True
    _, _, position_ms, _, _, _ = controller.get_state()
    assert position_ms == 60000


def test_local_playback_seek_relative_when_idle():
    controller = LocalPlaybackController(mock_mode=True)
    assert controller.seek_relative(30) is False


def test_seek_checkpod_saves_progress():
    app = Mello.__new__(Mello)
    app.local_playback = SimpleNamespace(
        get_state=lambda: (True, False, 40000, 60000, 'urn:ard:episode:1', 'Test'),
        seek_relative=lambda delta: True,
    )
    app.checkpod_manager = SimpleNamespace(save_progress=MagicMock())
    app.renderer = SimpleNamespace(invalidate=MagicMock())

    Mello._seek_checkpod(app, 30)

    app.checkpod_manager.save_progress.assert_called_once_with(
        'urn:ard:episode:1', 40000, 60000, 'Test', force=True
    )
    app.renderer.invalidate.assert_called_once()


def test_checkpod_cleanup_deletes_stale_download(tmp_path, monkeypatch):
    from datetime import timedelta

    cache_dir = tmp_path / 'checkpod'
    images_dir = cache_dir / 'images'
    cache_dir.mkdir(parents=True)
    progress_path = cache_dir / 'progress.json'
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_CACHE_DIR', cache_dir)
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_CATALOG_PATH', cache_dir / 'catalog.json')
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_PROGRESS_PATH', progress_path)
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_IMAGES_DIR', images_dir)

    manager = CheckPodManager()

    stale_id = '111'
    fresh_id = '222'
    active_id = '333'
    stale_path = cache_dir / f'{stale_id}.mp3'
    fresh_path = cache_dir / f'{fresh_id}.mp3'
    active_path = cache_dir / f'{active_id}.mp3'
    stale_path.write_bytes(b'stale')
    fresh_path.write_bytes(b'fresh')
    active_path.write_bytes(b'active')
    images_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / f'{stale_id}.png').write_bytes(b'img')

    old_date = (datetime.now() - timedelta(days=10)).isoformat()
    fresh_date = datetime.now().isoformat()
    progress_path.write_text(json.dumps({
        f'urn:ard:episode:{stale_id}': {
            'uri': f'urn:ard:episode:{stale_id}',
            'position': 1000,
            'duration': 60000,
            'name': 'Stale',
            'updatedAt': old_date,
        },
        f'urn:ard:episode:{fresh_id}': {
            'uri': f'urn:ard:episode:{fresh_id}',
            'position': 1000,
            'duration': 60000,
            'name': 'Fresh',
            'updatedAt': fresh_date,
        },
    }))

    deleted = manager.cleanup_stale_downloads(
        active_context_uri=f'urn:ard:episode:{active_id}',
        max_age_days=7,
    )

    assert deleted == 1
    assert not stale_path.exists()
    assert fresh_path.exists()
    assert active_path.exists()
    assert not (images_dir / f'{stale_id}.png').exists()
    remaining = json.loads(progress_path.read_text())
    assert f'urn:ard:episode:{stale_id}' not in remaining
    assert f'urn:ard:episode:{fresh_id}' in remaining


def test_checkpod_sleep_blocks_only_while_playing_not_paused():
    from mello.managers.sleep import SleepManager

    mgr = SleepManager()
    mgr.reset_timer()
    mgr.last_activity = __import__('time').time() - 130

    assert mgr.check_sleep(is_playing=False) is True
    mgr.wake_up()

    assert mgr.check_sleep(is_playing=True) is False
    mgr.last_activity = __import__('time').time() - 130
    assert mgr.check_sleep(is_playing=True) is False
    assert mgr.is_sleeping is False


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
    app._reset_local_music_screen_state = MagicMock()
    app._update_carousel_max_index = MagicMock()
    app._last_checkpod_context_uri = None
    app.checkpod_manager = SimpleNamespace(
        refresh_episodes=MagicMock(),
        cleanup_stale_downloads=MagicMock(),
        get_display_items=lambda: [],
        get_last_played_uri=lambda fallback_uri=None: None,
    )
    app.local_playback = SimpleNamespace(warm_up=MagicMock(), get_state=lambda: (False, False, 0, 0, None, ''))

    with patch('mello.app.run_async') as mock_async:
        Mello._open_checkpod_screen(app)

    assert app.app_screen == AppScreen.CHECKPOD
    assert app._checkpod_launch_lock is True
    assert app.selected_index == 0
    app._pause_active_playback.assert_called_once_with('checkpod_open')
    app.local_playback.warm_up.assert_called_once()
    mock_async.assert_called_once_with(app._refresh_checkpod_episodes)


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
        pin_buffer='',
        change_pin_step=0,
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


def test_checkpod_save_progress_rejects_regression(tmp_path, monkeypatch):
    cache_dir = tmp_path / 'checkpod'
    cache_dir.mkdir(parents=True)
    progress_path = cache_dir / 'progress.json'
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_CACHE_DIR', cache_dir)
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_CATALOG_PATH', cache_dir / 'catalog.json')
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_PROGRESS_PATH', progress_path)
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_IMAGES_DIR', cache_dir / 'images')

    manager = CheckPodManager()
    uri = 'urn:ard:episode:1'
    manager.save_progress(uri, 600000, 900000, 'Episode')
    manager.save_progress(uri, 0, 900000, 'Episode')

    saved = json.loads(progress_path.read_text())
    assert saved[uri]['position'] == 600000

    assert manager.save_progress(uri, 150000, 900000, 'Episode', force=True) is True
    saved = json.loads(progress_path.read_text())
    assert saved[uri]['position'] == 150000


def test_get_last_played_uri_returns_most_recent(tmp_path, monkeypatch):
    cache_dir = tmp_path / 'checkpod'
    cache_dir.mkdir(parents=True)
    progress_path = cache_dir / 'progress.json'
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_CACHE_DIR', cache_dir)
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_CATALOG_PATH', cache_dir / 'catalog.json')
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_PROGRESS_PATH', progress_path)
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_IMAGES_DIR', cache_dir / 'images')

    manager = CheckPodManager()
    progress_path.write_text(json.dumps({
        'urn:ard:episode:old': {
            'uri': 'urn:ard:episode:old',
            'position': 10000,
            'duration': 600000,
            'name': 'Old',
            'updatedAt': '2026-05-20T10:00:00',
        },
        'urn:ard:episode:new': {
            'uri': 'urn:ard:episode:new',
            'position': 20000,
            'duration': 600000,
            'name': 'New',
            'updatedAt': '2026-05-23T10:00:00',
        },
    }))

    assert manager.get_last_played_uri() == 'urn:ard:episode:new'


def test_get_last_played_uri_skips_near_complete(tmp_path, monkeypatch):
    cache_dir = tmp_path / 'checkpod'
    cache_dir.mkdir(parents=True)
    progress_path = cache_dir / 'progress.json'
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_CACHE_DIR', cache_dir)
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_CATALOG_PATH', cache_dir / 'catalog.json')
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_PROGRESS_PATH', progress_path)
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_IMAGES_DIR', cache_dir / 'images')

    manager = CheckPodManager()
    progress_path.write_text(json.dumps({
        'urn:ard:episode:done': {
            'uri': 'urn:ard:episode:done',
            'position': 590000,
            'duration': 600000,
            'name': 'Done',
            'updatedAt': '2026-05-23T12:00:00',
        },
        'urn:ard:episode:active': {
            'uri': 'urn:ard:episode:active',
            'position': 120000,
            'duration': 600000,
            'name': 'Active',
            'updatedAt': '2026-05-23T11:00:00',
        },
    }))

    assert manager.get_last_played_uri() == 'urn:ard:episode:active'


def test_restore_checkpod_carousel_focus():
    app = Mello.__new__(Mello)
    app._last_checkpod_context_uri = None
    app.carousel = SimpleNamespace(set_target=MagicMock())
    app.checkpod_manager = SimpleNamespace(
        get_display_items=lambda: [
            CatalogItem(id='1', uri='urn:ard:episode:1', name='First', images=[]),
            CatalogItem(id='2', uri='urn:ard:episode:2', name='Second', images=[]),
            CatalogItem(id='3', uri='urn:ard:episode:3', name='Third', images=[]),
        ],
        get_last_played_uri=lambda fallback_uri=None: 'urn:ard:episode:2',
    )

    Mello._restore_checkpod_carousel_focus(app)

    assert app.selected_index == 1
    app.carousel.set_target.assert_called_once_with(1)


def test_save_checkpod_progress_now_uses_live_position():
    app = Mello.__new__(Mello)
    app._last_checkpod_progress_save = 0.0
    app._last_checkpod_context_uri = None
    app.local_playback = SimpleNamespace(
        get_state=lambda: (True, False, 1000, 600000, 'urn:ard:episode:1', 'Episode'),
        get_live_position_ms=lambda: 180000,
    )
    app.checkpod_manager = SimpleNamespace(save_progress=MagicMock())

    Mello._save_checkpod_progress_now(app, 'home_open')

    app.checkpod_manager.save_progress.assert_called_once_with(
        'urn:ard:episode:1', 180000, 600000, 'Episode', force=True
    )
    assert app._last_checkpod_context_uri == 'urn:ard:episode:1'


def test_pause_active_playback_saves_before_stop():
    app = Mello.__new__(Mello)
    app._last_checkpod_progress_save = 0.0
    app._last_checkpod_context_uri = None
    app._now_playing_lock = __import__('threading').Lock()
    app._now_playing = NowPlaying()
    app.playback = SimpleNamespace(
        play_state=SimpleNamespace(should_show_loading=False),
        _play_in_progress=False,
        _execute_pause=MagicMock(),
    )
    app.local_playback = SimpleNamespace(
        is_active=True,
        get_state=lambda: (True, False, 120000, 600000, 'urn:ard:episode:1', 'Episode'),
        get_live_position_ms=lambda: 120000,
        stop=MagicMock(),
    )
    app.checkpod_manager = SimpleNamespace(save_progress=MagicMock())

    Mello._pause_active_playback(app, 'home_open')

    app.checkpod_manager.save_progress.assert_called_once()
    app.local_playback.stop.assert_called_once_with(save_progress=False)
    app.playback._execute_pause.assert_not_called()


def test_local_playback_stop_uses_live_position():
    saved = {}

    def on_stopped(uri, pos, dur, name):
        saved['uri'] = uri
        saved['position'] = pos

    controller = LocalPlaybackController(mock_mode=False, on_stopped=on_stopped)
    controller._playing = True
    controller._paused = False
    controller._context_uri = 'urn:ard:episode:1'
    controller._track_name = 'Episode'
    controller._position_ms = 1000
    controller._duration_ms = 600000
    controller._query_property = lambda name: 300.0 if name == 'time-pos' else 600.0

    controller.stop()

    assert saved['position'] == 300000


def test_play_checkpod_item_resumes_from_saved_progress(tmp_path, monkeypatch):
    cache_dir = tmp_path / 'checkpod'
    cache_dir.mkdir(parents=True)
    mp3 = cache_dir / '16407475.mp3'
    mp3.write_bytes(b'fake')
    progress_path = cache_dir / 'progress.json'
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_CACHE_DIR', cache_dir)
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_CATALOG_PATH', cache_dir / 'catalog.json')
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_PROGRESS_PATH', progress_path)
    monkeypatch.setattr('mello.managers.checkpod_manager.CHECKPOD_IMAGES_DIR', cache_dir / 'images')

    manager = CheckPodManager()
    uri = 'urn:ard:episode:16407475'
    manager.save_progress(uri, 150000, 600000, 'Reis')

    app = Mello.__new__(Mello)
    app._checkpod_play_in_progress = False
    app._checkpod_play_target_uri = None
    app._checkpod_play_failed_uri = None
    app._checkpod_play_failed_at = 0.0
    app._checkpod_launch_lock = True
    app.app_screen = AppScreen.CHECKPOD
    app.checkpod_manager = manager
    app.volume = SimpleNamespace(unmute=MagicMock())
    app.renderer = SimpleNamespace(invalidate=MagicMock())

    played = {}

    def fake_play(path, context_uri, track_name, start_position_ms=0, duration_ms=0):
        played['start_position_ms'] = start_position_ms
        played['context_uri'] = context_uri
        return True

    app.local_playback = SimpleNamespace(
        play=fake_play,
        get_state=lambda: (False, False, 0, 0, None, ''),
    )
    item = CatalogItem(id='16407475', uri=uri, name='Reis', images=[])

    with patch.object(manager, 'get_audio_url', return_value='https://example.com/ep.mp3'):
        with patch('mello.app.run_async', side_effect=lambda fn: fn()):
            Mello._play_checkpod_item(app, item)

    assert played['start_position_ms'] == 150000
    assert played['context_uri'] == uri


def test_restart_checkpod_episode_clears_progress_and_plays_from_start():
    app = Mello.__new__(Mello)
    app.app_screen = AppScreen.CHECKPOD
    item = CatalogItem(id='1', uri='urn:ard:episode:1', name='Test Episode', images=[])
    app.selected_index = 0
    app._display_items = lambda: [item]
    app.local_playback = SimpleNamespace(is_active=True, stop=MagicMock())
    app.checkpod_manager = SimpleNamespace(clear_progress=MagicMock())
    app._checkpod_pending_focus_uri = 'urn:ard:episode:1'
    app._checkpod_pending_focus_since = 1.0
    app._checkpod_play_failed_uri = 'urn:ard:episode:1'
    app._checkpod_play_failed_at = 1.0
    app._clear_manual_pause_lock = MagicMock()
    app._play_checkpod_item = MagicMock()
    app._local_media_manager = lambda: app.checkpod_manager

    Mello._restart_checkpod_episode(app)

    app.local_playback.stop.assert_called_once_with(save_progress=False)
    app.checkpod_manager.clear_progress.assert_called_once_with('urn:ard:episode:1')
    app._play_checkpod_item.assert_called_once_with(item, from_beginning=True)
    assert app._checkpod_pending_focus_uri is None
    assert app._checkpod_play_failed_uri is None
    assert app._user_activated_playback is True
