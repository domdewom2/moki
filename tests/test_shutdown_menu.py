"""
Tests for setup menu shutdown flow.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from mello.models import MenuState
from mello.managers.setup_menu import SetupMenu


def _make_menu(on_prepare_shutdown=None):
    menu = SetupMenu.__new__(SetupMenu)
    menu.state = MenuState.MAIN
    menu._reset_confirm_pending = False
    menu._shutdown_confirm_pending = False
    menu._update_running = False
    menu._update_checking = False
    menu._update_available = False
    menu._on_invalidate = MagicMock()
    menu._on_toast = MagicMock()
    menu._on_prepare_shutdown = on_prepare_shutdown or MagicMock()
    menu.close = MagicMock()
    return menu


def test_shutdown_requires_confirmation():
    menu = _make_menu()
    rect = SimpleNamespace(collidepoint=lambda x, y: True)

    menu.handle_tap((10, 10), {'shutdown': rect})

    assert menu._shutdown_confirm_pending is True
    menu._on_prepare_shutdown.assert_not_called()


def test_shutdown_confirmed_powers_off():
    on_prepare = MagicMock()
    menu = _make_menu(on_prepare_shutdown=on_prepare)
    menu._shutdown_confirm_pending = True
    rect = SimpleNamespace(collidepoint=lambda x, y: True)

    with patch('mello.managers.setup_menu.subprocess.run') as run_mock:
        menu.handle_tap((10, 10), {'shutdown': rect})

    on_prepare.assert_called_once()
    menu._on_toast.assert_called_with('Shutting down...')
    run_mock.assert_not_called()  # poweroff runs in background thread


def test_shutdown_clears_reset_confirmation():
    menu = _make_menu()
    menu._reset_confirm_pending = True
    rect = SimpleNamespace(collidepoint=lambda x, y: True)

    menu.handle_tap((10, 10), {'shutdown': rect})

    assert menu._reset_confirm_pending is False
    assert menu._shutdown_confirm_pending is True
