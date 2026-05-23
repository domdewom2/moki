"""
Tests for settings PIN gate and change-PIN flow.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from moki.models import MenuState
from moki.managers.setup_menu import SetupMenu
from moki.managers.settings import Settings
from moki.config import DEFAULT_ADMIN_PIN, PIN_LENGTH


def _make_pin_menu(admin_pin: str = DEFAULT_ADMIN_PIN):
    settings = Settings.__new__(Settings)
    settings._admin_pin = admin_pin
    settings.set_admin_pin = MagicMock()

    menu = SetupMenu.__new__(SetupMenu)
    menu.catalog_manager = MagicMock()
    menu.settings = settings
    menu._on_toast = MagicMock()
    menu._on_invalidate = MagicMock()
    menu._on_library_cleared = MagicMock()
    menu.bluetooth = None
    menu._on_volume_preview = None
    menu._on_open_home = MagicMock()
    menu._on_prepare_shutdown = MagicMock()
    menu.state = MenuState.CLOSED
    menu.scroll_offset = 0
    menu.known_networks = []
    menu.current_network = None
    menu._ssid_to_con_name = {}
    menu._wifi_process = None
    menu._reset_confirm_pending = False
    menu._reset_confirm_time = 0.0
    menu._shutdown_confirm_pending = False
    menu._shutdown_confirm_time = 0.0
    menu._update_available = False
    menu._update_checking = False
    menu._update_running = False
    menu._update_process = None
    menu._pin_buffer = ''
    menu._change_pin_step = 0
    menu._pending_new_pin = None
    return menu


def _pin_rect(key: str):
    return SimpleNamespace(collidepoint=lambda x, y, k=key: True)


def _enter_pin(menu, pin: str):
    for digit in pin:
        menu.handle_tap((0, 0), {f'pin_{digit}': _pin_rect(digit)})


def test_open_with_pin_starts_pin_entry():
    menu = _make_pin_menu()
    menu.open_with_pin()
    assert menu.state == MenuState.PIN_ENTRY
    assert menu.pin_buffer == ''


def test_open_bypasses_pin():
    menu = _make_pin_menu()
    menu.open()
    assert menu.state == MenuState.MAIN


def test_correct_pin_opens_main_menu():
    menu = _make_pin_menu()
    menu.open_with_pin()
    _enter_pin(menu, DEFAULT_ADMIN_PIN)
    assert menu.state == MenuState.MAIN
    menu._on_toast.assert_not_called()


def test_wrong_pin_shows_toast():
    menu = _make_pin_menu()
    menu.open_with_pin()
    _enter_pin(menu, '1234')
    assert menu.state == MenuState.PIN_ENTRY
    menu._on_toast.assert_called_once_with('Wrong code')
    assert menu.pin_buffer == ''


def test_pin_close_closes_menu():
    menu = _make_pin_menu()
    menu.open_with_pin()
    menu.close = MagicMock(side_effect=lambda: setattr(menu, 'state', MenuState.CLOSED))
    menu.handle_tap((0, 0), {'close': _pin_rect('close')})
    menu.close.assert_called_once()


def test_change_pin_flow():
    menu = _make_pin_menu()
    menu.state = MenuState.CHANGE_PIN
    _enter_pin(menu, DEFAULT_ADMIN_PIN)
    assert menu.change_pin_step == 1
    _enter_pin(menu, '5678')
    assert menu.change_pin_step == 2
    _enter_pin(menu, '5678')
    menu.settings.set_admin_pin.assert_called_once_with('5678')
    menu._on_toast.assert_called_with('PIN changed')
    assert menu.state == MenuState.MAIN


def test_change_pin_mismatch_resets_to_new_pin_step():
    menu = _make_pin_menu()
    menu.state = MenuState.CHANGE_PIN
    _enter_pin(menu, DEFAULT_ADMIN_PIN)
    _enter_pin(menu, '5678')
    _enter_pin(menu, '9999')
    menu._on_toast.assert_called_with('PINs do not match')
    assert menu.change_pin_step == 1
    menu.settings.set_admin_pin.assert_not_called()


def test_backspace_removes_digit():
    menu = _make_pin_menu()
    menu.open_with_pin()
    menu._pin_buffer = '12'
    menu.handle_tap((0, 0), {'pin_back': _pin_rect('back')})
    assert menu.pin_buffer == '1'
