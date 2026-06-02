"""
Setup Menu - WiFi management and library reset.

Extracted from app.py to keep system-admin concerns separate from the player.
"""
import json
import time
import logging
import subprocess
import threading
from typing import Optional, Callable

import shutil

from pathlib import Path

from ..config import CATALOG_PATH, IMAGES_DIR, LIBRESPOT_STATE_PATH, SETTINGS_PATH, PIN_LENGTH
from ..models import MenuState
from ..utils.wifi_info import (
    band_mode_label,
    format_link_detail,
    format_now_band,
    mode_to_nmcli_band,
    next_band_mode,
    nmcli_band_to_mode,
    parse_iw_link_output,
)

_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent)

# SSIDs to exclude when scanning or forgetting WiFi networks
_WIFI_SKIP_SSIDS = {'Moki-Setup', 'moki-ap', 'moki-setup'}

logger = logging.getLogger(__name__)


class SetupMenu:
    """Manages the setup menu overlay (WiFi, library clear, settings)."""

    def __init__(
        self,
        catalog_manager,
        settings,
        on_toast: Callable[[str], None],
        on_invalidate: Callable[[], None],
        on_library_cleared: Callable[[], None],
        bluetooth_manager=None,
        on_volume_preview: Optional[Callable[[int, str, int], None]] = None,
        on_open_home: Optional[Callable[[], None]] = None,
        on_prepare_shutdown: Optional[Callable[[], None]] = None,
        on_enter_voice_test: Optional[Callable[[], None]] = None,
        on_leave_voice_test: Optional[Callable[[], None]] = None,
        on_voice_record_toggle: Optional[Callable[[], None]] = None,
        on_voice_play: Optional[Callable[[], None]] = None,
        on_voice_transcribe: Optional[Callable[[], None]] = None,
        on_enter_music_search: Optional[Callable[[], None]] = None,
        on_music_search_key: Optional[Callable[[str], None]] = None,
        on_music_search_submit: Optional[Callable[[], None]] = None,
        on_music_search_result: Optional[Callable[[int], None]] = None,
        on_suppress_librespot_recovery: Optional[Callable[[float, str], None]] = None,
    ):
        self.catalog_manager = catalog_manager
        self.settings = settings
        self._on_toast = on_toast
        self._on_invalidate = on_invalidate
        self._on_library_cleared = on_library_cleared
        self.bluetooth = bluetooth_manager
        self._on_volume_preview = on_volume_preview
        self._on_open_home = on_open_home or (lambda: None)
        self._on_prepare_shutdown = on_prepare_shutdown or (lambda: None)
        self._on_enter_voice_test = on_enter_voice_test or (lambda: None)
        self._on_leave_voice_test = on_leave_voice_test or (lambda: None)
        self._on_voice_record_toggle = on_voice_record_toggle or (lambda: None)
        self._on_voice_play = on_voice_play or (lambda: None)
        self._on_voice_transcribe = on_voice_transcribe or (lambda: None)
        self._on_enter_music_search = on_enter_music_search or (lambda: None)
        self._on_music_search_key = on_music_search_key or (lambda _key: None)
        self._on_music_search_submit = on_music_search_submit or (lambda: None)
        self._on_music_search_result = on_music_search_result or (lambda _idx: None)
        self._on_suppress_librespot_recovery = on_suppress_librespot_recovery or (lambda _s, _r: None)

        self.state = MenuState.CLOSED
        self.scroll_offset: int = 0  # pixels scrolled in current menu screen
        self.known_networks: list = []
        self.current_network: Optional[str] = None
        self.wifi_now_band: str = ''
        self.wifi_link_detail: str = ''
        self.wifi_band_mode: str = '2.4'
        self.wifi_band_label: str = band_mode_label('2.4')
        self._ssid_to_con_name: dict = {}
        self._wifi_process: Optional[subprocess.Popen] = None
        self._wifi_link_refresh_at: float = 0.0

        # Reset confirmation state
        self._reset_confirm_pending: bool = False
        self._reset_confirm_time: float = 0.0

        # Shutdown confirmation state
        self._shutdown_confirm_pending: bool = False
        self._shutdown_confirm_time: float = 0.0

        # Reboot confirmation state
        self._reboot_confirm_pending: bool = False
        self._reboot_confirm_time: float = 0.0

        # Manual update state
        self._update_available: bool = False
        self._update_checking: bool = False
        self._update_running: bool = False
        self._update_process: Optional[subprocess.Popen] = None

        # PIN entry state
        self._pin_buffer: str = ''
        self._change_pin_step: int = 0
        self._pending_new_pin: Optional[str] = None

    @property
    def pin_buffer(self) -> str:
        return self._pin_buffer

    @property
    def change_pin_step(self) -> int:
        return self._change_pin_step

    @property
    def is_open(self) -> bool:
        return self.state != MenuState.CLOSED

    def _reset_pin_state(self):
        self._pin_buffer = ''
        self._change_pin_step = 0
        self._pending_new_pin = None

    def open_with_pin(self):
        """Open settings behind a PIN gate."""
        logger.info('Setup menu: PIN entry')
        self._reset_pin_state()
        self.state = MenuState.PIN_ENTRY
        self.scroll_offset = 0
        self._on_invalidate()

    def open(self):
        """Open the setup menu overlay (admin bypass, no PIN)."""
        logger.info('Setup menu opened')
        self._reset_pin_state()
        self.state = MenuState.MAIN
        self.scroll_offset = 0
        self.current_network = None
        self._on_invalidate()

    def show_wifi(self):
        """Open directly to the WiFi screen (skipping main menu)."""
        if self.state == MenuState.WIFI_AP:
            logger.info('show_wifi() ignored — AP mode active')
            return
        self._show_wifi_screen()

    def close(self):
        """Close the setup menu, stopping wifi-connect and BT scan if running."""
        logger.info('Setup menu closed')
        if self.state == MenuState.VOICE_TEST:
            self._on_leave_voice_test()
        if self._wifi_process:
            self._kill_wifi_processes()
            self._wifi_process = None
        if self.bluetooth and self.state == MenuState.BT_LIST:
            self.bluetooth.stop_scan()
        self._reset_confirm_pending = False
        self._shutdown_confirm_pending = False
        self._reboot_confirm_pending = False
        self._reset_pin_state()
        self.state = MenuState.CLOSED
        self.current_network = None
        self._on_invalidate()

    def handle_tap(self, pos, button_rects: dict):
        """Handle a tap while the menu is open."""
        x, y = pos

        if 'close' in button_rects and button_rects['close'].collidepoint(x, y):
            if self.state == MenuState.PIN_ENTRY:
                self.close()
            elif self.state == MenuState.CHANGE_PIN:
                self._reset_pin_state()
                self.state = MenuState.MAIN
                self.scroll_offset = 0
                self._on_invalidate()
            elif self.state == MenuState.MAIN:
                self.close()
            elif self.state == MenuState.WIFI_AP:
                self._restore_wifi_autoconnect()
                if self._wifi_process:
                    self._kill_wifi_processes()
                    self._wifi_process = None
                    self._reconnect_to_known_network()
                self.state = MenuState.WIFI_LIST
                self.scroll_offset = 0
                self._on_invalidate()
            else:
                # All other submenus → back to main (or previous search step)
                if self.state == MenuState.MUSIC_SEARCH_RESULTS:
                    self.state = MenuState.MUSIC_SEARCH
                    self.scroll_offset = 0
                    self._on_invalidate()
                    return
                if self.state == MenuState.MUSIC_SEARCH:
                    self.state = MenuState.MAIN
                    self.scroll_offset = 0
                    self._on_invalidate()
                    return
                if self.state == MenuState.BT_LIST and self.bluetooth:
                    self.bluetooth.stop_scan()
                if self.state == MenuState.VOICE_TEST:
                    self._on_leave_voice_test()
                self.state = MenuState.MAIN
                self.scroll_offset = 0
                self._on_invalidate()
            return

        if self.state in (MenuState.PIN_ENTRY, MenuState.CHANGE_PIN):
            self._handle_pin_tap(button_rects, x, y)
            return

        if self.state == MenuState.VOLUME_LEVELS:
            self._handle_volume_tap(button_rects, x, y)
        elif self.state == MenuState.VOICE_TEST:
            self._handle_voice_test_tap(button_rects, x, y)
        elif self.state == MenuState.MUSIC_SEARCH:
            self._handle_music_search_tap(button_rects, x, y)
        elif self.state == MenuState.MUSIC_SEARCH_RESULTS:
            self._handle_music_search_results_tap(button_rects, x, y)
        elif self.state == MenuState.BT_LIST:
            self._handle_bt_tap(button_rects, x, y)
        elif self.state == MenuState.WIFI_LIST:
            if 'wifi_band' in button_rects and button_rects['wifi_band'].collidepoint(x, y):
                self._cycle_wifi_band()
            elif 'new_network' in button_rects and button_rects['new_network'].collidepoint(x, y):
                self._start_wifi_ap()
            else:
                self._check_reconnect_tap(button_rects, x, y)
        elif self.state == MenuState.WIFI_AP:
            self._check_reconnect_tap(button_rects, x, y)
        else:
            if 'shutdown' in button_rects and button_rects['shutdown'].collidepoint(x, y):
                if self._shutdown_confirm_pending:
                    self._shutdown_confirm_pending = False
                    self._shutdown_system()
                else:
                    self._reset_confirm_pending = False
                    self._reboot_confirm_pending = False
                    self._shutdown_confirm_pending = True
                    self._shutdown_confirm_time = time.time()
                    self._on_invalidate()
                return
            if 'reboot' in button_rects and button_rects['reboot'].collidepoint(x, y):
                if self._reboot_confirm_pending:
                    self._reboot_confirm_pending = False
                    self._reboot_system()
                else:
                    self._reset_confirm_pending = False
                    self._shutdown_confirm_pending = False
                    self._reboot_confirm_pending = True
                    self._reboot_confirm_time = time.time()
                    self._on_invalidate()
                return
            if 'reset' in button_rects and button_rects['reset'].collidepoint(x, y):
                if self._reset_confirm_pending:
                    self._reset_confirm_pending = False
                    self._factory_reset()
                else:
                    self._shutdown_confirm_pending = False
                    self._reboot_confirm_pending = False
                    self._reset_confirm_pending = True
                    self._reset_confirm_time = time.time()
                    self._on_invalidate()
                return
            # Any other tap in main menu clears destructive confirmations
            if self._reset_confirm_pending or self._shutdown_confirm_pending or self._reboot_confirm_pending:
                self._reset_confirm_pending = False
                self._shutdown_confirm_pending = False
                self._reboot_confirm_pending = False
                self._on_invalidate()
            if 'home' in button_rects and button_rects['home'].collidepoint(x, y):
                self.close()
                self._on_open_home()
                return
            if 'wifi' in button_rects and button_rects['wifi'].collidepoint(x, y):
                self._show_wifi_screen()
            elif 'bluetooth' in button_rects and button_rects['bluetooth'].collidepoint(x, y):
                self._show_bt_screen()
            elif 'auto_pause' in button_rects and button_rects['auto_pause'].collidepoint(x, y):
                mins = self.settings.cycle_auto_pause()
                self._on_toast(f'Auto-pause: {mins} min')
                self._on_invalidate()
            elif 'progress_expiry' in button_rects and button_rects['progress_expiry'].collidepoint(x, y):
                hours = self.settings.cycle_progress_expiry()
                self._on_toast(f'Remember progress: {hours} hrs')
                self._on_invalidate()
            elif 'volume' in button_rects and button_rects['volume'].collidepoint(x, y):
                self.state = MenuState.VOLUME_LEVELS
                self.scroll_offset = 0
                self._on_invalidate()
            elif 'voice_test' in button_rects and button_rects['voice_test'].collidepoint(x, y):
                self._on_enter_voice_test()
                self.state = MenuState.VOICE_TEST
                self.scroll_offset = 0
                self._on_invalidate()
            elif 'music_search' in button_rects and button_rects['music_search'].collidepoint(x, y):
                self._on_enter_music_search()
                self.state = MenuState.MUSIC_SEARCH
                self.scroll_offset = 0
                self._on_invalidate()
            elif 'change_pin' in button_rects and button_rects['change_pin'].collidepoint(x, y):
                self._reset_pin_state()
                self.state = MenuState.CHANGE_PIN
                self.scroll_offset = 0
                self._on_invalidate()
            elif 'check_update' in button_rects and button_rects['check_update'].collidepoint(x, y):
                if self._update_running or self._update_checking:
                    return
                if self._update_available:
                    self._run_update()
                else:
                    self._check_for_update()

    def handle_scroll(self, delta: int, max_overflow: int):
        """Adjust scroll offset by delta, clamped to valid range."""
        self.scroll_offset = max(0, min(max_overflow, self.scroll_offset + delta))
        self._on_invalidate()

    def update(self):
        """Called each frame to detect wifi-connect / update exit."""
        # Auto-clear destructive confirmations after 4 seconds
        if self._reset_confirm_pending and time.time() - self._reset_confirm_time > 4:
            self._reset_confirm_pending = False
            self._on_invalidate()
        if self._shutdown_confirm_pending and time.time() - self._shutdown_confirm_time > 4:
            self._shutdown_confirm_pending = False
            self._on_invalidate()
        if self._reboot_confirm_pending and time.time() - self._reboot_confirm_time > 4:
            self._reboot_confirm_pending = False
            self._on_invalidate()

        if self.state == MenuState.WIFI_LIST:
            now = time.time()
            if now - self._wifi_link_refresh_at >= 3.0:
                self._refresh_wifi_link_info()
                self._wifi_link_refresh_at = now

        # Monitor manual update process
        if self._update_process is not None:
            ret = self._update_process.poll()
            if ret is not None:
                self._update_process = None
                self._update_running = False
                if ret != 0:
                    self._on_toast('Update failed')
                self._update_available = False
                self._on_invalidate()

        if self.state == MenuState.WIFI_AP and self._wifi_process:
            ret = self._wifi_process.poll()
            if ret is not None:
                self._wifi_process = None
                self._restore_wifi_autoconnect()
                if ret == 0:
                    logger.info('wifi-connect exited (code=0)')
                    self._on_toast('WiFi connected!')
                    self.close()
                else:
                    logger.info(f'wifi-connect exited (code={ret})')
                    self._reconnect_to_known_network()
                    self._show_wifi_screen()

    # ------------------------------------------------------------------
    # PIN entry
    # ------------------------------------------------------------------

    def _handle_pin_tap(self, button_rects: dict, x: int, y: int):
        for key, rect in button_rects.items():
            if not key.startswith('pin_') or not rect.collidepoint(x, y):
                continue
            suffix = key[4:]
            if suffix == 'back':
                self._pin_buffer = self._pin_buffer[:-1]
            elif suffix == 'ok':
                self._submit_pin()
            elif suffix.isdigit() and len(suffix) == 1:
                if len(self._pin_buffer) < PIN_LENGTH:
                    self._pin_buffer += suffix
                    if len(self._pin_buffer) == PIN_LENGTH:
                        self._submit_pin()
            self._on_invalidate()
            return

    def _submit_pin(self):
        if len(self._pin_buffer) != PIN_LENGTH:
            return

        entered = self._pin_buffer
        self._pin_buffer = ''

        if self.state == MenuState.PIN_ENTRY:
            if entered == self.settings.admin_pin:
                logger.info('PIN entry accepted')
                self.state = MenuState.MAIN
                self.scroll_offset = 0
            else:
                logger.info('PIN entry rejected')
                self._on_toast('Wrong code')
            self._on_invalidate()
            return

        if self.state == MenuState.CHANGE_PIN:
            if self._change_pin_step == 0:
                if entered == self.settings.admin_pin:
                    self._change_pin_step = 1
                else:
                    self._on_toast('Wrong code')
            elif self._change_pin_step == 1:
                self._pending_new_pin = entered
                self._change_pin_step = 2
            elif self._change_pin_step == 2:
                if entered == self._pending_new_pin:
                    self.settings.set_admin_pin(entered)
                    self._on_toast('PIN changed')
                    self._reset_pin_state()
                    self.state = MenuState.MAIN
                else:
                    self._on_toast('PINs do not match')
                    self._pending_new_pin = None
                    self._change_pin_step = 1
            self._on_invalidate()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_for_update(self):
        """Check if a newer version is available on the remote branch."""
        self._update_checking = True
        self._update_available = False
        self._on_invalidate()

        def _check():
            try:
                # Verify git repo is healthy before checking
                git_dir_check = subprocess.run(
                    ['git', '-C', _REPO_DIR, 'rev-parse', '--git-dir'],
                    capture_output=True, timeout=5,
                )
                if git_dir_check.returncode != 0:
                    logger.warning('Git repo broken — triggering auto-update to re-clone')
                    self._on_toast('Repairing install...')
                    self._update_checking = False
                    self._on_invalidate()
                    self._run_update()
                    return

                branch = subprocess.run(
                    ['git', '-C', _REPO_DIR, 'rev-parse', '--abbrev-ref', 'HEAD'],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip() or 'main'
                result = subprocess.run(
                    ['git', '-C', _REPO_DIR, 'fetch', 'origin', branch],
                    capture_output=True, timeout=15,
                )
                if result.returncode != 0:
                    self._on_toast('No internet?')
                    self._update_checking = False
                    self._on_invalidate()
                    return
                local = subprocess.run(
                    ['git', '-C', _REPO_DIR, 'rev-parse', 'HEAD'],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                remote = subprocess.run(
                    ['git', '-C', _REPO_DIR, 'rev-parse', f'origin/{branch}'],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                self._update_available = local != remote
                if self._update_available:
                    self._on_toast('Update available!')
                else:
                    self._on_toast('Up to date')
            except Exception as e:
                logger.error(f'Update check failed: {e}')
                self._on_toast('Check failed')
            finally:
                self._update_checking = False
                self._on_invalidate()

        threading.Thread(target=_check, daemon=True).start()

    def _run_update(self):
        """Trigger the auto-update script (will restart the app)."""
        self._update_running = True
        self._on_invalidate()
        self._on_toast("Updating... don't unplug")
        try:
            self._update_process = subprocess.Popen(
                ['bash', f'{_REPO_DIR}/pi/auto-update.sh'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            logger.info('Manual update triggered')
        except Exception as e:
            logger.error(f'Failed to start update: {e}')
            self._update_running = False
            self._on_toast('Update failed')
            self._on_invalidate()

    def _handle_volume_tap(self, button_rects: dict, x: int, y: int):
        """Handle taps on the volume settings screen (+/- buttons)."""
        for key, rect in button_rects.items():
            if not rect.collidepoint(x, y):
                continue
            # Keys are like "vol_plus_0_speaker", "vol_minus_1_bt"
            if key.startswith('vol_'):
                parts = key.split('_')  # ['vol', 'plus'/'minus', index, type]
                if len(parts) == 4:
                    delta = 1 if parts[1] == 'plus' else -1
                    level_idx = int(parts[2])
                    output_type = parts[3]
                    new_val = self.settings.adjust_volume(level_idx, output_type, delta)
                    if self._on_volume_preview:
                        self._on_volume_preview(level_idx, output_type, new_val)
                    self._on_invalidate()
                break

    def _handle_voice_test_tap(self, button_rects: dict, x: int, y: int):
        if 'voice_record' in button_rects and button_rects['voice_record'].collidepoint(x, y):
            self._on_voice_record_toggle()
            self._on_invalidate()
            return
        if 'voice_play' in button_rects and button_rects['voice_play'].collidepoint(x, y):
            self._on_voice_play()
            self._on_invalidate()
            return
        if 'voice_transcribe' in button_rects and button_rects['voice_transcribe'].collidepoint(x, y):
            self._on_voice_transcribe()
            self._on_invalidate()

    def _handle_music_search_tap(self, button_rects: dict, x: int, y: int):
        for key, rect in button_rects.items():
            if not rect.collidepoint(x, y):
                continue
            if key == 'search_go':
                self._on_music_search_submit()
                return
            if key == 'search_space':
                self._on_music_search_key('space')
                self._on_invalidate()
                return
            if key == 'search_back':
                self._on_music_search_key('back')
                self._on_invalidate()
                return
            if key.startswith('search_key_'):
                self._on_music_search_key(key[len('search_key_'):])
                self._on_invalidate()
                return

    def _handle_music_search_results_tap(self, button_rects: dict, x: int, y: int):
        if 'search_retry' in button_rects and button_rects['search_retry'].collidepoint(x, y):
            self._on_music_search_submit()
            return
        for key, rect in button_rects.items():
            if key.startswith('search_result_') and rect.collidepoint(x, y):
                idx = int(key.split('_')[-1])
                self._on_music_search_result(idx)
                return

    def _show_bt_screen(self):
        logger.info('Setup menu: Bluetooth screen')
        self.state = MenuState.BT_LIST
        self.scroll_offset = 0
        self._on_invalidate()
        if self.bluetooth:
            self.bluetooth.refresh_paired()
            self.bluetooth.start_scan()

    def _handle_bt_tap(self, button_rects: dict, x: int, y: int):
        if not self.bluetooth:
            return
        for key, rect in button_rects.items():
            if not rect.collidepoint(x, y):
                continue
            if key.startswith('bt_paired_'):
                idx = int(key.split('_')[2])
                paired = self.bluetooth.paired_devices
                if idx < len(paired):
                    dev = paired[idx]
                    if dev.connected:
                        self.bluetooth.disconnect()
                        self._on_toast(f'{dev.name} disconnected')
                    else:
                        self._on_toast(f'Connecting to {dev.name}...')
                        self.bluetooth.connect(dev.mac)
                break
            elif key.startswith('bt_discovered_'):
                idx = int(key.split('_')[2])
                discovered = self.bluetooth.discovered_devices
                if idx < len(discovered):
                    dev = discovered[idx]
                    self.bluetooth.pair_and_connect(dev.mac, dev.name)
                break

    def _notify_wifi_reconnect_started(self, reason: str, seconds: Optional[float] = None):
        """Tell the app to pause librespot recovery while the link is flapping."""
        from ..config import (
            LIBRESPOT_RECOVERY_WIFI_AP_SUPPRESS_SEC,
            LIBRESPOT_RECOVERY_WIFI_SUPPRESS_SEC,
        )
        duration = seconds if seconds is not None else LIBRESPOT_RECOVERY_WIFI_SUPPRESS_SEC
        if reason == 'wifi_ap_setup':
            duration = max(duration, LIBRESPOT_RECOVERY_WIFI_AP_SUPPRESS_SEC)
        self._on_suppress_librespot_recovery(duration, reason)

    def _check_reconnect_tap(self, button_rects: dict, x: int, y: int):
        for key, rect in button_rects.items():
            if key.startswith('reconnect_') and rect.collidepoint(x, y):
                idx = int(key.split('_')[1])
                if idx < len(self.known_networks):
                    self._reconnect_wifi(self.known_networks[idx])
                break

    def _resolve_ssid(self, con_name: str) -> str:
        """Get the actual SSID for a connection profile name."""
        try:
            result = subprocess.run(
                ['nmcli', '-g', '802-11-wireless.ssid', 'con', 'show', con_name],
                capture_output=True, text=True, timeout=3,
            )
            ssid = result.stdout.strip()
            if ssid:
                return ssid
        except Exception as e:
            logger.debug(f'Could not resolve SSID for {con_name}: {e}')
        return con_name

    def _active_wifi_connection_name(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show', '--active'],
                capture_output=True, text=True, timeout=3,
            )
            for line in result.stdout.splitlines():
                if ':802-11-wireless' in line:
                    return line.split(':', 1)[0]
        except Exception as e:
            logger.debug(f'Could not read active WiFi profile: {e}')
        return None

    def _refresh_wifi_link_info(self):
        """Read live band/signal from iw and preferred band from NetworkManager."""
        connected = False
        freq = signal = ssid = None
        try:
            result = subprocess.run(
                ['iw', 'dev', 'wlan0', 'link'],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0 and 'Connected to' in result.stdout:
                connected = True
                freq, signal, ssid = parse_iw_link_output(result.stdout)
        except Exception as e:
            logger.debug(f'Could not read WiFi link info: {e}')

        self.wifi_now_band = format_now_band(connected, freq)
        self.wifi_link_detail = format_link_detail(ssid, signal, connected)

        con_name = self._active_wifi_connection_name()
        if con_name:
            self._load_wifi_band_pref(con_name)
        self._on_invalidate()

    def _load_wifi_band_pref(self, con_name: str):
        try:
            result = subprocess.run(
                ['nmcli', '-g', '802-11-wireless.band', 'con', 'show', con_name],
                capture_output=True, text=True, timeout=3,
            )
            self.wifi_band_mode = nmcli_band_to_mode(result.stdout)
        except Exception as e:
            logger.debug(f'Could not read WiFi band preference: {e}')
        self.wifi_band_label = band_mode_label(self.wifi_band_mode)

    def _apply_wifi_band_pref(self, con_name: str, mode: str):
        band = mode_to_nmcli_band(mode)
        try:
            if band:
                subprocess.run(
                    ['sudo', 'nmcli', 'con', 'modify', con_name, '802-11-wireless.band', band],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
                )
            else:
                subprocess.run(
                    ['sudo', 'nmcli', 'con', 'modify', con_name, '802-11-wireless.band', ''],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
                )
            logger.info(f'WiFi band preference set to {mode} for {con_name}')
        except Exception as e:
            logger.warning(f'Could not set WiFi band preference: {e}')
            self._on_toast('Band change failed')
            return False
        return True

    def _cycle_wifi_band(self):
        con_name = self._active_wifi_connection_name()
        if not con_name:
            con_name = self._ssid_to_con_name.get(self.current_network or '')
        if not con_name:
            self._on_toast('No WiFi profile')
            return

        new_mode = next_band_mode(self.wifi_band_mode)
        if not self._apply_wifi_band_pref(con_name, new_mode):
            return

        self.wifi_band_mode = new_mode
        self.wifi_band_label = band_mode_label(new_mode)
        self._on_toast(self.wifi_band_label)
        self._on_invalidate()

        def _reconnect():
            self._notify_wifi_reconnect_started('wifi_band_change')
            try:
                subprocess.run(
                    ['sudo', 'nmcli', 'con', 'up', con_name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                )
            except Exception as e:
                logger.warning(f'WiFi reconnect after band change failed: {e}')

        threading.Thread(target=_reconnect, daemon=True).start()

    def _collect_known_networks(self):
        """Populate known_networks and current_network via nmcli."""
        try:
            active_result = subprocess.run(
                ['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show', '--active'],
                capture_output=True, text=True, timeout=3,
            )
            active_con_names = [
                line.split(':')[0]
                for line in active_result.stdout.strip().split('\n')
                if line and '802-11-wireless' in line
            ]
            all_result = subprocess.run(
                ['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show'],
                capture_output=True, text=True, timeout=3,
            )
            all_con_names = [
                line.split(':')[0]
                for line in all_result.stdout.strip().split('\n')
                if line and '802-11-wireless' in line
            ]
            skip = _WIFI_SKIP_SSIDS
            seen = set()
            ordered = []
            ssid_map = {}
            active_ssids = []
            for con_name in active_con_names + all_con_names:
                if not con_name or con_name in seen or con_name in skip:
                    continue
                seen.add(con_name)
                ssid = self._resolve_ssid(con_name)
                if ssid in skip or ssid in ssid_map:
                    continue
                ssid_map[ssid] = con_name
                ordered.append(ssid)
                if con_name in active_con_names:
                    active_ssids.append(ssid)
            self._ssid_to_con_name = ssid_map
            self.known_networks = ordered
            self.current_network = active_ssids[0] if active_ssids else None
            logger.info(f'Known WiFi: {self.known_networks}, current: {self.current_network}')
        except Exception as e:
            logger.warning(f'Could not read WiFi connections: {e}')
            self.known_networks = []
            self.current_network = None
            self._ssid_to_con_name = {}

    def _show_wifi_screen(self):
        logger.info('Setup menu: WiFi screen')
        self._collect_known_networks()
        self._refresh_wifi_link_info()
        self._wifi_link_refresh_at = time.time()
        self.state = MenuState.WIFI_LIST
        self.scroll_offset = 0
        self._on_invalidate()

    def _start_wifi_ap(self):
        logger.info('Setup menu: starting wifi-connect AP')
        # Clear _wifi_process BEFORE killing old processes, so update()
        # won't treat the old process's clean exit (code=0) as success.
        self._wifi_process = None
        self.state = MenuState.WIFI_AP
        self.scroll_offset = 0
        self._on_invalidate()

        def _prepare_and_launch():
            try:
                subprocess.run(
                    ['sudo', 'nmcli', 'device', 'wifi', 'rescan'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                )
                time.sleep(2)
            except Exception as e:
                logger.warning(f'WiFi rescan failed: {e}')
            # Disable autoconnect BEFORE disconnect — disconnect fails when
            # wlan0 is already disconnected (exit code 6) and won't suppress
            # autoconnect, letting NM reclaim wlan0 from the AP.
            try:
                subprocess.run(
                    ['sudo', 'nmcli', 'device', 'set', 'wlan0', 'autoconnect', 'no'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
                )
            except Exception:
                pass
            try:
                subprocess.run(
                    ['sudo', 'nmcli', 'device', 'disconnect', 'wlan0'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                )
            except Exception:
                pass
            self._kill_wifi_processes()
            self._delete_stale_ap_profile()
            self._launch_wifi_connect()

        threading.Thread(target=_prepare_and_launch, daemon=True).start()

    def _launch_wifi_connect(self):
        self._notify_wifi_reconnect_started('wifi_ap_setup')
        try:
            self._wifi_process = subprocess.Popen(
                ['sudo', 'wifi-connect',
                 '--portal-ssid', 'Moki-Setup',
                 '--ui-directory', '/usr/local/share/wifi-connect/ui'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            logger.info('wifi-connect started')

            def _log_output():
                for line in self._wifi_process.stdout:
                    logger.info(f'wifi-connect: {line.decode().rstrip()}')

            threading.Thread(target=_log_output, daemon=True).start()
        except Exception as e:
            logger.error(f'Failed to start wifi-connect: {e}')

    def _kill_wifi_processes(self):
        """Kill any lingering wifi-connect and dnsmasq processes.

        wifi-connect spawns dnsmasq as a child. When we terminate only the
        sudo wrapper, the real wifi-connect binary and dnsmasq survive,
        holding port 80 on 192.168.42.1 and causing the next launch to
        fail with 'Address already in use'.
        """
        for name in ('wifi-connect', 'dnsmasq'):
            try:
                subprocess.run(
                    ['sudo', 'pkill', '-f' if name == 'wifi-connect' else '-x', name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
                )
            except Exception:
                pass
        logger.info('Killed stale wifi-connect/dnsmasq processes')

    def _delete_stale_ap_profile(self):
        """Delete leftover Moki-Setup AP profile from NetworkManager.

        When wifi-connect exits, it leaves the AP connection profile behind.
        On the next launch, wifi-connect sees the old profile and deletes it
        internally, but this triggers NetworkManager to briefly activate the
        profile's dnsmasq — which then holds the port when wifi-connect tries
        to start its own dnsmasq. Deleting the profile upfront avoids this.
        """
        try:
            result = subprocess.run(
                ['sudo', 'nmcli', 'con', 'delete', 'Moki-Setup'],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                logger.info('Deleted stale Moki-Setup AP profile')
            time.sleep(1)
        except Exception:
            pass

    def _restore_wifi_autoconnect(self):
        """Re-enable NM autoconnect on wlan0 after leaving AP mode."""
        try:
            subprocess.run(
                ['sudo', 'nmcli', 'device', 'set', 'wlan0', 'autoconnect', 'yes'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
            )
        except Exception:
            pass

    def _reconnect_to_known_network(self):
        if self.known_networks:
            ssid = self.known_networks[0]
            con_name = self._ssid_to_con_name.get(ssid, ssid)
            logger.info(f'Auto-reconnecting to known network: {ssid} (con: {con_name})')
            self._notify_wifi_reconnect_started('wifi_auto_reconnect')
            try:
                self._force_wifi_24ghz(con_name)
                subprocess.Popen(
                    ['sudo', 'nmcli', 'con', 'up', con_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                logger.error(f'Auto-reconnect failed: {e}')
        else:
            logger.warning('No known networks to reconnect to')

    def _reconnect_wifi(self, ssid: str):
        con_name = self._ssid_to_con_name.get(ssid, ssid)
        logger.info(f'Setup menu: Reconnect to {ssid} (con: {con_name})')
        self._notify_wifi_reconnect_started('wifi_manual_reconnect')

        if self._wifi_process:
            self._kill_wifi_processes()
            self._wifi_process = None

        try:
            self._force_wifi_24ghz(con_name)
            subprocess.Popen(
                ['sudo', 'nmcli', 'con', 'up', con_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._on_toast('Connecting...')
        except Exception as e:
            logger.error(f'Reconnect failed: {e}')
            self._on_toast('Connection failed')

        self.close()

    def _force_wifi_24ghz(self, con_name: str):
        """Prefer 2.4 GHz for better range through walls on Pi 3 WiFi."""
        self._apply_wifi_band_pref(con_name, '2.4')

    def _factory_reset(self):
        """Full factory reset: catalog, settings, Spotify, Bluetooth, WiFi."""
        logger.info('Setup menu: Factory reset')

        # 1. Clear catalog and progress
        try:
            if CATALOG_PATH.exists():
                CATALOG_PATH.unlink()
            self.catalog_manager.clear_all_progress()
            self._on_library_cleared()
            logger.info('Catalog cleared')
        except Exception as e:
            logger.error(f'Failed to clear catalog: {e}')

        # 2. Clear Spotify credentials
        try:
            if LIBRESPOT_STATE_PATH.exists():
                state = json.loads(LIBRESPOT_STATE_PATH.read_text())
                state['credentials'] = {'username': '', 'data': None}
                LIBRESPOT_STATE_PATH.write_text(json.dumps(state))
                logger.info('Spotify credentials cleared')
        except Exception as e:
            logger.error(f'Failed to clear Spotify credentials: {e}')

        # 3. Delete settings (auto-pause, volume, BT device memory)
        try:
            if SETTINGS_PATH.exists():
                SETTINGS_PATH.unlink()
                logger.info('Settings deleted')
        except Exception as e:
            logger.error(f'Failed to delete settings: {e}')

        # 4. Delete cached album images
        try:
            if IMAGES_DIR.exists():
                shutil.rmtree(IMAGES_DIR)
                logger.info('Image cache deleted')
        except Exception as e:
            logger.error(f'Failed to delete image cache: {e}')

        # 5. Forget all Bluetooth paired devices
        try:
            subprocess.run(
                ['bluetoothctl', 'disconnect'],
                capture_output=True, timeout=5,
            )
            result = subprocess.run(
                ['bluetoothctl', 'devices', 'Paired'],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    mac = parts[1]
                    subprocess.run(
                        ['bluetoothctl', 'remove', mac],
                        capture_output=True, timeout=5,
                    )
            logger.info('Bluetooth devices forgotten')
        except Exception as e:
            logger.error(f'Failed to forget Bluetooth devices: {e}')

        # 6. Forget all WiFi networks (keep Moki-Setup AP)
        try:
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show'],
                capture_output=True, text=True, timeout=5,
            )
            skip = _WIFI_SKIP_SSIDS
            for line in result.stdout.strip().splitlines():
                if '802-11-wireless' in line:
                    name = line.split(':')[0]
                    if name and name not in skip:
                        subprocess.run(
                            ['sudo', 'nmcli', 'con', 'delete', name],
                            capture_output=True, timeout=5,
                        )
            logger.info('WiFi networks forgotten')
        except Exception as e:
            logger.error(f'Failed to forget WiFi networks: {e}')

        # 7. Restart app
        def _restart_app():
            time.sleep(2)
            try:
                subprocess.run(
                    ['sudo', 'systemctl', 'restart', 'moki-native'],
                    timeout=10,
                )
            except Exception as ex:
                logger.warning(f'Could not restart moki-native: {ex}')
        threading.Thread(target=_restart_app, daemon=True).start()

        self._on_toast('Reset complete')
        self.close()

    def _shutdown_system(self):
        """Gracefully save state and power off the Pi."""
        logger.info('Setup menu: Shutdown confirmed')
        self._on_prepare_shutdown()
        self._on_toast('Shutting down...')
        self._on_invalidate()

        def _poweroff():
            time.sleep(1.5)
            try:
                subprocess.run(
                    ['sudo', '/usr/bin/systemctl', 'poweroff'],
                    timeout=15,
                )
            except Exception as ex:
                logger.error(f'Shutdown failed: {ex}')
                self._on_toast('Shutdown failed')

        threading.Thread(target=_poweroff, daemon=True).start()
        self.close()

    def _reboot_system(self):
        """Gracefully save state and reboot the Pi."""
        logger.info('Setup menu: Reboot confirmed')
        self._on_prepare_shutdown()
        self._on_toast('Rebooting...')
        self._on_invalidate()

        def _reboot():
            time.sleep(1.5)
            try:
                subprocess.run(
                    ['sudo', '/usr/bin/systemctl', 'reboot'],
                    timeout=15,
                )
            except Exception as ex:
                logger.error(f'Reboot failed: {ex}')
                self._on_toast('Reboot failed')

        threading.Thread(target=_reboot, daemon=True).start()
        self.close()
