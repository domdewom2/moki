"""
Tests for home screen navigation and touch handling.
"""
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pygame_stub = types.ModuleType('pygame')
pygame_stub.Surface = object
pygame_stub.Rect = object
pygame_stub.font = SimpleNamespace(Font=object)
sys.modules.setdefault('pygame', pygame_stub)
sys.modules.setdefault('pygame.gfxdraw', types.ModuleType('pygame.gfxdraw'))

from mello.app import Mello
from mello.models import AppScreen, MenuState, NowPlaying
from mello.managers.setup_menu import SetupMenu
from mello.config import (
    BTN_SIZE,
    CAROUSEL_CENTER_Y,
    COVER_SIZE,
    COVER_SIZE_SMALL,
    COVER_SPACING,
    CONTROLS_X,
)


def _make_setup_menu(on_open_home=None):
    menu = SetupMenu.__new__(SetupMenu)
    menu.state = MenuState.MAIN
    menu._reset_confirm_pending = False
    menu._shutdown_confirm_pending = False
    menu._update_running = False
    menu._update_checking = False
    menu._update_available = False
    menu._on_invalidate = MagicMock()
    menu._on_open_home = on_open_home or MagicMock()
    menu.close = MagicMock(side_effect=lambda: setattr(menu, 'state', MenuState.CLOSED))
    return menu


def test_setup_menu_home_tap_opens_home():
    on_open_home = MagicMock()
    menu = _make_setup_menu(on_open_home=on_open_home)
    home_rect = SimpleNamespace(collidepoint=lambda x, y: True)

    menu.handle_tap((50, 40), {'home': home_rect})

    menu.close.assert_called_once()
    on_open_home.assert_called_once()
    assert menu.state == MenuState.CLOSED


def test_open_home_screen_pauses_when_playing():
    app = Mello.__new__(Mello)
    app.app_screen = AppScreen.SPOTIFY
    app._checkpod_launch_lock = False
    app._checkpod_play_in_progress = False
    app._checkpod_pending_focus_uri = None
    app._checkpod_pending_focus_since = 0.0
    app.renderer = SimpleNamespace(invalidate=MagicMock())
    app._pressed_button = 'play'
    app._pause_active_playback = MagicMock()
    app._set_manual_pause_lock = MagicMock()

    Mello._open_home_screen(app)

    app._pause_active_playback.assert_called_once_with('home_open')
    app._set_manual_pause_lock.assert_called_once_with('home_open')
    assert app.app_screen == AppScreen.HOME
    assert app._pressed_button is None


def test_open_home_screen_skips_pause_when_stopped():
    app = Mello.__new__(Mello)
    app.app_screen = AppScreen.SPOTIFY
    app._checkpod_launch_lock = False
    app._checkpod_play_in_progress = False
    app._checkpod_pending_focus_uri = None
    app._checkpod_pending_focus_since = 0.0
    app.renderer = SimpleNamespace(invalidate=MagicMock())
    app._pause_active_playback = MagicMock()
    app._set_manual_pause_lock = MagicMock()

    Mello._open_home_screen(app)

    app._pause_active_playback.assert_called_once_with('home_open')
    assert app.app_screen == AppScreen.HOME


def test_home_musik_tap_returns_to_spotify():
    app = Mello.__new__(Mello)
    app._pressed_button = 'home_musik'
    app.renderer = SimpleNamespace(
        home_musik_rect=types.SimpleNamespace(collidepoint=lambda p: p == (100, 100)),
        invalidate=MagicMock(),
    )
    app._open_spotify_screen = MagicMock()

    app._handle_home_touch_up((100, 100))

    app._open_spotify_screen.assert_called_once()


def test_home_musik_tap_outside_icon_does_not_navigate():
    app = Mello.__new__(Mello)
    app._pressed_button = 'home_musik'
    app.renderer = SimpleNamespace(
        home_musik_rect=types.SimpleNamespace(collidepoint=lambda p: False),
        invalidate=MagicMock(),
    )
    app._open_spotify_screen = MagicMock()

    app._handle_home_touch_up((5, 5))

    app._open_spotify_screen.assert_not_called()


def test_open_spotify_screen_sets_launch_lock():
    app = Mello.__new__(Mello)
    app.app_screen = AppScreen.HOME
    app._spotify_launch_lock = False
    app._checkpod_launch_lock = False
    app._reset_pending_focus = MagicMock()
    app._reset_checkpod_screen_state = MagicMock()
    app._reset_local_music_screen_state = MagicMock()
    app.local_playback = SimpleNamespace(is_active=False, stop=MagicMock())
    app._update_carousel_max_index = MagicMock()
    app.renderer = SimpleNamespace(invalidate=MagicMock())
    app._pressed_button = 'home_musik'

    Mello._open_spotify_screen(app)

    assert app.app_screen == AppScreen.SPOTIFY
    assert app._spotify_launch_lock is True
    app.local_playback.stop.assert_not_called()
    app._reset_pending_focus.assert_called_once_with('spotify_open')


