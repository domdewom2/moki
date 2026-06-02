"""
Tests for Radio TEDDY app navigation and playback.
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

from moki.app import Moki
from moki.models import AppScreen, CatalogItem, NowPlaying
from moki.config import RADIO_TEDDY_CONTEXT_URI, RADIO_TEDDY_STREAM_URL, RADIO_TEDDY_NAME


def _make_radio_app():
    app = Moki.__new__(Moki)
    app.app_screen = AppScreen.HOME
    app.renderer = SimpleNamespace(
        invalidate=MagicMock(),
        home_radio_rect=SimpleNamespace(collidepoint=lambda p: p == (120, 800)),
    )
    app._pause_active_playback = MagicMock()
    app._set_manual_pause_lock = MagicMock()
    app._reset_checkpod_screen_state = MagicMock()
    app._reset_local_music_screen_state = MagicMock()
    app._reset_radio_screen_state = MagicMock()
    app._update_carousel_max_index = MagicMock()
    app.local_playback = SimpleNamespace(warm_up=MagicMock())
    app.carousel = SimpleNamespace(max_index=0, scroll_x=0.0, set_target=MagicMock())
    app.selected_index = 0
    app._pressed_button = None
    app._radio_launch_lock = False
    app._radio_play_in_progress = False
    app._spotify_launch_lock = False
    app.volume = SimpleNamespace(unmute=MagicMock())
    return app


def test_open_radio_screen():
    app = _make_radio_app()

    Moki._open_radio_screen(app)

    app._pause_active_playback.assert_called_once_with('radio_open')
    app._set_manual_pause_lock.assert_called_once_with('radio_open')
    assert app.app_screen == AppScreen.RADIO
    assert app._radio_launch_lock is True
    app.local_playback.warm_up.assert_called_once()


def test_home_radio_tap_opens_radio():
    app = _make_radio_app()
    app._open_radio_screen = MagicMock()

    app._pressed_button = 'home_radio'
    app._handle_home_touch_up((120, 800))

    app._open_radio_screen.assert_called_once()


def test_display_items_returns_radio_catalog_item():
    app = Moki.__new__(Moki)
    app.app_screen = AppScreen.RADIO

    items = Moki._display_items(app)

    assert len(items) == 1
    assert items[0].uri == RADIO_TEDDY_CONTEXT_URI
    assert items[0].name == RADIO_TEDDY_NAME


def test_play_radio_stream_calls_mpv():
    app = Moki.__new__(Moki)
    app.app_screen = AppScreen.RADIO
    app._radio_play_in_progress = False
    app.renderer = SimpleNamespace(invalidate=MagicMock())
    app.local_playback = MagicMock()
    app.local_playback.play_stream.return_value = True
    app.volume = SimpleNamespace(unmute=MagicMock())
    app._is_local_media_item_playing = MagicMock(return_value=False)
    app._clear_radio_launch_lock = MagicMock()

    with patch('moki.app.run_async', side_effect=lambda fn: fn()):
        Moki._play_radio_stream(app)

    app.local_playback.play_stream.assert_called_once_with(
        RADIO_TEDDY_STREAM_URL,
        context_uri=RADIO_TEDDY_CONTEXT_URI,
        track_name=RADIO_TEDDY_NAME,
    )
    app.volume.unmute.assert_called_once()


def test_open_home_stops_radio_state():
    app = _make_radio_app()
    app.app_screen = AppScreen.RADIO
    app._pause_active_playback = MagicMock()
    app._set_manual_pause_lock = MagicMock()
    app._close_voice_search = MagicMock()
    app._reset_home_pager = MagicMock()

    Moki._open_home_screen(app)

    app._pause_active_playback.assert_called_once_with('home_open')
    assert app.app_screen == AppScreen.HOME
    app._reset_radio_screen_state.assert_called_once()


def test_local_playback_play_stream_sets_live_flag():
    from moki.controllers.local_playback import LocalPlaybackController

    player = LocalPlaybackController(mock_mode=True)
    ok = player.play_stream(
        'https://example.com/stream.mp3',
        context_uri=RADIO_TEDDY_CONTEXT_URI,
        track_name=RADIO_TEDDY_NAME,
    )

    assert ok is True
    assert player.is_live_stream is True
    playing, paused, _, _, context_uri, name = player.get_state()
    assert playing is True
    assert context_uri == RADIO_TEDDY_CONTEXT_URI
    assert name == RADIO_TEDDY_NAME


def test_local_playback_seek_disabled_for_live():
    from moki.controllers.local_playback import LocalPlaybackController

    player = LocalPlaybackController(mock_mode=True)
    player.play_stream(
        'https://example.com/stream.mp3',
        context_uri=RADIO_TEDDY_CONTEXT_URI,
        track_name=RADIO_TEDDY_NAME,
    )

    assert player.seek_relative(30) is False


def test_live_stream_stop_mutes_speaker_before_mpv_stop():
    from moki.controllers.local_playback import LocalPlaybackController

    player = LocalPlaybackController(
        mock_mode=False,
        get_speaker_level=lambda: 88,
    )
    player._send_command = MagicMock(return_value=True)
    player._send_commands = MagicMock(return_value=True)
    player._query_property = MagicMock(return_value=True)
    player._process = MagicMock()
    player._process.poll.return_value = None
    with player._lock:
        player._playing = True
        player._is_live_stream = True

    with patch('moki.controllers.local_playback.silence_wm8960_playback') as mock_silence, \
            patch('moki.controllers.local_playback.restore_wm8960_output') as mock_restore, \
            patch('moki.controllers.local_playback.time.sleep'):
        player.stop(save_progress=False)

    mock_silence.assert_called_once()
    mock_restore.assert_not_called()
    player._send_commands.assert_called_once_with([
        ['set_property', 'volume', 0],
        ['set_property', 'mute', True],
        ['set_property', 'pause', True],
    ])
    player._send_command.assert_called_once_with(['stop'])


def test_pause_active_playback_skips_spotify_pause_after_radio():
    app = Moki.__new__(Moki)
    app._last_checkpod_progress_save = 0.0
    app._last_local_music_progress_save = 0.0
    app._now_playing_lock = __import__('threading').Lock()
    app.now_playing = NowPlaying(
        playing=True,
        context_uri=RADIO_TEDDY_CONTEXT_URI,
        track_name=RADIO_TEDDY_NAME,
    )
    app.renderer = SimpleNamespace(invalidate=MagicMock())
    app.playback = SimpleNamespace(
        play_state=SimpleNamespace(should_show_loading=False),
        _play_in_progress=False,
        _execute_pause=MagicMock(),
    )
    app.local_playback = SimpleNamespace(
        is_active=True,
        get_state=lambda: (True, False, 3000, 0, RADIO_TEDDY_CONTEXT_URI, RADIO_TEDDY_NAME),
        get_live_position_ms=lambda: 3000,
        stop=MagicMock(),
    )
    app._save_local_media_progress_now = MagicMock()

    Moki._pause_active_playback(app, 'home_open')

    app.local_playback.stop.assert_called_once_with(save_progress=False)
    app.playback._execute_pause.assert_not_called()
    assert app.now_playing.stopped is True
