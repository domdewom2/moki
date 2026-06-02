"""
Tests for librespot recovery guards (screen context, WiFi, toasts, mute).
"""
import sys
import time
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pygame_stub = SimpleNamespace(
    Surface=object,
    Rect=object,
    font=SimpleNamespace(Font=object),
)
sys.modules.setdefault('pygame', pygame_stub)
sys.modules.setdefault('pygame.gfxdraw', SimpleNamespace())

from moki.app import Moki
from moki.models import AppScreen


def _make_app(screen: AppScreen = AppScreen.SPOTIFY, local_active: bool = False) -> Moki:
    app = Moki.__new__(Moki)
    app.mock_mode = False
    app.app_screen = screen
    app.local_playback = SimpleNamespace(is_active=local_active)
    app.playback = SimpleNamespace(stop_all=MagicMock())
    app.volume = SimpleNamespace(mute=MagicMock())
    app.renderer = SimpleNamespace(invalidate=MagicMock())
    app._librespot_recovery_suppressed_until = 0.0
    app._show_toast = MagicMock()
    app._connection_fail_count = 10
    app._connected = False
    app._connected_lock = threading.Lock()
    app.librespot_recovery = MagicMock()
    app.librespot_recovery.should_restart_for_connection.return_value = True
    app.librespot_recovery.should_restart_for_timeouts.return_value = False
    app.librespot_recovery.maybe_restart.return_value = True
    return app


class TestLibrespotRecoveryGuards:
    def test_skipped_on_checkpod_screen(self):
        app = _make_app(AppScreen.CHECKPOD)
        assert app._maybe_recover_librespot('connection_lost') is False
        app.librespot_recovery.maybe_restart.assert_not_called()

    def test_skipped_when_local_playback_active(self):
        app = _make_app(AppScreen.SPOTIFY, local_active=True)
        assert app._maybe_recover_librespot('connection_lost') is False

    def test_skipped_during_wifi_suppress_window(self):
        app = _make_app(AppScreen.SPOTIFY)
        app._librespot_recovery_suppressed_until = time.time() + 30
        assert app._maybe_recover_librespot('connection_lost') is False

    def test_runs_on_spotify_screen(self):
        app = _make_app(AppScreen.SPOTIFY)
        assert app._maybe_recover_librespot('connection_lost') is True
        app.librespot_recovery.maybe_restart.assert_called_once()

    def test_before_restart_does_not_mute(self):
        app = _make_app(AppScreen.SPOTIFY)
        app._on_librespot_before_restart()
        app.volume.mute.assert_not_called()
        app.playback.stop_all.assert_called_once()

    def test_recovery_toast_suppressed_off_spotify(self):
        app = _make_app(AppScreen.CHECKPOD)
        app._on_librespot_recovery_toast('Spotify wird neu verbunden…')
        app._show_toast.assert_not_called()

    def test_recovery_toast_shown_on_spotify(self):
        app = _make_app(AppScreen.SPOTIFY)
        app._on_librespot_recovery_toast('Spotify wird neu verbunden…')
        app._show_toast.assert_called_once_with('Spotify wird neu verbunden…')

    def test_suppress_extends_deadline(self):
        app = _make_app(AppScreen.SPOTIFY)
        app.suppress_librespot_recovery(10, 'test')
        first = app._librespot_recovery_suppressed_until
        time.sleep(0.01)
        app.suppress_librespot_recovery(30, 'test2')
        assert app._librespot_recovery_suppressed_until > first