def test_toggle_play_clears_spotify_launch_lock():
    app = Mello.__new__(Mello)
    app.app_screen = AppScreen.SPOTIFY
    app._spotify_launch_lock = True
    app.mock_mode = False
    app._now_playing_lock = threading.Lock()
    app._now_playing = NowPlaying(stopped=True, playing=False)
    app.catalog_manager = SimpleNamespace(items=[])
    app.temp_item = None
    app.selected_index = 0
    app.playback = SimpleNamespace(
        has_pending_play=False,
        toggle_play=MagicMock(),
    )
    app._clear_manual_pause_lock = MagicMock()
    app._clear_spotify_launch_lock = MagicMock()

    Mello._toggle_play(app)

    app._clear_spotify_launch_lock.assert_called_once_with('play_tap')


def test_home_touch_down_highlights_checker_icon():
    app = Mello.__new__(Mello)
    app.setup_menu = SimpleNamespace(is_open=False)
    app.app_screen = AppScreen.HOME
    app.renderer = SimpleNamespace(
        home_musik_rect=types.SimpleNamespace(collidepoint=lambda p: False),
        home_checker_rect=types.SimpleNamespace(collidepoint=lambda p: p == (80, 80)),
        home_local_music_rect=types.SimpleNamespace(collidepoint=lambda p: False),
        home_settings_rect=types.SimpleNamespace(collidepoint=lambda p: False),
        invalidate=MagicMock(),
    )
    app._user_activated_playback = False

    app._handle_touch_down((80, 80))

    assert app._pressed_button == 'home_checker'
    app.renderer.invalidate.assert_called_once()


def test_home_touch_down_highlights_settings_icon():
    app = Mello.__new__(Mello)
    app.setup_menu = SimpleNamespace(is_open=False)
    app.app_screen = AppScreen.HOME
    app.renderer = SimpleNamespace(
        home_musik_rect=types.SimpleNamespace(collidepoint=lambda p: False),
        home_checker_rect=types.SimpleNamespace(collidepoint=lambda p: False),
        home_local_music_rect=types.SimpleNamespace(collidepoint=lambda p: False),
        home_settings_rect=types.SimpleNamespace(collidepoint=lambda p: p == (90, 90)),
        invalidate=MagicMock(),
    )
    app._user_activated_playback = False

    app._handle_touch_down((90, 90))

    assert app._pressed_button == 'home_settings'
    app.renderer.invalidate.assert_called_once()


def test_home_settings_tap_opens_pin_entry():
    app = Mello.__new__(Mello)
    app._pressed_button = 'home_settings'
    app.renderer = SimpleNamespace(
        home_settings_rect=types.SimpleNamespace(collidepoint=lambda p: p == (100, 100)),
        invalidate=MagicMock(),
    )
    app._open_settings_with_pin = MagicMock()

    app._handle_home_touch_up((100, 100))

    app._open_settings_with_pin.assert_called_once()


def test_open_settings_with_pin_pauses_playback():
    app = Mello.__new__(Mello)
    app.setup_menu = SimpleNamespace(open_with_pin=MagicMock())
    app.renderer = SimpleNamespace(invalidate=MagicMock())
    app._pause_active_playback = MagicMock()

    Mello._open_settings_with_pin(app)

    app._pause_active_playback.assert_called_once_with('settings_open')
    app.setup_menu.open_with_pin.assert_called_once()


def test_home_touch_down_highlights_musik_icon():
    app = Mello.__new__(Mello)
    app.setup_menu = SimpleNamespace(is_open=False)
    app.app_screen = AppScreen.HOME
    app.renderer = SimpleNamespace(
        home_musik_rect=types.SimpleNamespace(collidepoint=lambda p: p == (50, 50)),
        home_checker_rect=types.SimpleNamespace(collidepoint=lambda p: False),
        home_local_music_rect=types.SimpleNamespace(collidepoint=lambda p: False),
        invalidate=MagicMock(),
    )
    app._user_activated_playback = False

    app._handle_touch_down((50, 50))

    assert app._pressed_button == 'home_musik'
    app.renderer.invalidate.assert_called_once()


def test_player_home_button_opens_home_screen():
    app = Mello.__new__(Mello)
    app.app_screen = AppScreen.SPOTIFY
    app._last_action_time = 0
    app._pressed_button = None
    app._pressed_time = 0
    app.renderer = SimpleNamespace(invalidate=MagicMock())
    app.bluetooth = SimpleNamespace(connected_device=None, toggle_audio=MagicMock())
    app._open_home_screen = MagicMock()

    home_y = (
        CAROUSEL_CENTER_Y
        - (COVER_SIZE + COVER_SPACING)
        - COVER_SIZE_SMALL // 2
        + BTN_SIZE // 2
    )
    Mello._handle_button_tap(app, (CONTROLS_X, home_y))

    app._open_home_screen.assert_called_once()
    assert app._pressed_button == 'home'
    app.renderer.invalidate.assert_called_once()
