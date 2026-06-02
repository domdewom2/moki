"""
Moki Application - Main application class.
"""
import os
import time
import signal
import logging
import subprocess
import threading
from pathlib import Path
from typing import Optional, List

import pygame
import requests

from .config import (
    SCREEN_WIDTH, SCREEN_HEIGHT,
    LIBRESPOT_URL, LIBRESPOT_WS,
    CATALOG_PATH, PROGRESS_PATH, IMAGES_DIR, ICONS_DIR, APP_LOGO, ASSETS_DIR,
    MOCK_MODE,
    COVER_SIZE, COVER_SIZE_SMALL, COVER_SPACING,
    CAROUSEL_X, CAROUSEL_CENTER_Y, CONTROLS_X, BTN_SIZE, PLAY_BTN_SIZE, BTN_SPACING,
    HOME_BTN_Y, RELOAD_BTN_Y, MIC_BTN_Y, HEADPHONE_BTN_Y, HEADPHONE_BTN_Y_CHECKPOD,
    CAROUSEL_TOUCH_MARGIN, MAX_SWIPE_JUMP, VELOCITY_THRESHOLDS,
    ACTION_DEBOUNCE, BUTTON_PRESS_DURATION, MENU_HOLD_TIME,
    CONTEXT_SWITCH_WATCHDOG_TIMEOUT,
    PROGRESS_SAVE_INTERVAL,
    STATUS_READY_MAX_AGE,
    STATUS_READY_WAKE_MAX_AGE,
    STATUS_READY_WAKE_GRACE_SEC,
    LIBRESPOT_RECOVERY_CONTEXT_STALL_SEC,
    LIBRESPOT_RECOVERY_WIFI_SUPPRESS_SEC,
    SNAP_PAUSE_SETTLE_SEC,
    MANUAL_PLAY_SUPPRESS_SEC,
    POSTHOG_API_KEY, POSTHOG_HOST, ANALYTICS_DISTINCT_ID,
    ANALYTICS_INCLUDE_CONTENT, ANALYTICS_USE_MACHINE_ID,
    HOME_ICON_SIZE, HOME_ICON_SCREEN_X_FRAC, HOME_ICON_SCREEN_Y_FRAC,
    HOME_CHECKER_ICON_SCREEN_Y_FRAC, HOME_LOCAL_MUSIC_ICON_SCREEN_Y_FRAC,
    HOME_RADIO_ICON_SCREEN_Y_FRAC, HOME_SETTINGS_ICON_SCREEN_Y_FRAC, MPV_SOCKET_PATH,
    RADIO_TEDDY_STREAM_URL, RADIO_TEDDY_CONTEXT_URI, RADIO_TEDDY_NAME, RADIO_TEDDY_IMAGE_PATH,
    VOICE_TEST_LAST_PATH, VOICE_SEARCH_LAST_PATH, VOICE_TEST_PLAYBACK_SPEAKER_BOOST,
    VOICE_SEARCH_MAX_SECONDS, VOICE_SEARCH_RESULT_LIMIT, VOICE_SEARCH_MP3_BITRATE,
    VOICE_SEARCH_API_PROBE_MAX_WAIT, VOICE_SEARCH_API_READ_TIMEOUT,
    VOICE_SEARCH_API_CONNECT_TIMEOUT,
    VOICE_SEARCH_COUNTDOWN_SECONDS,
    MOKIBOT_DIR, MOKIBOT_RECORD_PATH, MOKIBOT_TTS_PATH, MOKIBOT_TTS_CONTEXT_URI,
    MOKIBOT_MAX_SECONDS,
    CHECKPOD_LOAD_MORE_THRESHOLD,
)
from .models import (
    CatalogItem, NowPlaying, LibrespotStatus, MenuState, AppScreen, SearchResult,
    VoiceSearchPhase, MokiBotPhase, AssistantResponse,
)
from .api import LibrespotAPI, NullLibrespotAPI, CatalogManager
from .api import moki_search
from .api import moki_transcribe
from .api import moki_assistant
from .api.search_covers import prefetch_covers, prefetch_covers_incremental
from .handlers import TouchHandler, EventListener, EvdevTouchHandler
from .managers import (
    SleepManager, SmoothCarousel, PlayTimer, PerformanceMonitor, AutoPauseManager,
    SetupMenu, Settings, UsageTracker, BluetoothManager, CheckPodManager, LocalMusicManager,
    LibrespotRecoveryManager,
)
from .controllers import (
    VolumeController, PlaybackController, is_repeatable_spotify_context,
    LocalPlaybackController, VoiceRecorderController,
)
from .ui.home_launcher import (
    HomeAppEntry, home_page_count, app_center, icon_hit_rect, visible_app_indices,
)
from .ui import ImageCache, Renderer, RenderContext
from .utils import (
    run_async,
    get_runtime_version_label,
    set_system_volume,
    configure_wm8960_mic_once,
    mute_wm8960_output,
    unmute_wm8960_output,
)

logger = logging.getLogger(__name__)


class Moki:
    """Main Moki application."""
    
    def __init__(self, fullscreen: bool = False):
        # Restore display BEFORE pygame takes over DRM device.
        # Previous run may have been killed during sleep, leaving
        # backlight/DPMS off. Must happen before kmsdrm init.
        SleepManager.restore_display()

        # Fast path: init display and show boot splash BEFORE the heavy
        # pygame.init() so the user sees the logo instead of a black screen.
        self._setup_video_driver()
        self._init_display_early(fullscreen)

        # Now init remaining subsystems (splash is already visible).
        # pygame.init() is idempotent for already-inited subsystems.
        pygame.init()
        pygame.mixer.quit()  # Release audio device for go-librespot
        pygame.display.set_caption('Moki')

        self._init_components()
    
    def _check_kms_available(self) -> bool:
        """Check if KMS/DRM is likely configured on the system."""
        # Check for DRI devices (KMS/DRM creates these)
        if os.path.exists('/dev/dri'):
            try:
                dri_devices = os.listdir('/dev/dri')
                # Should have at least card0 or renderD128
                if any(dev.startswith(('card', 'renderD')) for dev in dri_devices):
                    return True
            except OSError:
                pass
        
        # Check if GL driver is configured (check for vc4-kms-v3d overlay)
        try:
            if os.path.exists('/boot/config.txt'):
                with open('/boot/config.txt', 'r') as f:
                    config = f.read()
                    # Check for KMS-related overlays
                    if (
                        'dtoverlay=vc4-kms-v3d' in config
                        or 'dtoverlay=vc4-kms-dsi-7inch' in config
                        or 'dtoverlay=vc4-kms-dsi-ili9881-5inch' in config
                    ):
                        return True
        except (OSError, IOError):
            pass

        try:
            if os.path.exists('/boot/firmware/config.txt'):
                with open('/boot/firmware/config.txt', 'r') as f:
                    config = f.read()
                    if (
                        'dtoverlay=vc4-kms-v3d' in config
                        or 'dtoverlay=vc4-kms-dsi-7inch' in config
                        or 'dtoverlay=vc4-kms-dsi-ili9881-5inch' in config
                    ):
                        return True
        except (OSError, IOError):
            pass
        
        return False
    
    def _setup_video_driver(self):
        """Select optimal video driver (env var only, no init)."""
        if os.environ.get('SDL_VIDEODRIVER'):
            return
        if not os.path.exists('/proc/device-tree/model'):
            return
        if self._check_kms_available():
            os.environ['SDL_VIDEODRIVER'] = 'kmsdrm'

    def _init_display_early(self, fullscreen: bool):
        """Init display and show boot splash as fast as possible.

        Called BEFORE pygame.init() so the logo appears immediately
        instead of a black screen while subsystems initialize.
        """
        _t0 = time.monotonic()
        # Init display subsystem only (not all of pygame)
        try:
            pygame.display.init()
            logger.info(f'Display driver: {os.environ.get("SDL_VIDEODRIVER", "default")} '
                        f'(display.init took {time.monotonic()-_t0:.2f}s)')
        except pygame.error as e:
            # kmsdrm failed — fall back to default driver
            driver = os.environ.pop('SDL_VIDEODRIVER', None)
            if driver:
                logger.warning(f'{driver} driver failed ({e}), using default')
            pygame.display.init()

        flags = pygame.DOUBLEBUF
        if fullscreen:
            flags |= pygame.FULLSCREEN

        _t1 = time.monotonic()
        try:
            self.screen = pygame.display.set_mode(
                (SCREEN_WIDTH, SCREEN_HEIGHT),
                flags | pygame.HWSURFACE
            )
        except pygame.error:
            self.screen = pygame.display.set_mode(
                (SCREEN_WIDTH, SCREEN_HEIGHT),
                flags
            )
        logger.info(f'set_mode took {time.monotonic()-_t1:.2f}s')

        # Show boot splash immediately — bridges Plymouth → app transition
        self._show_boot_splash()

        self.clock = pygame.time.Clock()
        pygame.mouse.set_visible(not fullscreen)

        self._log_video_info()

    def _show_boot_splash(self):
        """Show Moki logo on dark background with gradient.

        Plymouth shows plain black during early boot.  Once pygame takes
        over the DRM device we paint the logo + top gradient so the user
        sees something familiar while the rest of the app initialises.
        """
        try:
            logo_path = os.path.join(ICONS_DIR, APP_LOGO)
            if not os.path.exists(logo_path):
                return
            logo = pygame.image.load(logo_path).convert_alpha()
            # Scale to 320px wide (same as idle screen)
            logo_width = 320
            scale = logo_width / logo.get_width()
            logo = pygame.transform.smoothscale(
                logo, (logo_width, int(logo.get_height() * scale))
            )
            # Rotate for portrait display
            logo = pygame.transform.rotate(logo, -90)

            # Background with top gradient (matches carousel background)
            bg = (13, 13, 13)
            self.screen.fill(bg)
            for offset in range(150):
                x = SCREEN_WIDTH - 1 - offset
                alpha = int(30 * (1 - offset / 150))
                color = (
                    min(255, bg[0] + int(alpha * 0.75)),
                    min(255, bg[1] + int(alpha * 0.4)),
                    min(255, bg[2] + alpha),
                )
                pygame.draw.line(self.screen, color, (x, 0), (x, SCREEN_HEIGHT))

            x = (self.screen.get_width() - logo.get_width()) // 2
            y = (self.screen.get_height() - logo.get_height()) // 2
            self.screen.blit(logo, (x, y))
            pygame.display.flip()
            logger.info('Boot splash displayed')
        except Exception as e:
            logger.warning(f'Could not show boot splash: {e}')

    def _init_components(self):
        """Initialize all application components."""
        self.app_version_label = get_runtime_version_label()
        logger.info(f'App version: {self.app_version_label}')

        # Mock mode
        self.mock_mode = MOCK_MODE
        
        # API & Catalog (use NullAPI in mock mode)
        self.api = NullLibrespotAPI() if self.mock_mode else LibrespotAPI(LIBRESPOT_URL)
        self.settings = Settings()
        self.catalog_manager = CatalogManager(
            CATALOG_PATH, IMAGES_DIR, mock_mode=self.mock_mode,
            progress_path=PROGRESS_PATH,
            get_progress_expiry=lambda: self.settings.progress_expiry_hours,
        )
        self.catalog_manager.load()
        
        # UI Components
        self.image_cache = ImageCache(IMAGES_DIR)
        self.icons = self._load_icons()
        home_bg, home_apps = self._load_home_assets()
        self.renderer = Renderer(
            self.screen, self.image_cache, self.icons,
            home_background=home_bg,
            home_apps=home_apps,
        )
        self.home_pager = SmoothCarousel()
        self._home_touch = TouchHandler()
        self._home_touch_active = False
        self._sync_home_pager_limits()
        self.app_screen = AppScreen.HOME
        
        # Handlers
        self.touch = TouchHandler()
        self.events = EventListener(LIBRESPOT_WS, self._on_ws_update, self._on_ws_reconnect)
        
        # Evdev touch handler for KMSDRM mode (reads /dev/input directly)
        self.evdev_touch = EvdevTouchHandler(SCREEN_WIDTH, SCREEN_HEIGHT)
        touch_available = self.evdev_touch.start()  # Starts background thread if touchscreen found
        
        # Managers
        self.sleep_manager = SleepManager()
        if not touch_available and not self.mock_mode:
            self._disable_sleep_for_touch(
                self.evdev_touch.consume_failure_reason() or 'touchscreen unavailable at startup'
            )
        self._log_startup_health()
        self.carousel = SmoothCarousel()
        self.play_timer = PlayTimer()
        self.perf_monitor = PerformanceMonitor()
        self.volume = VolumeController(self.api, self.settings)
        # Usage analytics (only enabled if user opted in during install)
        analytics_key = POSTHOG_API_KEY if self.settings.share_usage_data else ''
        self.tracker = UsageTracker(
            api_key=analytics_key,
            host=POSTHOG_HOST,
            distinct_id=ANALYTICS_DISTINCT_ID,
            include_content=ANALYTICS_INCLUDE_CONTENT,
            use_machine_id=ANALYTICS_USE_MACHINE_ID,
        )
        
        self.auto_pause = AutoPauseManager(
            on_pause=lambda: (self.tracker.on_auto_pause(), run_async(self.api.pause)),
            get_volume=lambda: self.volume.speaker_level,
            get_timeout=lambda: self.settings.auto_pause_timeout,
        )
        
        # Playback controller (owns play/pause/resume, progress, navigation pause)
        self.playback = PlaybackController(
            api=self.api,
            catalog_manager=self.catalog_manager,
            volume=self.volume,
            mock_mode=self.mock_mode,
            on_toast=self._show_toast,
            on_invalidate=lambda: self.renderer.invalidate(),
            on_resume=self.auto_pause.restore_volume_if_needed,
            is_request_current=self._is_play_request_current,
            on_play_committed=self._on_play_committed,
            on_play_failed=self._on_play_failed,
        )

        self.librespot_recovery = LibrespotRecoveryManager(
            api=self.api,
            has_network_fn=self._has_network_connection,
            on_before_restart=self._on_librespot_before_restart,
            on_after_restart=self._on_librespot_after_restart,
            on_toast=self._on_librespot_recovery_toast,
            mock_mode=self.mock_mode,
        )
        if not self.mock_mode and isinstance(self.api, LibrespotAPI):
            self.api._on_transport_failure = self.librespot_recovery.note_transport_failure

        self.checkpod_manager = CheckPodManager(
            on_toast=self._show_toast,
            on_invalidate=lambda: (
                self._update_carousel_max_index(),
                self.renderer.invalidate(),
            ),
            get_progress_expiry=lambda: self.settings.progress_expiry_hours,
        )
        self.checkpod_manager.cleanup_stale_downloads()
        self.local_music_manager = LocalMusicManager(
            on_toast=self._show_toast,
            on_invalidate=lambda: (
                self._update_carousel_max_index(),
                self.renderer.invalidate(),
            ),
            get_progress_expiry=lambda: self.settings.progress_expiry_hours,
        )
        self.local_playback = LocalPlaybackController(
            mock_mode=self.mock_mode,
            on_state_changed=self._on_local_playback_changed,
            on_stopped=self._on_local_playback_stopped,
            on_error=self._show_toast,
            get_speaker_level=lambda: self.volume.speaker_level,
        )
        self.voice_search_carousel = SmoothCarousel()
        self._voice_search_phase = VoiceSearchPhase.CLOSED
        self._voice_search_query: str = ''
        self._voice_search_results: List[SearchResult] = []
        self._voice_search_error: Optional[str] = None
        self._voice_search_generation: int = 0
        self._voice_search_selected_index: int = 0
        self._voice_search_countdown_end: float = 0.0
        self.voice_recorder = VoiceRecorderController(
            mock_mode=self.mock_mode,
            on_state_changed=lambda: self.renderer.invalidate(),
            on_error=self._show_toast,
            get_speaker_level=lambda: self.volume.speaker_level,
            on_before_record=self._prepare_for_voice_recording,
            should_restore_output=lambda: (
                self.setup_menu.state != MenuState.VOICE_TEST
                and self._voice_search_phase != VoiceSearchPhase.RECORDING
            ),
        )
        self.voice_search_recorder = VoiceRecorderController(
            mock_mode=self.mock_mode,
            output_path=VOICE_SEARCH_LAST_PATH,
            max_seconds=VOICE_SEARCH_MAX_SECONDS,
            mp3_bitrate=VOICE_SEARCH_MP3_BITRATE,
            auto_start_capture=False,
            on_capture_ready=self._on_voice_search_capture_ready,
            on_state_changed=lambda: self.renderer.invalidate(),
            on_error=self._on_voice_search_error,
            get_speaker_level=lambda: self.volume.speaker_level,
            on_before_record=self._prepare_for_voice_recording,
            should_restore_output=lambda: self._voice_search_phase == VoiceSearchPhase.CLOSED,
            on_recording_complete=self._start_voice_search_pipeline,
        )
        self._mokibot_phase = MokiBotPhase.IDLE
        self._mokibot_session_id: Optional[str] = None
        self._mokibot_reply_text: str = ''
        self._mokibot_play_name: Optional[str] = None
        self._mokibot_generation: int = 0
        self._mokibot_countdown_end: float = 0.0
        self._mokibot_tts_cancel = threading.Event()
        self.mokibot_recorder = VoiceRecorderController(
            mock_mode=self.mock_mode,
            output_path=MOKIBOT_RECORD_PATH,
            max_seconds=MOKIBOT_MAX_SECONDS,
            mp3_bitrate=VOICE_SEARCH_MP3_BITRATE,
            auto_start_capture=False,
            on_capture_ready=self._on_mokibot_capture_ready,
            on_state_changed=lambda: self.renderer.invalidate(),
            on_error=self._on_mokibot_error,
            get_speaker_level=lambda: self.volume.speaker_level,
            on_before_record=self._prepare_for_voice_recording,
            should_restore_output=lambda: self._mokibot_phase == MokiBotPhase.IDLE,
            on_recording_complete=self._start_mokibot_pipeline,
        )
        self._voice_play_cancel = threading.Event()
        self._voice_test_uri = 'voice:test:last'
        
        # State (with thread-safe now_playing and connected)
        self._now_playing = NowPlaying()
        self._now_playing_lock = threading.Lock()
        self._connected = self.mock_mode
        self._connected_lock = threading.Lock()
        self.selected_index = 0
        self._connection_fail_count = 0
        self._connection_grace_threshold = 3
        self._running = threading.Event()
        self._running.set()
        self._poll_wake_event = threading.Event()
        self._last_sleep_wait_log: float = 0.0
        
        # TempItem and delete mode (with lock for thread-safe access)
        self.temp_item: Optional[CatalogItem] = None
        self._temp_item_lock = threading.Lock()
        self.delete_mode_id: Optional[str] = None
        self._delete_button_rect: Optional[tuple] = None
        self._saving = False
        self._deleting = False
        
        # True while user is actively controlling playback (swipe/play).
        # While True, _sync_to_playing only accepts confirmation of our own play request.
        # While False, _sync_to_playing accepts anything (external Spotify control).
        self._user_driving = False
        self._user_driving_since: float = 0.0
        self._focus_epoch: int = 0
        self._pending_focus_uri: Optional[str] = None
        self._pending_focus_since: float = 0.0
        self._pending_external_focus_uri: Optional[str] = None
        self._last_focus_gate_log: float = 0.0
        self._requested_focus_epoch: Optional[int] = None
        self._requested_focus_uri: Optional[str] = None
        self._requested_focus_since: float = 0.0
        self._last_requested_hold_log: float = 0.0
        self._last_title_diag_log: float = 0.0
        self._last_status_ok_at: float = 0.0
        self._wake_at: float = 0.0
        # True when status is temporarily unknown (timeout/error). While unknown
        # we keep the last known now_playing snapshot and block auto-retrigger.
        self._status_unknown: bool = False
        self._last_status_unknown_log: float = 0.0
        self._last_status_not_ready_log: float = 0.0
        self._user_activated_playback: bool = False
        self._last_play_commit_uri: Optional[str] = None
        self._last_play_commit_at: float = 0.0
        self._last_snap_pause_at: float = 0.0
        self._snap_pause_generation: int = 0
        self._manual_play_suppress_uri: Optional[str] = None
        self._manual_play_suppress_until: float = 0.0
        self._last_restore_handled_at: float = 0.0
        self._restore_dedup_count: int = 0
        self._repeat_context_uri: Optional[str] = None
        self._repeat_context_last_attempt: float = 0.0
        # Blocks auto-play after an explicit user pause until user gives a
        # positive play intent (play tap or context switch).
        self._manual_pause_lock: bool = False
        self._manual_pause_context_uri: Optional[str] = None
        # Block focus auto-play after opening Spotify from home until explicit play.
        self._spotify_launch_lock: bool = False
        # Block focus auto-play after opening CheckPod from home until explicit play.
        self._checkpod_launch_lock: bool = False
        self._checkpod_play_in_progress: bool = False
        self._checkpod_load_more_scheduled: bool = False
        self._checkpod_pending_focus_uri: Optional[str] = None
        self._checkpod_pending_focus_since: float = 0.0
        self._last_checkpod_progress_save: float = 0.0
        self._checkpod_play_failed_uri: Optional[str] = None
        self._checkpod_play_failed_at: float = 0.0
        self._checkpod_play_target_uri: Optional[str] = None
        self._last_checkpod_context_uri: Optional[str] = None
        self._local_music_launch_lock: bool = False
        self._local_music_play_in_progress: bool = False
        self._local_music_pending_focus_uri: Optional[str] = None
        self._local_music_pending_focus_since: float = 0.0
        self._last_local_music_progress_save: float = 0.0
        self._local_music_play_failed_uri: Optional[str] = None
        self._local_music_play_failed_at: float = 0.0
        self._local_music_play_target_uri: Optional[str] = None
        self._last_local_music_context_uri: Optional[str] = None
        self._radio_launch_lock: bool = False
        self._radio_play_in_progress: bool = False
        self._autoplay_stall_since: float = 0.0
        self._last_autoplay_stall_log: float = 0.0
        self._context_switch_stall_since: float = 0.0
        self._last_context_watchdog_log: float = 0.0
        self._last_loader_mismatch_log: float = 0.0
        
        # Interaction tracking
        self.user_interacting = False
        self._last_cover_collect_key: Optional[tuple] = None
        self._cover_collect_context: Optional[str] = None
        self._context_change_time: float = 0
        
        # Button debouncing and feedback
        self._last_action_time = 0
        self._pressed_button: Optional[str] = None
        self._pressed_time = 0
        
        # Toast messages (brief on-screen feedback)
        self._toast_message: Optional[str] = None
        self._toast_time: float = 0
        self._toast_duration: float = 3.0

        # Music search (Settings → Musik suchen)
        self._search_query: str = ''
        self._search_results: List[SearchResult] = []
        self._search_loading: bool = False
        self._search_error: Optional[str] = None
        self._search_generation: int = 0

        # Voice transcription (Settings → Sprachtest)
        self._voice_transcript: Optional[str] = None
        self._voice_transcribing: bool = False
        self._voice_transcribe_error: Optional[str] = None
        self._voice_transcribe_generation: int = 0
        
        # Startup gate: blocks auto-play until _initial_connect completes
        self._startup_ready = False

        # Cached network status (avoid shelling out to nmcli every frame)
        self._cached_has_network: bool = True
        self._network_check_time: float = 0.0
        self._last_sleep_network_reconnect_at: float = 0.0
        self._librespot_recovery_suppressed_until: float = 0.0
        
        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        
        # Performance logging
        self._last_fps_log = time.time()
        self._fps_log_interval = 30  # Log FPS every 30 seconds
        
        # Bluetooth manager
        self.bluetooth = BluetoothManager(
            settings=self.settings,
            on_toast=self._show_toast,
            on_invalidate=lambda: self.renderer.invalidate(),
            on_audio_changed=self._on_bt_audio_changed,
        )
        self._bt_audio_active: bool = False

        # Setup menu
        self.setup_menu = SetupMenu(
            catalog_manager=self.catalog_manager,
            settings=self.settings,
            on_toast=self._show_toast,
            on_invalidate=lambda: self.renderer.invalidate(),
            on_library_cleared=self._on_library_cleared,
            bluetooth_manager=self.bluetooth,
            on_volume_preview=self._preview_volume,
            on_open_home=self._open_home_screen,
            on_prepare_shutdown=self._prepare_shutdown,
            on_enter_voice_test=self._on_enter_voice_test,
            on_leave_voice_test=self._on_leave_voice_test,
            on_voice_record_toggle=self._on_voice_record_toggle,
            on_voice_play=self._on_voice_play,
            on_voice_transcribe=self._on_voice_transcribe,
            on_enter_music_search=self._on_enter_music_search,
            on_music_search_key=self._on_music_search_key,
            on_music_search_submit=self._on_music_search_submit,
            on_music_search_result=self._on_music_search_result,
            on_suppress_librespot_recovery=self.suppress_librespot_recovery,
        )
        # Volume button hold tracking (3s hold opens setup menu)
        self._volume_hold_start: Optional[float] = None
        self._menu_hold_triggered = False
        # Menu scroll tracking
        self._menu_touch_start: Optional[tuple] = None
        self._menu_touch_scrolled: bool = False
        self._voice_search_touch_active: bool = False
        
        # Initialize carousel
        self._update_carousel_max_index()
    
    def _load_icons(self) -> dict:
        """Load icon images."""
        icons = {}
        icon_files = {
            'play': 'play-fill.png',
            'pause': 'pause-fill.png',
            'prev': 'skip-back-fill.png',
            'next': 'skip-forward-fill.png',
            'volume_none': 'speaker-none-fill.png',
            'volume_low': 'speaker-low-fill.png',
            'volume_high': 'speaker-high-fill.png',
            'plus': 'plus-circle-fill.png',
            'minus': 'minus-circle-fill.png',
            'headphone': 'headphone.png',
            'close': 'close.png',
            'back': 'back.png',
            'home': 'home.png',
            'reload': 'reload.png',
            'mic': 'mic.png',
            'logo': APP_LOGO,
        }
        for name, filename in icon_files.items():
            try:
                icon = pygame.image.load(ICONS_DIR / filename).convert_alpha()
                if name in ('home', 'mic'):
                    icon = pygame.transform.rotate(icon, -90)
                icons[name] = icon
            except Exception as e:
                logger.warning(f'Failed to load icon {filename}: {e}', exc_info=True)
        return icons

    def _load_home_icon(self, icon_path: Path) -> Optional[pygame.Surface]:
        icon = pygame.image.load(icon_path).convert_alpha()
        icon_scaled = pygame.transform.smoothscale(icon, (HOME_ICON_SIZE, HOME_ICON_SIZE))
        return pygame.transform.rotate(icon_scaled, -90)

    def _load_home_assets(self):
        """Load home background and launcher app icons."""
        bg_path = ASSETS_DIR / 'moki-background.png'
        app_defs = [
            ('musik', ASSETS_DIR / 'musik.png'),
            ('checker', ASSETS_DIR / 'Checkpod.png'),
            ('local_music', ASSETS_DIR / 'local-music.png'),
            ('radio', ASSETS_DIR / 'radio-teddy.png'),
            ('mokibot', ASSETS_DIR / 'mokibot.png'),
            ('settings', ASSETS_DIR / 'settings.png'),
        ]
        try:
            bg = pygame.image.load(bg_path).convert()
            bg_rot = pygame.transform.rotate(bg, -90)
            rot_w, rot_h = bg_rot.get_size()
            scale = min(SCREEN_WIDTH / rot_w, SCREEN_HEIGHT / rot_h)
            bg_scaled = pygame.transform.smoothscale(
                bg_rot,
                (max(1, int(rot_w * scale)), max(1, int(rot_h * scale))),
            )

            apps: List[HomeAppEntry] = []
            for app_id, path in app_defs:
                if not path.exists():
                    logger.warning(f'Home icon missing ({app_id}): {path}')
                    continue
                icon = self._load_home_icon(path)
                if icon:
                    apps.append(HomeAppEntry(app_id, icon))

            logger.info(f'Home assets loaded | apps={len(apps)} pages={home_page_count(len(apps))}')
            return bg_scaled, apps
        except Exception as e:
            logger.warning(f'Failed to load home assets: {e}', exc_info=True)
            return None, []

    def _sync_home_pager_limits(self):
        pages = home_page_count(len(self.renderer._home_apps))
        self.home_pager.max_index = max(0, pages - 1)

    def _reset_home_pager(self):
        self.home_pager.scroll_x = 0.0
        self.home_pager.set_target(0)
        self._home_touch_active = False
        self._home_touch.dragging = False
        self._home_touch.drag_offset = 0

    def _open_home_app(self, app_id: str):
        handlers = {
            'musik': self._open_spotify_screen,
            'checker': self._open_checkpod_screen,
            'local_music': self._open_local_music_screen,
            'radio': self._open_radio_screen,
            'mokibot': self._open_mokibot_screen,
            'settings': self._open_settings_with_pin,
        }
        handler = handlers.get(app_id)
        if handler:
            handler()

    def _home_icon_at_pos(self, pos) -> Optional[str]:
        if self.app_screen != AppScreen.HOME:
            return None
        if self._home_touch.dragging and self._home_touch.is_swiping:
            return None
        drag_offset = self._home_touch.drag_offset if self._home_touch_active else 0.0
        if self._home_touch_active and self._home_touch.dragging:
            page_scroll = self.home_pager.scroll_x - drag_offset / SCREEN_HEIGHT
        else:
            page_scroll = self.home_pager.scroll_x
        for index in visible_app_indices(len(self.renderer._home_apps), page_scroll):
            app = self.renderer._home_apps[index]
            center = app_center(index, page_scroll, drag_offset if self._home_touch_active else 0.0)
            if icon_hit_rect(center).collidepoint(pos):
                return app.app_id
        return None

    def _radio_catalog_item(self) -> CatalogItem:
        return CatalogItem(
            id='radio_teddy',
            uri=RADIO_TEDDY_CONTEXT_URI,
            name=RADIO_TEDDY_NAME,
            type='radio',
            artist='Live',
            image=RADIO_TEDDY_IMAGE_PATH,
        )

    def _display_items(self) -> List[CatalogItem]:
        if self.app_screen == AppScreen.CHECKPOD:
            return self.checkpod_manager.get_display_items()
        if self.app_screen == AppScreen.LOCAL_MUSIC:
            return self.local_music_manager.get_display_items()
        if self.app_screen == AppScreen.RADIO:
            return [self._radio_catalog_item()]
        return self.display_items

    def _uses_mpv_playback_ui(self) -> bool:
        return self.app_screen in (AppScreen.CHECKPOD, AppScreen.LOCAL_MUSIC, AppScreen.RADIO)

    def _is_local_media_screen(self) -> bool:
        screen = getattr(self, 'app_screen', None)
        return screen in (AppScreen.CHECKPOD, AppScreen.LOCAL_MUSIC)

    def _manager_for_context_uri(self, context_uri: str):
        if context_uri.startswith('local:music:'):
            return self.local_music_manager
        if context_uri.startswith('urn:ard:episode:'):
            return self.checkpod_manager
        return None

    def _local_media_manager(self):
        screen = getattr(self, 'app_screen', None)
        if screen == AppScreen.CHECKPOD:
            return self.checkpod_manager
        if screen == AppScreen.LOCAL_MUSIC:
            return self.local_music_manager
        return None

    def _reset_checkpod_screen_state(self):
        self._checkpod_launch_lock = False
        self._checkpod_play_in_progress = False
        self._checkpod_play_target_uri = None
        self._checkpod_pending_focus_uri = None
        self._checkpod_pending_focus_since = 0.0

    def _reset_local_music_screen_state(self):
        self._local_music_launch_lock = False
        self._local_music_play_in_progress = False
        self._local_music_play_target_uri = None
        self._local_music_pending_focus_uri = None
        self._local_music_pending_focus_since = 0.0

    def _reset_radio_screen_state(self):
        self._radio_launch_lock = False
        self._radio_play_in_progress = False

    def _reset_mokibot_screen_state(self):
        """No-op — MokiBot state is cleared via _cancel_mokibot_pipeline."""
        pass

    def _is_moki_local_playback_uri(self, context_uri: Optional[str]) -> bool:
        if not context_uri:
            return False
        return (
            context_uri.startswith('radio:')
            or context_uri.startswith('local:music:')
            or context_uri.startswith('urn:ard:episode:')
            or context_uri == MOKIBOT_TTS_CONTEXT_URI
        )

    def _clear_local_playback_now_playing(self):
        """Drop mpv mirror state so Spotify pause/mute is not triggered by mistake."""
        ctx = self.now_playing.context_uri or ''
        if self._is_moki_local_playback_uri(ctx):
            self.now_playing = NowPlaying(stopped=True)
            self.renderer.invalidate()

    def _pause_active_playback(self, reason: str):
        """Stop whatever is currently playing (Spotify or local media)."""
        if self.local_playback.is_active:
            self._save_local_media_progress_now(reason)
            self.local_playback.stop(save_progress=False)
            self._clear_local_playback_now_playing()
            return
        if (self.now_playing.playing or self.playback.play_state.should_show_loading
                or self.playback._play_in_progress):
            self.playback._execute_pause(reason)

    def _playback_blocks_sleep(self) -> bool:
        """True only when real playback should keep the display awake."""
        playing, _, _, _, _, _ = self.local_playback.get_state()
        if playing:
            return True

        if self.app_screen not in (AppScreen.SPOTIFY, AppScreen.MOKIBOT):
            return False

        np = self.now_playing
        return bool(np.playing and not np.paused and np.track_name)

    def _open_home_screen(self):
        """Switch to home screen and stop playback immediately."""
        self._close_voice_search()
        self._cancel_mokibot_pipeline(reset_session=True)
        if self.app_screen != AppScreen.HOME:
            self._set_manual_pause_lock('home_open')
        self._pause_active_playback('home_open')
        self.app_screen = AppScreen.HOME
        self._reset_checkpod_screen_state()
        self._reset_local_music_screen_state()
        self._reset_radio_screen_state()
        self._reset_home_pager()
        self._pressed_button = None
        self.renderer.invalidate()
        logger.info('Home screen opened')

    def _open_spotify_screen(self):
        """Return to the Spotify player carousel without auto-starting playback."""
        if getattr(self.local_playback, 'is_active', False):
            self._save_local_media_progress_now('spotify_open')
            self.local_playback.stop(save_progress=False)
            self._clear_local_playback_now_playing()
        self.app_screen = AppScreen.SPOTIFY
        self._spotify_launch_lock = True
        self._reset_checkpod_screen_state()
        self._reset_local_music_screen_state()
        self._reset_radio_screen_state()
        self._reset_pending_focus('spotify_open')
        self._update_carousel_max_index()
        self._pressed_button = None
        self.renderer.invalidate()
        logger.info('Spotify screen opened (launch lock active)')

    def _open_checkpod_screen(self):
        """Open CheckPod episode carousel without auto-starting playback."""
        self._pause_active_playback('checkpod_open')
        self._set_manual_pause_lock('checkpod_open')
        self.app_screen = AppScreen.CHECKPOD
        self._checkpod_launch_lock = True
        self._spotify_launch_lock = False
        self._reset_local_music_screen_state()
        self._reset_radio_screen_state()
        self._checkpod_play_in_progress = False
        self._checkpod_play_target_uri = None
        self._checkpod_pending_focus_uri = None
        self._checkpod_pending_focus_since = 0.0
        self._restore_local_media_carousel_focus(AppScreen.CHECKPOD)
        self._update_carousel_max_index()
        self._pressed_button = None
        self.renderer.invalidate()
        run_async(self._refresh_checkpod_episodes)
        self.local_playback.warm_up()
        logger.info('CheckPod screen opened (launch lock active)')

    def _open_local_music_screen(self):
        """Open local MP3 carousel without auto-starting playback."""
        self._pause_active_playback('local_music_open')
        self._set_manual_pause_lock('local_music_open')
        self.app_screen = AppScreen.LOCAL_MUSIC
        self._local_music_launch_lock = True
        self._spotify_launch_lock = False
        self._reset_checkpod_screen_state()
        self._reset_radio_screen_state()
        self._local_music_play_in_progress = False
        self._local_music_play_target_uri = None
        self._local_music_pending_focus_uri = None
        self._local_music_pending_focus_since = 0.0
        self._restore_local_media_carousel_focus(AppScreen.LOCAL_MUSIC)
        self._update_carousel_max_index()
        self._pressed_button = None
        self.renderer.invalidate()
        run_async(self._refresh_local_music_catalog)
        self.local_playback.warm_up()
        logger.info('Local music screen opened (launch lock active)')

    def _open_radio_screen(self):
        """Open Radio TEDDY live stream screen without auto-starting playback."""
        self._pause_active_playback('radio_open')
        self._set_manual_pause_lock('radio_open')
        self.app_screen = AppScreen.RADIO
        self._radio_launch_lock = True
        self._spotify_launch_lock = False
        self._reset_checkpod_screen_state()
        self._reset_local_music_screen_state()
        self._radio_play_in_progress = False
        self.selected_index = 0
        self.carousel.max_index = 0
        self.carousel.scroll_x = 0.0
        self.carousel.set_target(0)
        self._update_carousel_max_index()
        self._pressed_button = None
        self.renderer.invalidate()
        self.local_playback.warm_up()
        logger.info('Radio screen opened (launch lock active)')

    def _open_mokibot_screen(self):
        """Open MokiBot voice assistant screen."""
        self._cancel_mokibot_pipeline(reset_session=True)
        self._pause_active_playback('mokibot_open')
        self._set_manual_pause_lock('mokibot_open')
        self._close_voice_search()
        self.app_screen = AppScreen.MOKIBOT
        self._spotify_launch_lock = False
        self._reset_checkpod_screen_state()
        self._reset_local_music_screen_state()
        self._reset_radio_screen_state()
        self._mokibot_phase = MokiBotPhase.IDLE
        self._mokibot_reply_text = ''
        self._mokibot_play_name = None
        self._mokibot_countdown_end = 0.0
        self._pressed_button = None
        MOKIBOT_DIR.mkdir(parents=True, exist_ok=True)
        self.renderer.invalidate()
        self.local_playback.warm_up()
        logger.info('MokiBot screen opened')

    def _cancel_mokibot_pipeline(self, *, reset_session: bool = False):
        """Stop in-flight MokiBot recording, TTS, or API work."""
        self._mokibot_generation += 1
        self._mokibot_tts_cancel.set()
        self.mokibot_recorder.cancel()
        playing, paused, _, _, ctx, _ = self.local_playback.get_state()
        if ctx == MOKIBOT_TTS_CONTEXT_URI and (
            playing or paused or self.local_playback.is_active
        ):
            self.local_playback.stop(save_progress=False)
            self._clear_local_playback_now_playing()
        self._mokibot_phase = MokiBotPhase.IDLE
        self._mokibot_countdown_end = 0.0
        if reset_session:
            self._mokibot_session_id = None
            self._mokibot_reply_text = ''
            self._mokibot_play_name = None
        self.renderer.invalidate()

    def _mokibot_status_text(self) -> str:
        phase = self._mokibot_phase
        if phase == MokiBotPhase.IDLE:
            return 'Tippe das Mikrofon und sag mir, was du hören willst.'
        if phase == MokiBotPhase.PREPARING:
            return 'Mikrofon wird bereit…'
        if phase == MokiBotPhase.COUNTDOWN:
            return ''
        if phase == MokiBotPhase.RECORDING:
            return 'Ich höre zu…'
        if phase == MokiBotPhase.THINKING:
            return 'Einen Moment…'
        if phase == MokiBotPhase.SPEAKING:
            return ''
        if phase == MokiBotPhase.PLAYING and self._mokibot_play_name:
            return f'Ich spiele: {self._mokibot_play_name}'
        return ''

    def _mokibot_countdown_label(self) -> str:
        if self._mokibot_phase != MokiBotPhase.COUNTDOWN:
            return ''
        remaining = self._mokibot_countdown_end - time.time()
        if remaining > 3.0:
            return '3'
        if remaining > 2.0:
            return '2'
        if remaining > 1.0:
            return '1'
        if remaining > 0.0:
            return 'OK'
        return ''

    def _on_mokibot_error(self, message: str):
        logger.warning(f'MokiBot recorder error: {message}')
        if self._mokibot_phase != MokiBotPhase.IDLE:
            self._cancel_mokibot_pipeline()
            self._show_toast(message)

    def _on_mokibot_capture_ready(self):
        self._mokibot_countdown_end = time.time() + VOICE_SEARCH_COUNTDOWN_SECONDS
        self._mokibot_phase = MokiBotPhase.COUNTDOWN
        self.renderer.invalidate()

    def _tick_mokibot(self):
        if self._mokibot_phase != MokiBotPhase.COUNTDOWN:
            return
        if time.time() < self._mokibot_countdown_end:
            return
        if self.mokibot_recorder.start_capture():
            self._mokibot_phase = MokiBotPhase.RECORDING
            self.renderer.invalidate()
        else:
            self._cancel_mokibot_pipeline()
            self._show_toast('Mikrofon nicht bereit')

    def _on_mokibot_mic(self):
        if self._mokibot_phase in (
            MokiBotPhase.PREPARING,
            MokiBotPhase.COUNTDOWN,
            MokiBotPhase.THINKING,
            MokiBotPhase.SPEAKING,
        ):
            return
        if self._mokibot_phase in (MokiBotPhase.IDLE, MokiBotPhase.PLAYING):
            self._pause_active_playback('mokibot_record')
            self._mokibot_tts_cancel.set()
            self._mokibot_generation += 1
            self._mokibot_countdown_end = 0.0
            self._mokibot_phase = MokiBotPhase.PREPARING
            self._pressed_button = 'mokibot_mic'
            self._pressed_time = time.time()
            self.renderer.invalidate()
            self.mokibot_recorder.begin_recording()
        elif self._mokibot_phase == MokiBotPhase.RECORDING:
            if self.mokibot_recorder.is_recording:
                self._pressed_button = 'mokibot_mic'
                self._pressed_time = time.time()
                self.renderer.invalidate()
                self.mokibot_recorder.stop_recording()

    def _start_mokibot_pipeline(self):
        if self._mokibot_phase != MokiBotPhase.RECORDING:
            return
        self._mokibot_generation += 1
        generation = self._mokibot_generation
        self._mokibot_phase = MokiBotPhase.THINKING
        self.renderer.invalidate()
        logger.info('MokiBot pipeline started')

        def _run():
            try:
                if not moki_transcribe.wait_for_api(max_wait=VOICE_SEARCH_API_PROBE_MAX_WAIT):
                    if generation != self._mokibot_generation:
                        return
                    self._cancel_mokibot_pipeline()
                    self._show_toast('Keine Verbindung zum Server')
                    return

                text = moki_transcribe.transcribe(
                    MOKIBOT_RECORD_PATH,
                    timeout=(VOICE_SEARCH_API_CONNECT_TIMEOUT, VOICE_SEARCH_API_READ_TIMEOUT),
                )
                if generation != self._mokibot_generation:
                    return
                logger.info(f'MokiBot transcript: "{text[:80]}"')

                response = moki_assistant.assistant_request(
                    text,
                    session_id=self._mokibot_session_id,
                )
                if generation != self._mokibot_generation:
                    return
                if response.session_id:
                    self._mokibot_session_id = response.session_id
                self._handle_mokibot_response(response, generation)
            except requests.HTTPError as e:
                if generation != self._mokibot_generation:
                    return
                logger.warning(f'MokiBot assistant HTTP error: {e}')
                self._cancel_mokibot_pipeline()
                self._show_toast('Server-Fehler — bitte nochmal versuchen')
            except requests.RequestException as e:
                if generation != self._mokibot_generation:
                    return
                logger.warning(f'MokiBot network error: {e}')
                self._cancel_mokibot_pipeline()
                self._show_toast('Keine Verbindung — bitte nochmal versuchen')
            except (ValueError, FileNotFoundError, RuntimeError) as e:
                if generation != self._mokibot_generation:
                    return
                logger.warning(f'MokiBot pipeline error: {e}')
                self._cancel_mokibot_pipeline()
                self._show_toast('Das habe ich nicht verstanden')
            except Exception as e:
                if generation != self._mokibot_generation:
                    return
                logger.error(f'MokiBot pipeline failed: {e}', exc_info=True)
                self._cancel_mokibot_pipeline()
                self._show_toast('Etwas ist schiefgelaufen')

        run_async(_run)

    def _wait_mokibot_tts_done(self, generation: int, timeout: float = 30.0) -> bool:
        """Wait until TTS mpv playback finishes so Spotify can start."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if generation != self._mokibot_generation or self._mokibot_tts_cancel.is_set():
                return False
            playing, paused, position_ms, duration_ms, ctx, _ = self.local_playback.get_state()
            if ctx != MOKIBOT_TTS_CONTEXT_URI:
                return True
            if not playing and not paused:
                return True
            if duration_ms > 0 and position_ms >= max(0, duration_ms - 400):
                time.sleep(0.2)
                return True
            time.sleep(0.1)
        logger.warning('MokiBot TTS wait timed out — starting Spotify anyway')
        return True

    def _handle_mokibot_response(self, response: AssistantResponse, generation: int):
        self._mokibot_reply_text = response.reply_text or ''
        self._mokibot_phase = MokiBotPhase.SPEAKING
        self.renderer.invalidate()
        logger.info(f'MokiBot action={response.action} reply="{self._mokibot_reply_text[:60]}"')

        def _tts_then_continue():
            tts_path = MOKIBOT_TTS_PATH
            if response.reply_audio_url:
                try:
                    moki_assistant.download_tts(response.reply_audio_url, tts_path)
                except requests.RequestException as e:
                    logger.warning(f'MokiBot TTS download failed: {e}')
            if generation != self._mokibot_generation:
                return
            if tts_path.is_file() and tts_path.stat().st_size > 0:
                self._mokibot_tts_cancel.clear()
                ok = self.local_playback.play(
                    tts_path,
                    context_uri=MOKIBOT_TTS_CONTEXT_URI,
                    track_name='MokiBot',
                    start_position_ms=0,
                    duration_ms=0,
                )
                if ok:
                    if not self._wait_mokibot_tts_done(generation):
                        return
                elif generation == self._mokibot_generation:
                    unmute_wm8960_output(self.volume.speaker_level)
            if generation != self._mokibot_generation:
                return
            if response.action == 'play' and response.play:
                logger.info('MokiBot TTS done — enqueueing Spotify play')
                self._mokibot_play_result(response.play, generation)
            elif response.action == 'play' and not response.play:
                logger.warning('MokiBot action=play but no play payload from API')
                self._mokibot_phase = MokiBotPhase.IDLE
                self.renderer.invalidate()
            else:
                self._mokibot_phase = MokiBotPhase.IDLE
                self.renderer.invalidate()

        run_async(_tts_then_continue)

    def _mokibot_play_result(self, result: SearchResult, generation: int):
        if generation != self._mokibot_generation:
            return
        logger.info(
            f'MokiBot play: {result.name} ({result.type}) uri={result.uri[:40]}'
        )

        def _save_and_play():
            if generation != self._mokibot_generation:
                return
            item_data = {
                'type': result.type,
                'uri': result.uri,
                'name': result.name,
                'artist': result.artist,
                'image': result.image_url,
            }
            saved = self.catalog_manager.save_item(item_data)
            self.catalog_manager.load()
            self._update_carousel_max_index()
            self.image_cache.preload_catalog(self.catalog_manager.items)

            items = self.display_items
            target_index = next(
                (i for i, item in enumerate(items) if item.uri == result.uri),
                None,
            )
            if target_index is None:
                target_index = max(0, len(items) - 1) if items else 0
            if not items:
                logger.warning(f'MokiBot play: no catalog items after save uri={result.uri[:40]}')
                self._mokibot_phase = MokiBotPhase.IDLE
                self.renderer.invalidate()
                return

            if generation != self._mokibot_generation:
                return
            _, _, _, _, ctx, _ = self.local_playback.get_state()
            if ctx == MOKIBOT_TTS_CONTEXT_URI and self.local_playback.is_active:
                self.local_playback.stop(save_progress=False)
                self._clear_local_playback_now_playing()
            self.selected_index = target_index
            self.carousel.set_target(target_index)
            self._bump_focus_epoch(f'mokibot_play {target_index}')
            self._clear_manual_pause_lock('mokibot_play')
            self._clear_spotify_launch_lock('mokibot_play')
            self._user_activated_playback = True
            self._play_item(result.uri)
            self._mokibot_play_name = result.name
            self._mokibot_phase = MokiBotPhase.PLAYING
            if saved:
                self._show_toast('Gespeichert')
            self.renderer.invalidate()

        run_async(_save_and_play)

    def _handle_mokibot_touch_down(self, pos):
        if self.renderer.mokibot_home_rect and self.renderer.mokibot_home_rect.collidepoint(pos):
            self._pressed_button = 'mokibot_home'
            self._pressed_time = time.time()
            self.renderer.invalidate()
            return
        if (
            self._mokibot_phase in (MokiBotPhase.IDLE, MokiBotPhase.RECORDING, MokiBotPhase.PLAYING)
            and self.renderer.mokibot_mic_rect
            and self.renderer.mokibot_mic_rect.collidepoint(pos)
        ):
            self._on_mokibot_mic()

    def _handle_mokibot_touch_up(self, pos):
        if self._pressed_button == 'mokibot_home':
            rect = self.renderer.mokibot_home_rect
            self._pressed_button = None
            if rect and rect.collidepoint(pos):
                self._open_home_screen()
            else:
                self.renderer.invalidate()
            return
        if self._pressed_button == 'mokibot_mic':
            self._pressed_button = None
            self.renderer.invalidate()

    def _refresh_checkpod_episodes(self):
        """Refresh ARD catalog and prune stale downloads."""
        self.checkpod_manager.refresh_episodes()
        _, _, _, _, context_uri, _ = self.local_playback.get_state()
        self.checkpod_manager.cleanup_stale_downloads(active_context_uri=context_uri)
        if self.app_screen == AppScreen.CHECKPOD:
            self._update_carousel_max_index()
            self.renderer.invalidate()
            self._maybe_load_more_checkpod_episodes()

    def _maybe_load_more_checkpod_episodes(self):
        """Prefetch older CheckPod episodes when the user nears the list end."""
        if self.app_screen != AppScreen.CHECKPOD:
            return
        items = self.checkpod_manager.items
        if not items:
            return
        if not self.checkpod_manager.has_more_episodes:
            return
        if self._checkpod_load_more_scheduled or self.checkpod_manager.is_loading_more:
            return
        threshold = max(0, len(items) - CHECKPOD_LOAD_MORE_THRESHOLD)
        if self.selected_index < threshold:
            return
        self._checkpod_load_more_scheduled = True
        run_async(self._load_more_checkpod_episodes)

    def _load_more_checkpod_episodes(self):
        try:
            added = self.checkpod_manager.load_more_episodes()
            if not added:
                return
            if self.app_screen != AppScreen.CHECKPOD:
                logger.info(
                    f'CheckPod older episodes loaded in background | '
                    f'total={len(self.checkpod_manager.items)}'
                )
                return
            self._update_carousel_max_index()
            self.renderer.invalidate()
            logger.info(
                f'CheckPod older episodes loaded | total={len(self.checkpod_manager.items)} '
                f'| index={self.selected_index}'
            )
        finally:
            self._checkpod_load_more_scheduled = False

    def _refresh_local_music_catalog(self):
        """Rescan on-disk MP3 library."""
        self.local_music_manager.refresh_catalog()

    def _local_media_default_artist(self) -> str:
        if self.app_screen == AppScreen.LOCAL_MUSIC:
            items = self.local_music_manager.get_display_items()
            if items and self.selected_index < len(items):
                return items[self.selected_index].artist or 'Lokale Musik'
            return 'Lokale Musik'
        if self.app_screen == AppScreen.RADIO:
            return 'Live'
        return 'Checker Tobi'

    def _on_local_playback_changed(self):
        playing, paused, position_ms, duration_ms, context_uri, track_name = self.local_playback.get_state()
        if not self._uses_mpv_playback_ui():
            if not playing and not paused and self._is_moki_local_playback_uri(
                self.now_playing.context_uri
            ):
                self.now_playing = NowPlaying(stopped=True)
                self.renderer.invalidate()
            return
        if self.app_screen == AppScreen.RADIO:
            artist = 'Live' if playing else ''
        elif self.app_screen == AppScreen.LOCAL_MUSIC and context_uri:
            artist = self._local_media_default_artist()
            for item in self.local_music_manager.get_display_items():
                if item.uri == context_uri:
                    artist = item.artist or artist
                    break
        else:
            artist = self._local_media_default_artist()
        self.now_playing = NowPlaying(
            playing=playing,
            paused=paused,
            stopped=not playing and not paused,
            context_uri=context_uri,
            track_uri=context_uri,
            track_name=track_name,
            track_artist=artist,
            position=position_ms,
            duration=duration_ms,
        )
        self.renderer.invalidate()

    def _on_local_playback_stopped(self, context_uri: str, position_ms: int, duration_ms: int, name: str):
        manager = self._manager_for_context_uri(context_uri)
        if not manager:
            return
        if context_uri.startswith('local:music:'):
            self._last_local_music_context_uri = context_uri
        elif context_uri.startswith('urn:ard:episode:'):
            self._last_checkpod_context_uri = context_uri
        manager.save_progress(context_uri, position_ms, duration_ms, name, force=True)

    def _save_local_media_progress_now(self, reason: str = ''):
        """Persist local media position immediately (before stop or screen change)."""
        playing, paused, position_ms, duration_ms, context_uri, track_name = self.local_playback.get_state()
        if not context_uri or not (playing or paused):
            return
        manager = self._manager_for_context_uri(context_uri)
        if not manager:
            return
        live_position = self.local_playback.get_live_position_ms()
        if live_position is not None:
            position_ms = live_position
        if position_ms <= 0:
            return
        saved = manager.save_progress(
            context_uri, position_ms, duration_ms, track_name, force=True
        )
        if not saved:
            return
        if context_uri.startswith('local:music:'):
            self._last_local_music_context_uri = context_uri
            self._last_local_music_progress_save = time.time()
            label = 'Local music'
        else:
            self._last_checkpod_context_uri = context_uri
            self._last_checkpod_progress_save = time.time()
            label = 'CheckPod'
        if reason:
            logger.info(
                f'{label} progress flush ({reason}) | '
                f'{track_name} @ {position_ms // 1000}s'
            )

    def _save_checkpod_progress_now(self, reason: str = ''):
        """Backward-compatible alias for tests and existing call sites."""
        self._save_local_media_progress_now(reason)

    def _restore_local_media_carousel_focus(self, screen: AppScreen):
        if screen == AppScreen.CHECKPOD:
            manager = self.checkpod_manager
            fallback = getattr(self, '_last_checkpod_context_uri', None)
            label = 'CheckPod'
        elif screen == AppScreen.LOCAL_MUSIC:
            manager = self.local_music_manager
            fallback = getattr(self, '_last_local_music_context_uri', None)
            label = 'Local music'
        else:
            return

        items = manager.get_display_items()
        if not items:
            self.selected_index = 0
            self.carousel.scroll_x = 0.0
            self.carousel.set_target(0)
            return

        target_uri = manager.get_last_played_uri(fallback_uri=fallback)
        index = 0
        if target_uri:
            for i, item in enumerate(items):
                if item.uri == target_uri:
                    index = i
                    break

        self.selected_index = index
        self.carousel.set_target(index)
        if index > 0:
            logger.info(f'{label} restored focus: {items[index].name} (index {index})')

    def _restore_checkpod_carousel_focus(self):
        self._restore_local_media_carousel_focus(AppScreen.CHECKPOD)

    def _clear_local_media_launch_lock(self, reason: str):
        screen = getattr(self, 'app_screen', None)
        if screen == AppScreen.CHECKPOD:
            if self._checkpod_launch_lock:
                logger.info(f'CheckPod launch lock cleared ({reason})')
            self._checkpod_launch_lock = False
        elif screen == AppScreen.LOCAL_MUSIC:
            if self._local_music_launch_lock:
                logger.info(f'Local music launch lock cleared ({reason})')
            self._local_music_launch_lock = False
        elif screen == AppScreen.RADIO:
            if self._radio_launch_lock:
                logger.info(f'Radio launch lock cleared ({reason})')
            self._radio_launch_lock = False

    def _clear_radio_launch_lock(self, reason: str):
        if self._radio_launch_lock:
            logger.info(f'Radio launch lock cleared ({reason})')
        self._radio_launch_lock = False

    def _clear_checkpod_launch_lock(self, reason: str):
        self._clear_local_media_launch_lock(reason)

    def _play_checkpod_item(self, item: CatalogItem, from_beginning: bool = False):
        episode_id = self.checkpod_manager.get_episode_id_for_uri(item.uri)
        if not episode_id:
            self._show_toast('Folge nicht gefunden')
            return

        if self._checkpod_play_in_progress and self._checkpod_play_target_uri == item.uri:
            return
        if not from_beginning and self._is_local_media_item_playing(item):
            return

        self._checkpod_play_target_uri = item.uri

        def _do_play():
            self._checkpod_play_in_progress = True
            self.renderer.invalidate()
            audio_url = self.checkpod_manager.get_audio_url(episode_id)
            path = self.checkpod_manager.ensure_cached(episode_id, audio_url)
            if not path:
                self._checkpod_play_in_progress = False
                self.renderer.invalidate()
                return

            progress = None if from_beginning else self.checkpod_manager.get_progress(item.uri)
            start_ms = 0
            duration_ms = 0
            if progress:
                start_ms = int(progress.get('position') or 0)
                duration_ms = int(progress.get('duration') or 0)

            ok = self.local_playback.play(
                path,
                context_uri=item.uri,
                track_name=item.name,
                start_position_ms=start_ms,
                duration_ms=duration_ms,
            )
            self._checkpod_play_in_progress = False
            if self._checkpod_play_target_uri == item.uri:
                self._checkpod_play_target_uri = None
            if ok:
                self.volume.unmute()
                self._checkpod_play_failed_uri = None
                self._checkpod_play_failed_at = 0.0
                self._clear_checkpod_launch_lock('play_started')
            else:
                self._checkpod_play_failed_uri = item.uri
                self._checkpod_play_failed_at = time.time()
            self.renderer.invalidate()

        run_async(_do_play)

    def _play_local_music_item(self, item: CatalogItem, from_beginning: bool = False):
        if self._local_music_play_in_progress and self._local_music_play_target_uri == item.uri:
            return
        if not from_beginning and self._is_local_media_item_playing(item):
            return

        self._local_music_play_target_uri = item.uri

        def _do_play():
            self._local_music_play_in_progress = True
            self.renderer.invalidate()
            try:
                path = self.local_music_manager.get_media_path(item)
                if not path:
                    self._show_toast('Datei nicht gefunden')
                    return

                progress = None if from_beginning else self.local_music_manager.get_progress(item.uri)
                start_ms = 0
                duration_ms = 0
                if progress:
                    start_ms = int(progress.get('position') or 0)
                    duration_ms = int(progress.get('duration') or 0)

                ok = self.local_playback.play(
                    path,
                    context_uri=item.uri,
                    track_name=item.name,
                    start_position_ms=start_ms,
                    duration_ms=duration_ms,
                )
                if self._local_music_play_target_uri == item.uri:
                    self._local_music_play_target_uri = None
                if ok:
                    self.volume.unmute()
                    self._local_music_play_failed_uri = None
                    self._local_music_play_failed_at = 0.0
                    self._clear_local_media_launch_lock('play_started')
                else:
                    self._local_music_play_failed_uri = item.uri
                    self._local_music_play_failed_at = time.time()
            finally:
                self._local_music_play_in_progress = False
                self.renderer.invalidate()

        run_async(_do_play)

    def _play_radio_stream(self):
        if self._radio_play_in_progress:
            return
        item = self._radio_catalog_item()
        if self._is_local_media_item_playing(item):
            return

        def _do_play():
            self._radio_play_in_progress = True
            self.renderer.invalidate()
            ok = self.local_playback.play_stream(
                RADIO_TEDDY_STREAM_URL,
                context_uri=item.uri,
                track_name=item.name,
            )
            self._radio_play_in_progress = False
            if ok:
                self.volume.unmute()
                self._clear_radio_launch_lock('play_started')
            self.renderer.invalidate()

        run_async(_do_play)

    def _play_local_media_item(self, item: CatalogItem, from_beginning: bool = False):
        if self.app_screen == AppScreen.CHECKPOD:
            self._play_checkpod_item(item, from_beginning=from_beginning)
        elif self.app_screen == AppScreen.LOCAL_MUSIC:
            self._play_local_music_item(item, from_beginning=from_beginning)

    def _prepare_shutdown(self):
        """Save playback progress and flush analytics before poweroff."""
        logger.info('Preparing for shutdown')
        if self.voice_recorder.is_recording or self.voice_recorder.is_preparing:
            self.voice_recorder.cancel()
        if self.local_playback.is_active:
            self.local_playback.stop()
        if self.now_playing.playing or self.playback.play_state.should_show_loading:
            self.playback._execute_pause('shutdown')
        self._save_progress_on_shutdown()
        self.tracker.on_shutdown()

    def _on_enter_voice_test(self):
        """Open Sprachtest — stop playback and keep speaker muted until play."""
        logger.info('Voice test screen opened')
        self._pause_active_playback('voice_test_open')
        configure_wm8960_mic_once()
        mute_wm8960_output()

    def _on_leave_voice_test(self):
        """Leave Sprachtest — stop capture/playback and restore speaker."""
        logger.info('Voice test screen closed')
        self._voice_play_cancel.set()
        if self.voice_recorder.is_recording or self.voice_recorder.is_preparing:
            self.voice_recorder.cancel()
        if self._is_voice_test_playing():
            self.local_playback.stop(save_progress=False)
        self._reset_voice_transcribe_state()
        unmute_wm8960_output(self.volume.speaker_level)
        self.renderer.invalidate()

    def _prepare_for_voice_recording(self):
        """Free the audio path before WM8960 capture."""
        self.local_playback.silence_for_capture()
        if self.now_playing.playing or self.playback.has_pending_play:
            run_async(self.api.pause)

    def _prepare_voice_test_playback(self) -> int:
        boosted = min(100, self.volume.speaker_level + VOICE_TEST_PLAYBACK_SPEAKER_BOOST)
        unmute_wm8960_output(boosted)
        return boosted

    def _on_voice_record_toggle(self):
        if not self.voice_recorder.is_recording:
            self._reset_voice_transcribe_state()
        self.voice_recorder.toggle_recording()

    def _reset_voice_transcribe_state(self):
        self._voice_transcript = None
        self._voice_transcribing = False
        self._voice_transcribe_error = None
        self._voice_transcribe_generation += 1

    def _on_voice_transcribe(self):
        if not self.voice_recorder.has_recording():
            self._show_toast('Noch keine Aufnahme')
            return
        if (
            self.voice_recorder.is_recording
            or self.voice_recorder.is_encoding
            or self.voice_recorder.is_preparing
            or self._voice_transcribing
        ):
            return
        if not self._get_cached_network_status():
            self._show_toast('Kein Internet')
            return

        self._voice_transcribe_generation += 1
        generation = self._voice_transcribe_generation
        self._voice_transcribing = True
        self._voice_transcribe_error = None
        self._voice_transcript = None
        self.renderer.invalidate()
        logger.info('Voice transcribe started')

        def _do_transcribe():
            try:
                text = moki_transcribe.transcribe(VOICE_TEST_LAST_PATH)
                if generation != self._voice_transcribe_generation:
                    return
                self._voice_transcript = text
                self._voice_transcribe_error = None
                logger.info(f'Voice transcribe result: {text[:80]!r}')
            except Exception as e:
                logger.error(f'Voice transcribe failed: {e}', exc_info=True)
                if generation != self._voice_transcribe_generation:
                    return
                self._voice_transcript = None
                self._voice_transcribe_error = 'Transkription fehlgeschlagen'
            finally:
                if generation == self._voice_transcribe_generation:
                    self._voice_transcribing = False
                self.renderer.invalidate()

        run_async(_do_transcribe)

    def _on_voice_play(self):
        if not self.voice_recorder.has_recording():
            self._show_toast('Noch keine Aufnahme')
            return
        if (
            self.voice_recorder.is_recording
            or self.voice_recorder.is_encoding
            or self.voice_recorder.is_preparing
        ):
            return
        self._voice_play_cancel.clear()
        self._prepare_voice_test_playback()

        def _play():
            ok = self.local_playback.play(
                VOICE_TEST_LAST_PATH,
                context_uri=self._voice_test_uri,
                track_name='Sprachtest',
                start_position_ms=0,
                duration_ms=0,
            )
            if not ok and not self._voice_play_cancel.is_set():
                self._show_toast('Wiedergabe fehlgeschlagen')
                unmute_wm8960_output(self.volume.speaker_level)
            self.renderer.invalidate()

        run_async(_play)

    def _is_voice_test_playing(self) -> bool:
        playing, _, _, _, context_uri, _ = self.local_playback.get_state()
        return bool(playing and context_uri == self._voice_test_uri)

    def _on_enter_music_search(self):
        """Open music search — pause playback while parent adds content."""
        logger.info('Music search screen opened')
        self._pause_active_playback('music_search_open')
        self._search_generation += 1
        self._search_query = ''
        self._search_results = []
        self._search_loading = False
        self._search_error = None

    def _on_music_search_key(self, key: str):
        if key == 'space':
            if len(self._search_query) < 80:
                self._search_query += ' '
        elif key == 'back':
            self._search_query = self._search_query[:-1]
        elif key and len(self._search_query) < 80:
            self._search_query += key

    def _on_music_search_submit(self):
        query = self._search_query.strip()
        if not query:
            self._show_toast('Bitte Suchbegriff eingeben')
            return

        self._search_generation += 1
        generation = self._search_generation
        self._search_loading = True
        self._search_error = None
        self._search_results = []
        self.setup_menu.state = MenuState.MUSIC_SEARCH_RESULTS
        self.setup_menu.scroll_offset = 0
        self.renderer.invalidate()
        logger.info(f'Music search submit: q="{query}"')

        def _do_search():
            try:
                results = moki_search.search(query)
                if generation != self._search_generation:
                    return
                self._search_results = results
                self._search_loading = False
                self._search_error = None
                logger.info(f'Music search done: {len(results)} results')
            except Exception as e:
                logger.error(f'Music search failed: {e}', exc_info=True)
                if generation != self._search_generation:
                    return
                self._search_results = []
                self._search_loading = False
                self._search_error = 'Suche fehlgeschlagen'
            self.renderer.invalidate()

        run_async(_do_search)

    def _on_music_search_result(self, index: int):
        if index < 0 or index >= len(self._search_results):
            return
        result = self._search_results[index]
        self._pressed_button = f'search_result_{index}'
        self._pressed_time = time.time()
        self.renderer.invalidate()
        self._select_search_result(result, from_settings=True)

    def _on_voice_search_error(self, message: str):
        """Reset voice search UI after recorder failure."""
        self._show_toast(message)
        if self._voice_search_phase != VoiceSearchPhase.CLOSED:
            self._voice_search_generation += 1
            self._voice_search_phase = VoiceSearchPhase.CLOSED
            self.renderer.invalidate()
            logger.warning(f'Voice search reset after error: {message}')

    def _close_voice_search(self):
        """Close voice search overlay and reset state."""
        self._voice_search_generation += 1
        self.voice_search_recorder.cancel()
        self._voice_search_phase = VoiceSearchPhase.CLOSED
        self._voice_search_countdown_end = 0.0
        self._voice_search_query = ''
        self._voice_search_results = []
        self._voice_search_error = None
        self._voice_search_selected_index = 0
        self.voice_search_carousel.scroll_x = 0.0
        self.voice_search_carousel.set_target(0)
        self.renderer.invalidate()
        logger.info('Voice search closed')

    def _voice_search_countdown_label(self) -> str:
        """Map remaining countdown time to 3/2/1/OK step labels."""
        if self._voice_search_phase != VoiceSearchPhase.COUNTDOWN:
            return ''
        remaining = self._voice_search_countdown_end - time.time()
        if remaining > 3:
            return '3'
        if remaining > 2:
            return '2'
        if remaining > 1:
            return '1'
        if remaining > 0:
            return 'OK'
        return ''

    def _on_voice_search_capture_ready(self):
        """Mic hardware ready — start visible countdown before capture."""
        self._voice_search_countdown_end = time.time() + VOICE_SEARCH_COUNTDOWN_SECONDS
        self._voice_search_phase = VoiceSearchPhase.COUNTDOWN
        self.renderer.invalidate()
        logger.info('Voice search mic ready, countdown starting')

    def _tick_voice_search(self):
        """Advance countdown and start arecord when it hits zero."""
        if self._voice_search_phase != VoiceSearchPhase.COUNTDOWN:
            return
        if time.time() < self._voice_search_countdown_end:
            return
        if self.voice_search_recorder.start_capture():
            self._voice_search_phase = VoiceSearchPhase.RECORDING
            self.renderer.invalidate()
            logger.info('Voice search countdown done — speak now')
        else:
            self._close_voice_search()

    def _on_voice_search_mic(self):
        """Start or stop voice search recording."""
        if self._voice_search_phase == VoiceSearchPhase.CLOSED:
            self._pause_active_playback('voice_search_open')
            configure_wm8960_mic_once()
            mute_wm8960_output()
            self._voice_search_generation += 1
            self._voice_search_query = ''
            self._voice_search_results = []
            self._voice_search_error = None
            self._voice_search_countdown_end = 0.0
            self._voice_search_phase = VoiceSearchPhase.PREPARING
            self._pressed_button = 'mic'
            self._pressed_time = time.time()
            self.renderer.invalidate()
            logger.info('Voice search preparing')
            self.voice_search_recorder.begin_recording()
        elif self._voice_search_phase == VoiceSearchPhase.RECORDING:
            if self.voice_search_recorder.is_recording:
                self._pressed_button = 'mic'
                self._pressed_time = time.time()
                self.renderer.invalidate()
                logger.info('Voice search recording stopping')
                self.voice_search_recorder.stop_recording()

    def _show_voice_search_results(self, results: List[SearchResult], generation: int):
        """Show search hits immediately — covers may still be loading."""
        if generation != self._voice_search_generation:
            return
        self._voice_search_results = results
        self._voice_search_selected_index = 0
        self.voice_search_carousel.max_index = max(0, len(results) - 1)
        self.voice_search_carousel.scroll_x = 0.0
        self.voice_search_carousel.set_target(0)
        self._voice_search_phase = VoiceSearchPhase.RESULTS
        self.renderer.invalidate()
        logger.info(f'Voice search results: {len(results)} items')

    def _prefetch_voice_search_covers(self, results: List[SearchResult], generation: int):
        """Load cover images one-by-one after results are visible."""
        def _run():
            try:
                def _on_update(updated: List[SearchResult]):
                    if generation != self._voice_search_generation:
                        return
                    self._voice_search_results = updated
                    self.renderer.invalidate()

                prefetch_covers_incremental(results, on_update=_on_update)
                if generation == self._voice_search_generation:
                    logger.info('Voice search covers ready')
            except Exception as e:
                logger.warning(f'Voice search cover prefetch failed: {e}', exc_info=True)
            finally:
                if generation == self._voice_search_generation:
                    self.renderer.invalidate()

        run_async(_run)

    def _start_voice_search_pipeline(self):
        """Transcribe recording, search, show results, prefetch covers in background."""
        if self._voice_search_phase != VoiceSearchPhase.RECORDING:
            return

        self._voice_search_generation += 1
        generation = self._voice_search_generation
        self._voice_search_phase = VoiceSearchPhase.TRANSCRIBING
        self.renderer.invalidate()
        logger.info('Voice search pipeline started')
        pipeline_started = time.time()

        def _run():
            try:
                if not self._get_cached_network_status():
                    if generation != self._voice_search_generation:
                        return
                    self._close_voice_search()
                    self._show_toast('Kein Internet')
                    return

                if not moki_transcribe.wait_for_api(max_wait=VOICE_SEARCH_API_PROBE_MAX_WAIT):
                    if generation != self._voice_search_generation:
                        return
                    self._close_voice_search()
                    self._show_toast('Internet noch nicht bereit — nochmal versuchen')
                    return
                logger.info(
                    f'Voice search API ready in {time.time() - pipeline_started:.1f}s'
                )

                transcribe_started = time.time()
                text = moki_transcribe.transcribe(
                    VOICE_SEARCH_LAST_PATH,
                    timeout=VOICE_SEARCH_API_READ_TIMEOUT,
                )
                if generation != self._voice_search_generation:
                    return
                self._voice_search_query = text
                self._voice_search_phase = VoiceSearchPhase.SEARCHING
                self.renderer.invalidate()
                logger.info(
                    f'Voice search query: {text[:80]!r} '
                    f'(transcribe {time.time() - transcribe_started:.1f}s)'
                )

                search_started = time.time()
                results = moki_search.search(text)
                if generation != self._voice_search_generation:
                    return
                results = results[:VOICE_SEARCH_RESULT_LIMIT]

                if not results:
                    self._close_voice_search()
                    self._show_toast('Keine Treffer')
                    return

                self._show_voice_search_results(results, generation)
                logger.info(
                    f'Voice search UI ready in {time.time() - pipeline_started:.1f}s '
                    f'(search {time.time() - search_started:.1f}s, covers loading)'
                )
                self._prefetch_voice_search_covers(results, generation)
            except requests.RequestException as e:
                logger.error(f'Voice search network error: {e}', exc_info=True)
                if generation != self._voice_search_generation:
                    return
                self._close_voice_search()
                if isinstance(e, requests.Timeout):
                    self._show_toast('Dauert zu lange — nochmal versuchen')
                else:
                    self._show_toast('Kein Internet')
            except ValueError as e:
                logger.error(f'Voice search transcribe empty/invalid: {e}', exc_info=True)
                if generation != self._voice_search_generation:
                    return
                self._close_voice_search()
                self._show_toast('Nicht verstanden — nochmal sprechen')
            except requests.HTTPError as e:
                logger.error(f'Voice search HTTP error: {e}', exc_info=True)
                if generation != self._voice_search_generation:
                    return
                self._close_voice_search()
                if e.response is not None and e.response.status_code == 503:
                    self._show_toast('Gerade nicht verfügbar — nochmal versuchen')
                else:
                    self._show_toast('Suche fehlgeschlagen')
            except Exception as e:
                logger.error(f'Voice search pipeline failed: {e}', exc_info=True)
                if generation != self._voice_search_generation:
                    return
                self._close_voice_search()
                self._show_toast('Sprachsuche fehlgeschlagen')
            finally:
                if generation == self._voice_search_generation:
                    self.renderer.invalidate()

        run_async(_run)

    def _select_search_result(self, result: SearchResult, *, from_settings: bool = False):
        """Save search result to catalog and play."""
        logger.info(
            f'Search selected: {result.name} ({result.type}) uri={result.uri[:40]}'
        )

        def _save_and_play():
            item_data = {
                'type': result.type,
                'uri': result.uri,
                'name': result.name,
                'artist': result.artist,
                'image': result.image_url,
            }
            saved = self.catalog_manager.save_item(item_data)
            self.catalog_manager.load()
            self._update_carousel_max_index()
            self.image_cache.preload_catalog(self.catalog_manager.items)
            if from_settings:
                self.setup_menu.close()
                self._search_query = ''
                self._search_results = []
                self._search_loading = False
                self._search_error = None
            else:
                self._close_voice_search()
            self._open_spotify_screen()
            self._focus_catalog_uri_and_play(result.uri)
            if saved:
                self._show_toast('Gespeichert')
            else:
                self._show_toast('Schon in deiner Liste')
            self.renderer.invalidate()

        run_async(_save_and_play)

    def _voice_search_catalog_items(self) -> List[CatalogItem]:
        """Convert voice search results to temporary catalog items for the carousel."""
        items = []
        for i, result in enumerate(self._voice_search_results):
            items.append(
                CatalogItem(
                    id=f'voice_search:{i}',
                    uri=result.uri,
                    name=result.name,
                    type=result.type,
                    artist=result.artist,
                    image=result.preview_image,
                )
            )
        return items

    def _snap_to_voice_search(self, target_index: int):
        """Snap voice search results carousel to index."""
        items = self._voice_search_results
        if not items:
            return
        target_index = max(0, min(target_index, len(items) - 1))
        if target_index != self._voice_search_selected_index:
            self._voice_search_selected_index = target_index
            self.voice_search_carousel.set_target(target_index)
            self.renderer.invalidate()

    def _navigate_voice_search(self, direction: int):
        """Move voice search carousel by one item."""
        items = self._voice_search_results
        if not items:
            return
        new_index = max(0, min(self._voice_search_selected_index + direction, len(items) - 1))
        self._snap_to_voice_search(new_index)

    def _handle_voice_search_touch_down(self, pos):
        """Touch down while voice search overlay is open."""
        if self._voice_search_phase == VoiceSearchPhase.RESULTS:
            if self.renderer.voice_search_close_rect and self.renderer.voice_search_close_rect.collidepoint(pos):
                self._pressed_button = 'voice_search_close'
                self._pressed_time = time.time()
                self.renderer.invalidate()
                return
            carousel_x_min = CAROUSEL_X - CAROUSEL_TOUCH_MARGIN
            carousel_x_max = CAROUSEL_X + COVER_SIZE + CAROUSEL_TOUCH_MARGIN
            x, _y = pos
            if carousel_x_min <= x <= carousel_x_max:
                self._voice_search_touch_active = True
                self.touch.on_down(pos)
            return

        if self.renderer.voice_search_close_rect and self.renderer.voice_search_close_rect.collidepoint(pos):
            self._pressed_button = 'voice_search_close'
            self._pressed_time = time.time()
            self.renderer.invalidate()
            return

        if (
            self._voice_search_phase == VoiceSearchPhase.RECORDING
            and self.renderer.voice_search_mic_rect
            and self.renderer.voice_search_mic_rect.collidepoint(pos)
        ):
            self._on_voice_search_mic()

    def _handle_voice_search_touch_up(self, pos):
        """Touch up while voice search results carousel is active."""
        if self._voice_search_phase != VoiceSearchPhase.RESULTS:
            if self._pressed_button == 'voice_search_close':
                close_rect = self.renderer.voice_search_close_rect
                self._pressed_button = None
                self.renderer.invalidate()
                if close_rect and close_rect.collidepoint(pos):
                    self._close_voice_search()
            return
        if self._pressed_button == 'voice_search_close':
            close_rect = self.renderer.voice_search_close_rect
            self._pressed_button = None
            self.renderer.invalidate()
            if close_rect and close_rect.collidepoint(pos):
                self._close_voice_search()
            return
        if not getattr(self, '_voice_search_touch_active', False):
            return
        self._voice_search_touch_active = False
        if not self.touch.dragging:
            return

        drag_index_offset = -self.touch.drag_offset / (COVER_SIZE + COVER_SPACING)
        visual_position = self._voice_search_selected_index + drag_index_offset
        action, velocity = self.touch.on_up(pos)
        self.voice_search_carousel.scroll_x = visual_position

        _x, y = pos
        if action in ('left', 'right'):
            abs_vel = abs(velocity)
            v_low, v_mid, v_high = VELOCITY_THRESHOLDS
            velocity_bonus = 0 if abs_vel < v_low else (1 if abs_vel < v_mid else (2 if abs_vel < v_high else 3))
            base_target = round(visual_position)
            target = base_target + velocity_bonus if velocity < 0 else base_target - velocity_bonus
            target = max(
                self._voice_search_selected_index - MAX_SWIPE_JUMP,
                min(target, self._voice_search_selected_index + MAX_SWIPE_JUMP),
            )
            target = max(0, min(target, len(self._voice_search_results) - 1))
            self._snap_to_voice_search(target)
        elif action == 'tap':
            now = time.time()
            if now - self._last_action_time < ACTION_DEBOUNCE:
                return
            center_y = CAROUSEL_CENTER_Y
            if y < center_y - COVER_SIZE // 2:
                self._navigate_voice_search(-1)
            elif y > center_y + COVER_SIZE // 2:
                self._navigate_voice_search(1)
            else:
                idx = self._voice_search_selected_index
                if 0 <= idx < len(self._voice_search_results):
                    self._last_action_time = now
                    self._select_search_result(self._voice_search_results[idx])

    def _focus_catalog_uri_and_play(self, uri: str):
        """Focus carousel on uri and start playback."""
        items = self.display_items
        target_index = next((i for i, item in enumerate(items) if item.uri == uri), None)
        if target_index is None:
            target_index = max(0, len(items) - 1) if items else 0
        if not items:
            logger.warning(f'Music search: no catalog items after save uri={uri[:40]}')
            return

        old_index = self.selected_index
        self.selected_index = target_index
        self.carousel.set_target(target_index)
        self._bump_focus_epoch(f'music_search {old_index}->{target_index}')
        self._clear_manual_pause_lock('music_search_play')
        self._clear_spotify_launch_lock('music_search_play')
        self._user_activated_playback = True
        self._play_item(uri)
        self.renderer.invalidate()
    
    def _show_toast(self, message: str):
        """Show a brief toast message on screen."""
        self._toast_message = message
        self._toast_time = time.time()
        self.renderer.invalidate()

    def _bump_focus_epoch(self, reason: str):
        """Increment focus epoch so stale play responses can be ignored."""
        self._focus_epoch += 1
        self._requested_focus_epoch = None
        self._requested_focus_uri = None
        self._requested_focus_since = 0.0
        logger.info(f'Focus epoch -> {self._focus_epoch} ({reason})')

    def _current_focused_uri(self) -> Optional[str]:
        """Return currently focused URI, or None."""
        items = self._display_items()
        if not items or self.selected_index >= len(items):
            return None
        return items[self.selected_index].uri

    def _is_play_request_current(self, epoch: int, uri: str) -> bool:
        """True when play response still matches latest focus intent."""
        return epoch == self._focus_epoch and uri == self._current_focused_uri()

    def _has_active_user_focus_intent(self) -> bool:
        """True while user intent should block remote context focus sync."""
        requested_focus_active = (
            self._requested_focus_epoch == self._focus_epoch and
            self._requested_focus_uri is not None
        )
        return (
            self.touch.dragging
            or self._user_driving
            or self.play_timer.item is not None
            or requested_focus_active
        )

    def _should_prioritize_remote_focus(self, focused_item: Optional[CatalogItem]) -> bool:
        """True when playing context should win over focused auto-play request."""
        if not focused_item:
            return False
        if not self.now_playing.playing:
            return False
        playing_ctx = self.now_playing.context_uri
        if not playing_ctx:
            return False
        if playing_ctx == focused_item.uri:
            return False
        return not self._has_active_user_focus_intent()

    def _focus_on_uri_without_interrupt(self, context_uri: str, reason: str) -> bool:
        """Move focus to context URI without interrupting playback."""
        items = self.display_items
        if not items:
            return False
        target_index = next((i for i, item in enumerate(items) if item.uri == context_uri), None)
        if target_index is None:
            return False
        if target_index == self.selected_index:
            return True

        old_index = self.selected_index
        self.selected_index = target_index
        self.carousel.set_target(target_index)
        self._bump_focus_epoch(f'{reason} {old_index}->{target_index}')
        self._reset_pending_focus()
        self._pending_external_focus_uri = None
        self._user_driving = False
        self.renderer.invalidate()
        logger.info(
            'SYNC applied | remote focus moved '
            f'{old_index}->{target_index} | ctx={context_uri[:40]}'
        )
        return True

    def _set_manual_pause_lock(self, reason: str):
        """Block auto-play until explicit positive user intent."""
        self._manual_pause_lock = True
        self._manual_pause_context_uri = self.now_playing.context_uri
        logger.info(
            f'Manual pause lock set ({reason}) | '
            f'ctx={(self._manual_pause_context_uri or "none")[:40]}'
        )

    def _clear_manual_pause_lock(self, reason: str):
        """Allow auto-play again after explicit user intent."""
        if self._manual_pause_lock:
            logger.info(
                f'Manual pause lock cleared ({reason}) | '
                f'ctx={(self._manual_pause_context_uri or "none")[:40]}'
            )
        self._manual_pause_lock = False
        self._manual_pause_context_uri = None

    def _clear_spotify_launch_lock(self, reason: str):
        """Allow focus auto-play after user explicitly starts playback in Spotify."""
        if self._spotify_launch_lock:
            logger.info(f'Spotify launch lock cleared ({reason})')
        self._spotify_launch_lock = False

    def _display_title_for_item(self, item: Optional[CatalogItem]) -> tuple[str, str]:
        """Return (title_source, title_text) used by renderer track header."""
        if not item:
            return ('none', '')
        if (self.now_playing.context_uri == item.uri and
                self.now_playing.track_name and
                (self.now_playing.playing or self.now_playing.paused)):
            return ('now_playing', self.now_playing.track_name)
        return ('none', '')

    def _on_play_committed(self, uri: str, epoch: int):
        """Called by PlaybackController when a play request is accepted."""
        self._user_driving = False
        # Keep requested marker until status confirms focused context is active.
        # This prevents duplicate re-requests while /status lags behind.
        self.playback.last_context_uri = uri
        self._last_play_commit_uri = uri
        self._last_play_commit_at = time.time()
        logger.info(f'Play committed: uri={uri[:40]} epoch={epoch}')

    def _on_play_failed(self, uri: str, epoch: int):
        """Called by PlaybackController when play request failed."""
        # Keep requested marker after a failed attempt so update-loop does not
        # instantly fire the same request again. Retry happens via stale-timeout.
        if self._requested_focus_epoch == epoch and self._requested_focus_uri == uri and self._requested_focus_since <= 0:
            self._requested_focus_since = time.time()
        if self._is_play_request_current(epoch, uri):
            self._user_driving = False
            logger.warning(f'Play failed for current focus: uri={uri[:40]} epoch={epoch}')
        else:
            logger.info(f'Play failed for stale request: uri={uri[:40]} epoch={epoch}')

    def _suppress_focus_autoplay(self, uri: str):
        """Block focus-stable auto-play briefly after explicit user play."""
        self._manual_play_suppress_uri = uri
        self._manual_play_suppress_until = time.time() + MANUAL_PLAY_SUPPRESS_SEC

    def suppress_librespot_recovery(self, seconds: float, reason: str):
        """Pause librespot auto-restart (e.g. during WiFi reconnect)."""
        until = time.time() + seconds
        if until > self._librespot_recovery_suppressed_until:
            self._librespot_recovery_suppressed_until = until
        logger.info(f'Librespot recovery suppressed for {seconds:.0f}s ({reason})')

    def _is_librespot_recovery_suppressed(self) -> bool:
        return time.time() < self._librespot_recovery_suppressed_until

    def _should_run_librespot_recovery(self, reason: str) -> bool:
        """Only restart librespot when Spotify is the active screen and nothing blocks it."""
        if self._is_librespot_recovery_suppressed():
            logger.info(f'Librespot recovery skipped ({reason}): wifi/network change in progress')
            return False
        if self.app_screen != AppScreen.SPOTIFY:
            logger.info(
                f'Librespot recovery skipped ({reason}): active screen={self.app_screen.name}'
            )
            return False
        if getattr(self.local_playback, 'is_active', False):
            logger.info(f'Librespot recovery skipped ({reason}): local playback active')
            return False
        return True

    def _on_librespot_recovery_toast(self, message: str):
        """Show librespot recovery toasts only on the Spotify screen."""
        if self.app_screen != AppScreen.SPOTIFY:
            logger.debug(
                f'Librespot recovery toast suppressed ({self.app_screen.name}): {message}'
            )
            return
        self._show_toast(message)

    def _on_librespot_before_restart(self):
        """Prepare Spotify playback before librespot systemd restart."""
        self.playback.stop_all()
        self.renderer.invalidate()

    def _on_librespot_after_restart(self, ok: bool):
        """Reconnect and retry playback after librespot restart."""
        if not ok:
            self._on_librespot_recovery_toast('Spotify-Verbindung fehlgeschlagen')
            return
        self._connection_fail_count = 0
        self.connected = False
        self._poll_wake_event.set()

        def _post_restart():
            try:
                for _ in range(6):
                    self._refresh_status()
                    if self.connected:
                        break
                    time.sleep(0.5)
                if self.connected:
                    self.playback.retry_failed()
            except Exception as e:
                logger.error(f'Post librespot restart refresh failed: {e}')

        run_async(_post_restart)

    def _maybe_recover_librespot(self, reason: str) -> bool:
        """Try librespot restart if triggers match. Returns True if started."""
        if self.mock_mode:
            return False
        if not self._should_run_librespot_recovery(reason):
            return False
        if reason == 'connection_lost':
            if not self.librespot_recovery.should_restart_for_connection(
                self.connected, self._connection_fail_count
            ):
                return False
        elif reason == 'context_stall':
            pass  # caller validates stall conditions
        elif reason == 'play_timeout_cascade':
            if not self.librespot_recovery.should_restart_for_timeouts():
                return False
        return self.librespot_recovery.maybe_restart(reason)

    def _schedule_deferred_snap_pause(self):
        """Send remote pause only after carousel settles (local mute is immediate)."""
        self._snap_pause_generation += 1
        gen = self._snap_pause_generation

        def _deferred():
            time.sleep(SNAP_PAUSE_SETTLE_SEC)
            if gen != self._snap_pause_generation:
                return
            if self.app_screen != AppScreen.SPOTIFY:
                return
            if self.touch.dragging or not self.carousel.settled:
                return
            self._last_snap_pause_at = time.time()
            run_async(self.api.pause)

        run_async(_deferred)

    def _reset_pending_focus(self, reason: str = ''):
        """Clear pending focus-stability request timer."""
        if self._pending_focus_uri and reason:
            logger.debug(
                f'Pending focus cleared | reason={reason} '
                f'| uri={self._pending_focus_uri[:40]}'
            )
        self._pending_focus_uri = None
        self._pending_focus_since = 0.0

    def _reset_context_switch_watchdog(self):
        """Clear context-switch watchdog timer."""
        self._context_switch_stall_since = 0.0

    def _trigger_context_switch_watchdog(self, focused_item: CatalogItem, stall_age: float):
        """Fail-safe when context-switch loading appears stuck for too long."""
        logger.error(
            'WATCHDOG tripped | context-switch stuck -> hard silent stop | '
            f'age={stall_age:.1f}s | focused="{focused_item.name}" | '
            f'focused_uri={focused_item.uri[:40]} | spotify_ctx={(self.now_playing.context_uri or "none")[:40]} | '
            f'connected={self.connected} | status_unknown={self._status_unknown} | '
            f'pending_focus={(self._pending_focus_uri or "none")[:40]} | '
            f'requested_uri={(self._requested_focus_uri or "none")[:40]} | '
            f'requested_epoch={self._requested_focus_epoch} | focus_epoch={self._focus_epoch}'
        )
        self.playback.stop_all()
        self.playback.last_context_uri = None
        self._reset_pending_focus('watchdog_trip')
        self._pending_external_focus_uri = None
        self._requested_focus_epoch = None
        self._requested_focus_uri = None
        self._requested_focus_since = 0.0
        self._user_driving = False
        self._user_driving_since = 0.0
        self.volume.mute()
        run_async(self.api.pause)
        self._show_toast('Loading cancelled, try again')

    def _check_context_switch_watchdog(self, focused_item: Optional[CatalogItem]):
        """Detect and break out of a stuck context-switch loading state."""
        if focused_item is None or focused_item.is_temp:
            self._reset_context_switch_watchdog()
            return

        focused_uri = focused_item.uri
        requested_current_focus = (
            self._requested_focus_epoch == self._focus_epoch
            and self._requested_focus_uri == focused_uri
        )
        waiting_for_switch_commit = (
            self.playback.play_in_progress
            or self.playback.play_state.should_show_loading
            or self._pending_focus_uri == focused_uri
            or requested_current_focus
        )
        context_mismatch = bool(
            self.now_playing.context_uri
            and self.now_playing.context_uri != focused_uri
        )
        stalled_switch = (
            self._user_activated_playback
            and not self._manual_pause_lock
            and not self._is_item_playing(focused_item)
            and (waiting_for_switch_commit or (self._user_driving and context_mismatch))
        )

        if not stalled_switch:
            self._reset_context_switch_watchdog()
            return

        now = time.time()
        if self._context_switch_stall_since <= 0.0:
            self._context_switch_stall_since = now
            return

        stall_age = now - self._context_switch_stall_since
        spotify_ctx = self.now_playing.context_uri
        if (
            stall_age >= LIBRESPOT_RECOVERY_CONTEXT_STALL_SEC
            and waiting_for_switch_commit
            and not spotify_ctx
            and self.librespot_recovery.should_restart_for_context_stall(
                stall_age, spotify_ctx, waiting_for_switch_commit
            )
        ):
            if self._maybe_recover_librespot('context_stall'):
                self._reset_context_switch_watchdog()
                return

        if stall_age >= CONTEXT_SWITCH_WATCHDOG_TIMEOUT:
            if self._maybe_recover_librespot('watchdog'):
                self._reset_context_switch_watchdog()
                return
            self._trigger_context_switch_watchdog(focused_item, stall_age)
            self._reset_context_switch_watchdog()
            return

        if now - self._last_context_watchdog_log > 5.0:
            logger.warning(
                'WATCHDOG armed | waiting for context-switch commit | '
                f'age={stall_age:.1f}s/{CONTEXT_SWITCH_WATCHDOG_TIMEOUT:.0f}s | '
                f'focused_uri={focused_uri[:40]} | spotify_ctx={(self.now_playing.context_uri or "none")[:40]} | '
                f'waiting_for_commit={waiting_for_switch_commit} | user_driving={self._user_driving}'
            )
            self._last_context_watchdog_log = now

    def _preview_volume(self, level_idx: int, output_type: str, new_val: int):
        """Switch to the edited volume level and apply it immediately."""
        self.volume.index = level_idx
        if output_type == 'speaker':
            set_system_volume(new_val)
        elif output_type == 'bt' and self.bluetooth:
            self.bluetooth.set_volume(new_val)

    def _on_library_cleared(self):
        """Reset in-memory state after library clear (called by SetupMenu)."""
        self.catalog_manager.load()
        with self._temp_item_lock:
            self.temp_item = None
        self.selected_index = 0
        self.carousel.scroll_x = 0.0
        self.carousel.set_target(0)
        self._update_carousel_max_index()
        self.image_cache.cache.clear()
        self.image_cache._access_times.clear()
    
    @property
    def _active_toast(self) -> Optional[str]:
        """Return toast message if still within display duration."""
        if self._toast_message and time.time() - self._toast_time < self._toast_duration:
            return self._toast_message
        self._toast_message = None
        return None
    
    def _log_video_info(self):
        """Log video driver and display info."""
        video_driver = os.environ.get('SDL_VIDEODRIVER', 'default')
        actual_driver = pygame.display.get_driver()
        info = pygame.display.Info()
        
        logger.info(f'Display: {actual_driver} (requested: {video_driver})')
        logger.info(f'Resolution: {info.current_w}x{info.current_h}')
        
        # Check for Raspberry Pi
        if os.path.exists('/proc/device-tree/model'):
            try:
                with open('/proc/device-tree/model', 'r') as f:
                    pi_model = f.read().strip().replace('\x00', '')
                logger.info(f'Device: {pi_model}')
                
                # Only show warning if not using GPU acceleration
                if actual_driver not in ('kmsdrm', 'KMSDRM'):
                    kms_available = self._check_kms_available()
                    if not kms_available:
                        logger.debug('KMS/DRM not detected - GPU acceleration unavailable')
                    else:
                        logger.debug('KMS/DRM detected but not using kmsdrm driver')
            except Exception:
                pass

    def _read_text_file(self, path: str) -> Optional[str]:
        """Read a text file for diagnostics."""
        try:
            with open(path, 'r') as f:
                return f.read().strip()
        except Exception:
            return None

    def _boot_config_status(self) -> str:
        """Return compact boot config diagnostics for field support."""
        config_path = None
        for path in ('/boot/firmware/config.txt', '/boot/config.txt'):
            if os.path.exists(path):
                config_path = path
                break
        if not config_path:
            return 'missing'
        content = self._read_text_file(config_path) or ''
        explicit_overlay = 'dtoverlay=vc4-kms-dsi-ili9881-5inch,rotation=90' in content
        display_auto_detect_active = any(
            line.strip() == 'display_auto_detect=1'
            for line in content.splitlines()
        )
        disable_splash = any(
            line.strip() == 'disable_splash=1'
            for line in content.splitlines()
        )
        return (
            f'path={config_path}, ili9881_5inch_overlay={explicit_overlay}, '
            f'display_auto_detect_active={display_auto_detect_active}, '
            f'disable_splash={disable_splash}'
        )

    def _log_startup_health(self):
        """Log display/touch health so black-screen diagnosis is evidence-based."""
        backlight_value = (
            self._read_text_file(self.sleep_manager.backlight_path)
            if self.sleep_manager.backlight_path else None
        )
        dsi_status = self._read_text_file('/sys/class/drm/card0-DSI-1/status')
        dsi_dpms = self._read_text_file('/sys/class/drm/card0-DSI-1/dpms')
        logger.info(f'HEALTH boot_config: {self._boot_config_status()}')
        logger.info(
            'HEALTH display: '
            f'backlight_path={self.sleep_manager.backlight_path or "none"}, '
            f'backlight_value={backlight_value or "unknown"}, '
            f'dsi_status={dsi_status or "unknown"}, dsi_dpms={dsi_dpms or "unknown"}'
        )
        logger.info(
            'HEALTH touch: '
            f'available={self.evdev_touch.is_available}, '
            f'device={self.evdev_touch.device_name or "none"}, '
            f'path={self.evdev_touch.device_path or "none"}, '
            f'sleep_enabled={self.sleep_manager.sleep_enabled}'
        )

    def _disable_sleep_for_touch(self, reason: str):
        """Disable sleep when touch wake is unavailable, waking display if needed."""
        was_sleeping = self.sleep_manager.is_sleeping
        self.sleep_manager.disable_sleep(f'touch wake unavailable: {reason}')
        if was_sleeping:
            self.bluetooth.resume_monitoring()
            self.tracker.on_wake()
        self.renderer.invalidate()

    def _check_touch_health(self):
        """React if the background touch reader fails after startup."""
        reason = self.evdev_touch.consume_failure_reason()
        if reason:
            self._disable_sleep_for_touch(reason)
    
    def _on_ws_update(self):
        """Called when WebSocket receives an event."""
        logger.debug(f'WebSocket event, context: {self.events.context_uri}')
        if self.sleep_manager.is_sleeping:
            self._poll_wake_event.set()
    
    def _on_ws_reconnect(self):
        """Called when WebSocket reconnects after disconnect."""
        logger.info('WebSocket reconnected - refreshing state')
        self._connection_fail_count = 0
        run_async(self._refresh_status)
    
    @property
    def display_items(self) -> List[CatalogItem]:
        """Return catalog items + tempItem if present."""
        items = self.catalog_manager.items
        if self.temp_item:
            return items + [self.temp_item]
        return items
    
    @property
    def now_playing(self) -> NowPlaying:
        """Thread-safe getter for now_playing state."""
        with self._now_playing_lock:
            return self._now_playing
    
    @now_playing.setter
    def now_playing(self, value: NowPlaying):
        """Thread-safe setter for now_playing state."""
        with self._now_playing_lock:
            self._now_playing = value
    
    @property
    def connected(self) -> bool:
        """Thread-safe getter for connected state."""
        with self._connected_lock:
            return self._connected
    
    @connected.setter
    def connected(self, value: bool):
        """Thread-safe setter for connected state."""
        with self._connected_lock:
            self._connected = value
    
    @property
    def running(self) -> bool:
        """Thread-safe running flag (backed by threading.Event)."""
        return self._running.is_set()
    
    @running.setter
    def running(self, value: bool):
        if value:
            self._running.set()
        else:
            self._running.clear()
    
    def _update_carousel_max_index(self):
        """Update carousel max index when items change."""
        items = self._display_items()
        self.carousel.max_index = max(0, len(items) - 1)
    
    def _on_bt_audio_changed(self, active: bool):
        """Called by BluetoothManager when audio routing changes."""
        self._bt_audio_active = active
        if active:
            # Set initial volume on BT sink
            self.bluetooth.set_volume(self.volume.bt_level)
        self.renderer.invalidate()

    def _handle_signal(self, signum, frame):
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        sig_name = 'SIGTERM' if signum == signal.SIGTERM else 'SIGINT'
        logger.info(f'Received {sig_name}, shutting down...')
        self.running = False
    
    def start(self):
        """Start the application."""
        logger.info('Starting Moki...')
        self.tracker.on_app_started(catalog_size=len(self.catalog_manager.items))
        
        # Pre-load images
        self.image_cache.preload_catalog(self.catalog_manager.items)
        
        if not self.mock_mode:
            self.events.start()
            self.catalog_manager.cleanup_unused_images()
            
            # Set system volume at startup (also unmutes as safety reset)
            self.volume.init()
            
            # Start status polling
            run_async(self._poll_status)
            logger.info(f'Polling {LIBRESPOT_URL}')

            # Force initial connection check (don't wait for first poll interval)
            run_async(self._initial_connect)

            # Start Bluetooth monitoring
            self.bluetooth.start_monitoring()
        else:
            logger.info('Running in MOCK MODE')
            self._startup_ready = True
        
        logger.info('Entering main loop...')
        dt = 1.0 / 60  # Initial delta time
        
        # Main loop
        while self.running:
            self._check_touch_health()
            # Sleep mode: wait for touch/key to wake up
            if self.sleep_manager.is_sleeping:
                # Primary wake: evdev threading.Event (reliable across threads)
                # Fallback: pygame.event.wait with timeout (catches KEYDOWN/QUIT)
                self.evdev_touch.wake_event.wait(0.2)
                if self.evdev_touch.wake_event.is_set():
                    self.evdev_touch.wake_event.clear()
                    self._wake_from_sleep('evdev_touch')
                    pygame.event.clear()  # Discard stale events from sleep
                    continue
                # Check for keyboard/quit events that bypass evdev
                for event in pygame.event.get():
                    if event.type == pygame.KEYDOWN:
                        self._wake_from_sleep(f'key:{event.key}')
                        pygame.event.clear()
                        break
                    elif event.type == pygame.QUIT:
                        self.running = False
                        break
                self._log_sleep_wait_if_due()
                continue
            
            self._handle_events()
            self._update(dt)
            dirty_rects = self._draw()
            
            if dirty_rects:
                pygame.display.update(dirty_rects)
            else:
                pygame.display.flip()
            
            target_fps = self._target_fps()
            is_animating = not self.carousel.settled or self.touch.dragging
            
            if target_fps <= 5 and not is_animating:
                # Idle: true sleep instead of busy-wait, CPU can idle
                frame_start = time.time()
                pygame.time.wait(200)
                dt = time.time() - frame_start
            else:
                dt = self.clock.tick(target_fps) / 1000.0
            
            target_frame_time = 1.0 / target_fps
            spike_threshold = max(0.1, target_frame_time * 1.2)
            if dt > spike_threshold and target_fps > 5:
                logger.warning(f'Frame spike: {dt*1000:.0f}ms (target: {target_fps} FPS)')
            
            self.perf_monitor.update(dt)
            self._log_fps_if_due(target_fps)
        
        # Save progress before shutdown
        logger.info('Shutting down...')
        self._save_progress_on_shutdown()
        self.tracker.on_shutdown()
        self.bluetooth.stop()
        
        # Restore display before exit so next boot doesn't start with black screen
        if self.sleep_manager.is_sleeping:
            self.sleep_manager.wake_up('shutdown')
        
        self.events.stop()
        self.evdev_touch.stop()
        self.local_playback.shutdown()
        pygame.quit()
        logger.info('Moki stopped')
    
    def _target_fps(self) -> int:
        """Calculate target FPS based on current activity.
        
        60 FPS for animations/loading, 10 for playback/menu, 5 for idle.
        """
        if self.setup_menu.is_open or self._volume_hold_start is not None:
            return 10
        if self._voice_search_phase not in (
            VoiceSearchPhase.CLOSED,
            VoiceSearchPhase.RESULTS,
        ):
            return 10
        if self._mokibot_phase not in (
            MokiBotPhase.IDLE,
            MokiBotPhase.PLAYING,
        ):
            return 10
        if self.app_screen == AppScreen.MOKIBOT and self._mokibot_phase == MokiBotPhase.THINKING:
            return 10
        is_animating = not self.carousel.settled or self.touch.dragging
        if is_animating or self.playback.play_state.is_loading:
            return 60
        if self._is_local_media_screen():
            playing, paused, _, _, _, _ = self.local_playback.get_state()
            play_in_progress = (
                self._checkpod_play_in_progress if self.app_screen == AppScreen.CHECKPOD
                else self._local_music_play_in_progress
            )
            if play_in_progress or playing or paused or self._active_toast:
                return 10
            return 5
        elif self.now_playing.playing or self._active_toast:
            return 10
        return 5
    
    def _log_fps_if_due(self, target_fps: int):
        """Log FPS stats periodically and warn on drops."""
        now = time.time()
        if now - self._last_fps_log < self._fps_log_interval:
            return
        
        self._last_fps_log = now
        avg_fps = self.perf_monitor.current_fps
        items = self._display_items()
        focused = items[self.selected_index].name if items and self.selected_index < len(items) else '?'
        if self._is_local_media_screen():
            playing, paused, _, _, playing_ctx, playing_name = self.local_playback.get_state()
            is_loading = (
                self._checkpod_play_in_progress if self.app_screen == AppScreen.CHECKPOD
                else self._local_music_play_in_progress
            )
            playing_ctx = playing_ctx or 'none'
            playing_name = playing_name or 'none'
            is_playing = playing or paused
        else:
            is_loading = self.playback.play_state.is_loading
            playing_ctx = self.now_playing.context_uri or 'none'
            playing_name = self.now_playing.track_name or 'none'
            is_playing = self.now_playing.playing
        api_metrics = self.api.metrics_snapshot() if hasattr(self.api, 'metrics_snapshot') else {}
        suppressed = api_metrics.get('suppressed', {})
        failures = api_metrics.get('failures', {})
        
        logger.info(
            f'STATE | focused="{focused}" | playing="{playing_name}" | ctx={playing_ctx[:40]} '
            f'| driving={self._user_driving} | loading={is_loading} | connected={self.connected} '
            f'| fps={avg_fps:.0f}/{target_fps} | restore_dedup={self._restore_dedup_count} '
            f'| api_suppressed={suppressed} | api_failures={failures}'
        )

        focused_uri = items[self.selected_index].uri if items and self.selected_index < len(items) else None
        if (is_playing and focused_uri and playing_ctx and playing_ctx != 'none'
                and focused_uri != playing_ctx and not is_loading and not self._user_driving):
            logger.warning(
                f'MISMATCH | screen="{focused}" | audio="{playing_name}" '
                f'| focused_uri={focused_uri[:40]} | playing_ctx={playing_ctx[:40]} '
                f'| last_ctx={self.playback.last_context_uri} | interacting={self.user_interacting} '
                f'| settled={self.carousel.settled} | timer={self.play_timer.item is not None}'
            )
        
        if not self.sleep_manager.is_sleeping:
            if target_fps == 60 and avg_fps < 30:
                logger.warning(f'Low FPS during animation: {avg_fps:.1f} (target: 60 FPS)')
            elif target_fps == 10 and avg_fps < 8:
                logger.warning(f'Low FPS while playing: {avg_fps:.1f} (target: 10 FPS)')
            elif target_fps == 5 and avg_fps < 4:
                logger.warning(f'Low FPS while idle: {avg_fps:.1f} (target: 5 FPS)')
    
    def _status_ready_for_play(self, now: Optional[float] = None) -> bool:
        """True when librespot status is fresh enough to auto-start playback."""
        if self._status_unknown:
            return False
        now = now or time.time()
        age = now - self._last_status_ok_at
        if now - self._wake_at < STATUS_READY_WAKE_GRACE_SEC:
            return age < STATUS_READY_WAKE_MAX_AGE
        return age < STATUS_READY_MAX_AGE

    def _poll_status(self):
        """Poll librespot status in background.
        
        Intervals adapt to state: fast when disconnected, slow when idle,
        near-zero during sleep (WebSocket can signal instant wake via
        _poll_wake_event).
        """
        was_fast_polling = False
        while self.running:
            # During sleep: wait up to 30s, but wake instantly on WS signal
            if self.sleep_manager.is_sleeping:
                self._poll_wake_event.wait(timeout=30)
                self._poll_wake_event.clear()
                if not self.running:
                    break
            
            try:
                self._refresh_status()
            except Exception as e:
                self._connection_fail_count += 1
                if self._connection_fail_count >= self._connection_grace_threshold:
                    if self.connected:
                        logger.error(f'Status poll error: {e}')
                    self.connected = False

            if (
                self.sleep_manager.is_sleeping
                and self._connection_fail_count >= 2
                and (not self.connected or self._status_unknown)
            ):
                self._recover_sleep_network('status_unknown')
            
            # Poll faster when disconnected for quicker recovery
            is_fast_polling = not self.connected
            if is_fast_polling != was_fast_polling:
                if is_fast_polling:
                    logger.debug('Fast polling mode (disconnected)')
                else:
                    logger.debug('Normal polling mode (connected)')
                was_fast_polling = is_fast_polling
            
            if is_fast_polling:
                poll_interval = 0.5
            elif not self.now_playing.playing:
                poll_interval = 3.0
            else:
                poll_interval = 1.0
            self._poll_wake_event.wait(timeout=poll_interval)
            self._poll_wake_event.clear()
    
    def _refresh_status(self):
        """Refresh playback status from librespot."""
        raw = self.api.status()
        was_connected = self.connected
        
        # Determine connection with grace period
        has_connection = raw is not None or self.api.is_connected()
        if has_connection:
            if self._connection_fail_count > 0:
                logger.debug(f'Connection recovered after {self._connection_fail_count} failures')
            self._connection_fail_count = 0
            self.connected = True
        else:
            self._connection_fail_count += 1
            if self._connection_fail_count >= self._connection_grace_threshold:
                self.connected = False
        
        # Log connection state changes
        if was_connected != self.connected:
            if self.connected:
                now = time.time()
                if now - self._last_restore_handled_at < 0.5:
                    self._restore_dedup_count += 1
                    logger.info(f'CONNECTION RESTORED deduped (count={self._restore_dedup_count})')
                else:
                    self._last_restore_handled_at = now
                    logger.info(f'CONNECTION RESTORED (was disconnected)')
                    self._startup_ready = True
                    self.playback.retry_failed()
                self._startup_ready = True
            else:
                logger.warning(f'CONNECTION LOST after {self._connection_fail_count} failures')
            logger.info(f'  fail_count={self._connection_fail_count}, status={raw is not None}')

        if not self.connected:
            if self._maybe_recover_librespot('connection_lost'):
                pass
            elif self.librespot_recovery.should_restart_for_timeouts():
                self._maybe_recover_librespot('play_timeout_cascade')
        elif self.librespot_recovery.should_restart_for_timeouts():
            self._maybe_recover_librespot('play_timeout_cascade')
        
        if raw and isinstance(raw, dict):
            self._last_status_ok_at = time.time()
            self._status_unknown = False

            if self._is_local_media_screen():
                return

            api_context_uri = raw.get('context_uri')
            ws_context_uri = self.events.context_uri
            if api_context_uri and ws_context_uri and api_context_uri != ws_context_uri:
                logger.warning(
                    'CONTEXT source mismatch | '
                    f'api_ctx={api_context_uri[:40]} | ws_ctx={ws_context_uri[:40]} | '
                    f'track="{(raw.get("track") or {}).get("name") if isinstance(raw.get("track"), dict) else "none"}"'
                )
            status = LibrespotStatus.from_dict(raw, context_uri=ws_context_uri)

            old_ctx = self.now_playing.context_uri
            old_playing = self.now_playing.playing
            
            self.now_playing = NowPlaying(
                playing=status.playing,
                paused=status.paused,
                stopped=status.stopped,
                context_uri=status.context_uri,
                track_name=status.track_name,
                track_artist=status.track_artist,
                track_album=status.track_album,
                track_cover=status.track_cover,
                track_uri=status.track_uri,
                position=status.position,
                duration=status.duration,
                repeat_context=status.repeat_context,
            )

            if status.context_uri != old_ctx or status.playing != old_playing:
                state = 'playing' if status.playing else ('paused' if status.paused else 'stopped')
                logger.info(f'SPOTIFY changed | {state} "{status.track_name}" | ctx={status.context_uri or "none"}')

            pending_action = self.playback.play_state.pending_action
            if pending_action == 'pause':
                if status.paused or status.stopped or not status.playing:
                    self.playback.play_state.pending_action = None
                    logger.info('pending_action_cleared | action=pause | status_ack=not_playing')
                else:
                    logger.info('pending_action_hold | action=pause | waiting_status_ack=playing')
            elif pending_action == 'play' and status.playing:
                self.playback.play_state.pending_action = None
                logger.info('pending_action_cleared | action=play | status_ack=playing')
            self.tracker.update(self.now_playing)
            
            self._update_temp_item()
            self._check_autoplay()
            self._ensure_repeat_context_for_current_status()
            
            if status.playing and self.sleep_manager.is_sleeping:
                self._wake_from_sleep('spotify_playing')
            
            if status.playing:
                self.auto_pause.on_play(status.context_uri)
                self.auto_pause.check(is_playing=True)
                # Ensure audio goes to BT headphone if active
                self.bluetooth.ensure_stream_on_desired_sink()
            elif status.paused or status.stopped:
                self.auto_pause.on_stop()
        else:
            # Transport/timeout errors are "unknown", not "stopped".
            # Keep last known now_playing to avoid duplicate play re-triggers.
            self._status_unknown = True
            now = time.time()
            if now - self._last_status_unknown_log > 3.0:
                logger.warning(
                    'STATUS unknown | preserving last now_playing snapshot '
                    f'| connected={self.connected} | fail_count={self._connection_fail_count}'
                )
                self._last_status_unknown_log = now
    
    def _check_autoplay(self):
        """Detect autoplay and clear progress when context finishes."""
        self.playback.check_autoplay(self.now_playing)

    def _ensure_repeat_context_for_current_status(self):
        """Keep externally-started albums/playlists inside their context."""
        np = self.now_playing
        context_uri = np.context_uri
        if not np.playing or not is_repeatable_spotify_context(context_uri):
            return

        if np.repeat_context:
            self._repeat_context_uri = context_uri
            return

        now = time.time()
        if self._repeat_context_uri == context_uri and now - self._repeat_context_last_attempt < 15.0:
            return

        self._repeat_context_uri = context_uri
        self._repeat_context_last_attempt = now
        logger.info(f'repeat_context_request | reason=status_playing | uri={context_uri[:50]}')

        def _set_repeat():
            ok = self.api.set_repeat_context(True)
            if ok:
                logger.info(f'repeat_context_on | reason=status_playing | uri={context_uri[:50]}')
            else:
                logger.warning(f'repeat_context_failed | reason=status_playing | uri={context_uri[:50]}')

        run_async(_set_repeat)
    
    def _update_temp_item(self):
        """Update tempItem based on current playback context.
        
        Thread-safe: uses _temp_item_lock since download threads also write temp_item.
        """
        context_uri = self.now_playing.context_uri
        
        if not context_uri:
            with self._temp_item_lock:
                had_temp = self.temp_item is not None
                self.temp_item = None
            if had_temp:
                self._update_carousel_max_index()
                self.renderer.invalidate()
            return
        
        # Check if in catalog (with valid image)
        catalog_item = next((item for item in self.catalog_manager.items if item.uri == context_uri), None)
        if catalog_item and catalog_item.image:
            with self._temp_item_lock:
                had_temp = self.temp_item is not None
                self.temp_item = None
            if had_temp:
                self._update_carousel_max_index()
                self.renderer.invalidate()
            return
        
        # Create/update tempItem
        is_playlist = 'playlist' in context_uri
        collected_covers = self.catalog_manager.get_collected_covers(context_uri) if is_playlist else None
        track_cover = self.now_playing.track_cover
        
        start_download = False
        
        with self._temp_item_lock:
            current_cover_count = len(self.temp_item.images or []) if self.temp_item else 0
            new_cover_count = len(collected_covers or [])
            
            uri_changed = not self.temp_item or self.temp_item.uri != context_uri
            
            needs_update = (
                uri_changed or
                new_cover_count > current_cover_count
            )
            
            if not needs_update:
                return
            
            # Only preserve local image if same URI (prevents wrong cover on wrong item)
            if not uri_changed and self.temp_item.image and self.temp_item.image.startswith('/images/'):
                local_image = self.temp_item.image
            else:
                local_image = None
            
            self.temp_item = CatalogItem(
                id='temp',
                uri=context_uri,
                name=self.now_playing.track_album or ('Playlist' if is_playlist else 'Album'),
                type='playlist' if is_playlist else 'album',
                artist=self.now_playing.track_artist,
                image=local_image or track_cover,
                images=collected_covers,
                is_temp=True
            )
            
            start_download = not local_image and bool(track_cover)
        
        self._update_carousel_max_index()
        self.renderer.invalidate()
        logger.info(f'TempItem: {self.temp_item.name}')
        
        # Download cover in background if we don't have a local image
        if start_download:
            run_async(self._download_temp_cover_async, context_uri, track_cover)
    
    def _download_temp_cover_async(self, context_uri: str, cover_url: str):
        """Download temp item cover in background thread."""
        try:
            local_path = self.catalog_manager.download_temp_image(cover_url)
            if not local_path:
                return

            # Thread-safe update of temp_item
            with self._temp_item_lock:
                if self.temp_item and self.temp_item.uri == context_uri:
                    # Update temp item with downloaded image
                    self.temp_item = CatalogItem(
                        id=self.temp_item.id,
                        uri=self.temp_item.uri,
                        name=self.temp_item.name,
                        type=self.temp_item.type,
                        artist=self.temp_item.artist,
                        image=local_path,
                        images=self.temp_item.images,
                        is_temp=True
                    )
            self.renderer.invalidate()
            logger.info(f'TempItem cover downloaded: {local_path}')
        except Exception as e:
            logger.debug(f'Temp cover download failed: {e}')
    
    def _handle_events(self):
        """Handle pygame events."""
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            
            elif event.type == pygame.MOUSEBUTTONDOWN:
                logger.debug(f'Event: MOUSEBUTTONDOWN at {event.pos}')
                if self.sleep_manager.is_sleeping:
                    self._user_activated_playback = True
                    self._wake_from_sleep(f'pygame_touch:{event.pos}')
                    continue
                self.sleep_manager.reset_timer()
                self._handle_touch_down(event.pos)
            
            elif event.type == pygame.KEYDOWN:
                if self.sleep_manager.is_sleeping:
                    self._user_activated_playback = True
                    self._wake_from_sleep(f'pygame_key:{event.key}')
                    continue
                self.sleep_manager.reset_timer()
                self._handle_key(event.key)
            
            elif event.type == pygame.MOUSEMOTION:
                if self.setup_menu.is_open and self._menu_touch_start is not None:
                    # Menu scroll: track vertical drag (physical x-axis)
                    dx = event.pos[0] - self._menu_touch_start[0]
                    if abs(dx) > 10 and self.renderer.menu_content_overflow > 0:
                        self._menu_touch_scrolled = True
                        self.setup_menu.handle_scroll(
                            dx, self.renderer.menu_content_overflow)
                        self._menu_touch_start = event.pos
                elif self._home_touch_active and self.app_screen == AppScreen.HOME:
                    self.sleep_manager.reset_timer()
                    self._home_touch.on_move(event.pos)
                    if self._home_touch.is_swiping:
                        self._pressed_button = None
                    self.renderer.invalidate()
                elif self.touch.dragging:
                    self.sleep_manager.reset_timer()
                    self.touch.on_move(event.pos)
                    if self._voice_search_touch_active:
                        self.renderer.invalidate()

                    # Cancel delete mode when user starts swiping
                    if self.touch.is_swiping and self.delete_mode_id:
                        self.delete_mode_id = None
                        self.renderer.invalidate()

            elif event.type == pygame.MOUSEBUTTONUP:
                logger.debug(f'Event: MOUSEBUTTONUP at {event.pos}')
                if not self.sleep_manager.is_sleeping:
                    if self.setup_menu.is_open and self._menu_touch_start is not None:
                        if not self._menu_touch_scrolled:
                            # Flash pressed state on tapped menu button
                            for key, rect in self.renderer.menu_button_rects.items():
                                if rect.collidepoint(*event.pos):
                                    if key == 'close':
                                        self._pressed_button = 'menu_close'
                                    elif key.startswith('pin_') or key.startswith('search_') or key.startswith('voice_'):
                                        self._pressed_button = key
                                    self._pressed_time = time.time()
                                    break
                            self.setup_menu.handle_tap(event.pos, self.renderer.menu_button_rects)
                        self._menu_touch_start = None
                        self._menu_touch_scrolled = False
                    else:
                        if self.app_screen == AppScreen.HOME and not self.setup_menu.is_open:
                            self._handle_home_touch_up(event.pos)
                        else:
                            self._handle_touch_up(event.pos)
                    self._handle_button_up()
    
    def _handle_key(self, key):
        """Handle keyboard input."""
        self._user_activated_playback = True
        if key == pygame.K_ESCAPE:
            self.running = False
        elif key == pygame.K_LEFT:
            self._navigate(-1)
        elif key == pygame.K_RIGHT:
            self._navigate(1)
        elif key == pygame.K_SPACE or key == pygame.K_RETURN:
            self._toggle_play()
        elif key == pygame.K_n:
            self._skip_track(self.api.next)
        elif key == pygame.K_p:
            self._skip_track(self.api.prev)
    
    def _handle_touch_down(self, pos):
        """Handle touch/mouse down."""
        self._user_activated_playback = True
        # Menu intercept — track touch start for scroll vs tap detection
        if self.setup_menu.is_open:
            self._menu_touch_start = pos
            self._menu_touch_scrolled = False
            return

        if self.app_screen == AppScreen.SPOTIFY and self._voice_search_phase != VoiceSearchPhase.CLOSED:
            self._handle_voice_search_touch_down(pos)
            return

        if self.app_screen == AppScreen.MOKIBOT:
            self._handle_mokibot_touch_down(pos)
            return

        if self.app_screen == AppScreen.HOME:
            app_id = self._home_icon_at_pos(pos)
            if app_id:
                self._pressed_button = f'home_{app_id}'
                self._pressed_time = time.time()
                self.renderer.invalidate()
            else:
                self._home_touch.on_down(pos)
                self._home_touch_active = True
            return
        
        x, y = pos
        
        carousel_x_min = CAROUSEL_X - CAROUSEL_TOUCH_MARGIN
        carousel_x_max = CAROUSEL_X + COVER_SIZE + CAROUSEL_TOUCH_MARGIN
        
        logger.debug(f'Touch down: pos={pos}, carousel_x_range={carousel_x_min}-{carousel_x_max}')

        # Delete mode is modal: the next tap confirms delete or cancels it.
        # It must never fall through to cover/play handling.
        if self.delete_mode_id:
            self._handle_delete_mode_tap(pos)
            return
        
        # Check button clicks
        if self._check_button_click(pos):
            logger.debug('Touch down: button click')
            return
        
        # Carousel swipes - within carousel X zone, full Y range
        if carousel_x_min <= x <= carousel_x_max:
            logger.debug('Touch down: carousel swipe start')
            self.touch.on_down(pos)
            self.user_interacting = True
        else:
            logger.debug('Touch down: outside carousel')
            self._handle_button_tap(pos)
    
    def _handle_home_touch_up(self, pos):
        """Handle tap release on the home screen (icons + page swipe)."""
        pressed = self._pressed_button
        self._pressed_button = None

        drag_offset = self._home_touch.drag_offset if self._home_touch_active else 0.0
        page_scroll = self.home_pager.scroll_x - drag_offset / SCREEN_HEIGHT
        action, velocity = self._home_touch.on_up(pos)
        self._home_touch_active = False

        pages = home_page_count(len(self.renderer._home_apps))
        if pages > 1 and action in ('left', 'right') and not self._home_touch.long_press_fired:
            abs_vel = abs(velocity)
            v_low, v_mid, v_high = VELOCITY_THRESHOLDS
            velocity_bonus = 0 if abs_vel < v_low else (1 if abs_vel < v_mid else (2 if abs_vel < v_high else 3))
            base_target = round(page_scroll)
            if action == 'right':
                target = min(self.home_pager.max_index, base_target + 1 + velocity_bonus)
            else:
                target = max(0, base_target - 1 - velocity_bonus)
            self.home_pager.scroll_x = page_scroll
            self.home_pager.set_target(target)
            self.renderer.invalidate()
            return

        if pages > 1 and action == 'tap' and not pressed:
            self.home_pager.scroll_x = page_scroll
            self.home_pager.set_target(max(0, min(round(page_scroll), self.home_pager.max_index)))
            self.renderer.invalidate()
            return

        if not pressed or not pressed.startswith('home_'):
            self.renderer.invalidate()
            return

        app_id = pressed.replace('home_', '', 1)
        if self._home_icon_at_pos(pos) == app_id:
            self._open_home_app(app_id)
        self.renderer.invalidate()

    def _open_settings_with_pin(self):
        """Open settings menu behind PIN gate."""
        self._pause_active_playback('settings_open')
        self.setup_menu.open_with_pin()
        self.renderer.invalidate()
        logger.info('Settings PIN entry opened')

    def _check_button_click(self, pos) -> bool:
        """Check if click is on add/delete button."""
        x, y = pos
        
        if self.renderer.add_button_rect:
            bx, by, bw, bh = self.renderer.add_button_rect
            if bx <= x <= bx + bw and by <= y <= by + bh:
                self._save_temp_item()
                return True
        
        if self.renderer.delete_button_rect:
            bx, by, bw, bh = self.renderer.delete_button_rect
            if bx <= x <= bx + bw and by <= y <= by + bh:
                self._delete_current_item()
                return True

        if self.renderer.settings_button_rect:
            bx, by, bw, bh = self.renderer.settings_button_rect
            if bx <= x <= bx + bw and by <= y <= by + bh:
                self._open_settings_with_pin()
                return True

        return False

    @staticmethod
    def _point_in_rect(pos, rect: Optional[tuple]) -> bool:
        """Return True if pos is inside a tuple/pygame-style rect."""
        if not rect:
            return False
        x, y = pos
        bx, by, bw, bh = rect
        return bx <= x <= bx + bw and by <= y <= by + bh

    def _delete_fallback_rect(self) -> tuple:
        """Expected delete overlay hit rect for the centered cover.

        Mirrors Renderer._draw_overlay_button geometry. This is only a
        safety net for the rare case where the visual delete mode is active
        but the renderer hit rect was not captured yet.
        """
        btn_size, margin, touch_padding = 100, 16, 60
        cover_x = CAROUSEL_X
        cover_y = CAROUSEL_CENTER_Y - COVER_SIZE // 2
        btn_x = cover_x + margin
        btn_y = cover_y + COVER_SIZE - btn_size - margin
        hit_x = btn_x - touch_padding
        hit_y = btn_y - touch_padding
        hit_size = btn_size + touch_padding * 2
        return (hit_x, hit_y, hit_size, hit_size)

    def _focused_delete_item(self) -> Optional[CatalogItem]:
        """Return focused catalog item while delete mode is active."""
        items = self.display_items
        if not items or self.selected_index >= len(items):
            return None
        item = items[self.selected_index]
        if item.is_temp or item.id != self.delete_mode_id:
            return None
        return item

    def _handle_delete_mode_tap(self, pos):
        """Confirm or cancel delete mode, consuming the tap either way."""
        item = self._focused_delete_item()
        renderer_rect = self.renderer.delete_button_rect
        remembered_rect = self._delete_button_rect
        fallback_rect = self._delete_fallback_rect()
        active_rect = renderer_rect or remembered_rect

        if item is None:
            logger.info(
                'Delete confirm hit-test | result=stale_item '
                f'| pos={pos} | rect={active_rect} | fallback={fallback_rect} '
                f'| delete_mode_id={self.delete_mode_id}'
            )
            self.delete_mode_id = None
            self._delete_button_rect = None
            self.renderer.invalidate()
            return

        if active_rect is None:
            result = 'confirm' if self._point_in_rect(pos, fallback_rect) else 'rect_missing'
            hit = result == 'confirm'
        else:
            hit = self._point_in_rect(pos, active_rect) or self._point_in_rect(pos, fallback_rect)
            result = 'confirm' if hit else 'miss_cancel'

        logger.info(
            'Delete confirm hit-test | '
            f'result={result} | pos={pos} | rect={active_rect} | fallback={fallback_rect} '
            f'| focused="{item.name}" | focused_id={item.id} | focused_uri={item.uri[:50]} '
            f'| delete_mode_id={self.delete_mode_id}'
        )

        if hit:
            self._delete_current_item()
            return

        self.delete_mode_id = None
        self._delete_button_rect = None
        self.renderer.invalidate()
    
    def _handle_touch_up(self, pos):
        """Handle touch/mouse up."""
        logger.debug(f'Touch up: pos={pos}, dragging={self.touch.dragging}')
        if self.app_screen == AppScreen.SPOTIFY and self._voice_search_phase != VoiceSearchPhase.CLOSED:
            self._handle_voice_search_touch_up(pos)
            return
        if self.app_screen == AppScreen.MOKIBOT:
            self._handle_mokibot_touch_up(pos)
            return
        if not self.touch.dragging:
            logger.debug('Touch up: ignored (not dragging)')
            return
        
        drag_index_offset = -self.touch.drag_offset / (COVER_SIZE + COVER_SPACING)
        visual_position = self.selected_index + drag_index_offset
        
        action, velocity = self.touch.on_up(pos)
        self.carousel.scroll_x = visual_position
        
        x, y = pos
        
        if action in ('left', 'right'):
            abs_vel = abs(velocity)
            v_low, v_mid, v_high = VELOCITY_THRESHOLDS
            velocity_bonus = 0 if abs_vel < v_low else (1 if abs_vel < v_mid else (2 if abs_vel < v_high else 3))
            
            base_target = round(visual_position)
            target = base_target + velocity_bonus if velocity < 0 else base_target - velocity_bonus
            
            target = max(self.selected_index - MAX_SWIPE_JUMP, min(target, self.selected_index + MAX_SWIPE_JUMP))
            active_items = self._display_items()
            target = max(0, min(target, len(active_items) - 1 if active_items else 0))
            
            self._snap_to(target)
        elif action == 'tap':
            # Debounce tap actions
            now = time.time()
            if now - self._last_action_time < ACTION_DEBOUNCE:
                logger.debug('Carousel tap debounced')
                return
            
            # Carousel runs along Y axis - check Y position for tap target
            center_y = CAROUSEL_CENTER_Y  # 640
            if y < center_y - COVER_SIZE // 2:
                # Tap on previous item (lower Y)
                self._navigate(-1)
            elif y > center_y + COVER_SIZE // 2:
                # Tap on next item (higher Y)
                self._navigate(1)
            else:
                logger.debug('Carousel tap: play')
                self._last_action_time = now
                self._pressed_button = 'play'
                self._pressed_time = now
                self._toggle_play()
                self.renderer.invalidate()
    
    def _handle_button_tap(self, pos):
        """Handle direct tap on control buttons with debouncing.
        
        Portrait mode: buttons stacked vertically at X=CONTROLS_X, along Y axis.
        """
        now = time.time()
        if now - self._last_action_time < ACTION_DEBOUNCE:
            logger.debug(f'Button tap debounced at ({pos[0]}, {pos[1]})')
            return
        
        x, y = pos
        center_y = CAROUSEL_CENTER_Y
        btn_spacing = BTN_SPACING  # 155

        # Volume button Y position (matches renderer)
        vol_y = center_y + (COVER_SIZE + COVER_SPACING) + COVER_SIZE_SMALL // 2 - BTN_SIZE // 2

        show_reload = self._is_local_media_screen()
        hp_y = HEADPHONE_BTN_Y_CHECKPOD if show_reload else HEADPHONE_BTN_Y
        show_mic = (
            self.app_screen == AppScreen.SPOTIFY
            and self._voice_search_phase == VoiceSearchPhase.CLOSED
        )

        # Portrait mode: check if X is in button column
        if CONTROLS_X - PLAY_BTN_SIZE <= x <= CONTROLS_X + PLAY_BTN_SIZE:
            button_pressed = None

            # Home — return to launcher from Spotify/CheckPod
            if HOME_BTN_Y - BTN_SIZE // 2 <= y <= HOME_BTN_Y + BTN_SIZE // 2:
                button_pressed = 'home'
                if self._voice_search_phase != VoiceSearchPhase.CLOSED:
                    self._close_voice_search()
                else:
                    self._open_home_screen()
            # Mic — Spotify voice search
            elif (
                show_mic
                and MIC_BTN_Y - BTN_SIZE // 2 <= y <= MIC_BTN_Y + BTN_SIZE // 2
            ):
                button_pressed = 'mic'
                self._on_voice_search_mic()
            # Reload — CheckPod only: restart focused episode from the beginning
            elif (
                show_reload
                and RELOAD_BTN_Y - BTN_SIZE // 2 <= y <= RELOAD_BTN_Y + BTN_SIZE // 2
            ):
                button_pressed = 'reload'
                self._restart_local_media_episode()
            # Headphone — only active when BT device connected
            elif hp_y - BTN_SIZE // 2 <= y <= hp_y + BTN_SIZE // 2 and self.bluetooth.connected_device:
                button_pressed = 'headphone'
                self.bluetooth.toggle_audio()
            # Prev: Y = center_y - btn_spacing (485)
            elif center_y - btn_spacing - BTN_SIZE <= y <= center_y - btn_spacing + BTN_SIZE:
                button_pressed = 'prev'
                if self.app_screen == AppScreen.RADIO:
                    pass
                elif self._is_local_media_screen():
                    self._seek_local_media(-30)
                else:
                    self._skip_track(self.api.prev)
            # Play: Y = center_y (640)
            elif center_y - PLAY_BTN_SIZE <= y <= center_y + PLAY_BTN_SIZE:
                button_pressed = 'play'
                self._toggle_play()
            # Next: Y = center_y + btn_spacing (795)
            elif center_y + btn_spacing - BTN_SIZE <= y <= center_y + btn_spacing + BTN_SIZE:
                button_pressed = 'next'
                if self.app_screen == AppScreen.RADIO:
                    pass
                elif self._is_local_media_screen():
                    self._seek_local_media(30)
                else:
                    self._skip_track(self.api.next)
            # Volume: Y = vol_y (~1173) — start hold timer; action fires on release
            elif vol_y - BTN_SIZE <= y <= vol_y + BTN_SIZE:
                button_pressed = 'volume'
                self._volume_hold_start = now
                self._menu_hold_triggered = False
                # Don't toggle volume here — wait for button up (short tap) or hold (menu)
            
            if button_pressed:
                logger.debug(f'Button press: {button_pressed}')
                self._last_action_time = now
                self._pressed_button = button_pressed
                self._pressed_time = now
                self.renderer.invalidate()
    
    def _snap_to(self, target_index: int):
        """Snap carousel to a specific index.

        When the index changes this is the single point where playback
        is interrupted: timer cancelled, running play-thread invalidated,
        and instant silence sent to librespot.
        """
        items = self._display_items()
        if not items:
            return

        target_index = max(0, min(target_index, len(items) - 1))

        if target_index != self.selected_index:
            old_index = self.selected_index
            self.selected_index = target_index
            self.carousel.set_target(target_index)
            self._bump_focus_epoch(f'snap {old_index}->{target_index}')
            self._reset_pending_focus('snap_focus_changed')
            self._checkpod_pending_focus_uri = None
            self._checkpod_pending_focus_since = 0.0
            self._checkpod_play_failed_uri = None
            self._checkpod_play_failed_at = 0.0
            self._local_music_pending_focus_uri = None
            self._local_music_pending_focus_since = 0.0
            self._local_music_play_failed_uri = None
            self._local_music_play_failed_at = 0.0
            self._clear_manual_pause_lock('focus_changed')

            self.play_timer.cancel()
            if self._is_local_media_screen():
                self._save_local_media_progress_now('snap')
                self.local_playback.stop(save_progress=False)
            else:
                self.playback.stop_all()
                self.playback.last_context_uri = None
                self.volume.mute()
                now = time.time()
                should_pause_remote = (
                    now - self._last_snap_pause_at > 0.4 and
                    (self.now_playing.playing or self.playback.has_pending_play)
                )
                if should_pause_remote:
                    self._schedule_deferred_snap_pause()
            self._user_driving = True
            self._user_driving_since = time.time()

            item = items[target_index]
            if not self._is_local_media_screen() and not item.is_temp and not self._is_item_playing(item):
                self.playback.play_state.start_loading()
            logger.info(f'Snap: {old_index} -> {target_index}, item={item.name}, _user_driving=True')
        else:
            self.carousel.set_target(target_index)
        if self.app_screen == AppScreen.CHECKPOD:
            self._maybe_load_more_checkpod_episodes()
    
    def _navigate(self, direction: int):
        """Navigate carousel."""
        items = self._display_items()
        if not items:
            return
        
        new_index = max(0, min(self.selected_index + direction, len(items) - 1))
        self._snap_to(new_index)
    
    def _is_item_playing(self, item: CatalogItem) -> bool:
        """Check if an item is currently playing."""
        return self.playback.is_item_playing(item, self.now_playing)

    def _is_paused_same_focus_context(self, item: CatalogItem) -> bool:
        """Spotify is paused on the same context as carousel focus (do not auto-resume from dwell)."""
        return (
            item.uri == self.now_playing.context_uri
            and self.now_playing.paused
        )

    def _skip_track(self, api_fn):
        """Save progress, mark as user action, then skip prev/next."""
        self.playback.last_user_play_time = time.time()
        self.playback.save_progress(self.now_playing, force=True)

        def _do_skip():
            if not api_fn():
                time.sleep(1)
                if not api_fn():
                    self._show_toast('Not connected')

        run_async(_do_skip)

    def _toggle_play(self):
        """Toggle play/pause."""
        self._user_activated_playback = True
        if self.app_screen == AppScreen.CHECKPOD:
            self._toggle_checkpod_play()
            return
        if self.app_screen == AppScreen.LOCAL_MUSIC:
            self._toggle_local_music_play()
            return
        if self.app_screen == AppScreen.RADIO:
            self._toggle_radio_play()
            return
        if self.mock_mode:
            self._toggle_mock_play()
            return
        if self.now_playing.playing or self.playback.has_pending_play:
            self._set_manual_pause_lock('pause_tap')
        else:
            self._clear_manual_pause_lock('play_tap')
            self._clear_spotify_launch_lock('play_tap')
        items = self.display_items
        if self.now_playing.paused and items and self.selected_index < len(items):
            focused_item = items[self.selected_index]
            if not focused_item.is_temp:
                logger.info(
                    'Paused state: forcing focused context play '
                    f'(focused={focused_item.uri[:40]}, paused_ctx={(self.now_playing.context_uri or "none")[:40]})'
                )
                self._suppress_focus_autoplay(focused_item.uri)
                self._play_item(focused_item.uri)
                return
        if items and self.selected_index < len(items):
            focused = items[self.selected_index]
            if (
                not focused.is_temp
                and not (self.now_playing.playing or self.playback.has_pending_play)
            ):
                self._suppress_focus_autoplay(focused.uri)
        self.playback.toggle_play(items, self.selected_index, self.now_playing)

    def _restart_local_media_episode(self):
        """Clear saved progress and restart the focused local track from the beginning."""
        items = self._display_items()
        if not items or self.selected_index >= len(items):
            return
        item = items[self.selected_index]
        manager = self._local_media_manager()
        if not manager:
            return

        if self.local_playback.is_active:
            self.local_playback.stop(save_progress=False)
        manager.clear_progress(item.uri)
        if self.app_screen == AppScreen.CHECKPOD:
            self._checkpod_pending_focus_uri = None
            self._checkpod_pending_focus_since = 0.0
            self._checkpod_play_failed_uri = None
            self._checkpod_play_failed_at = 0.0
            label = 'CheckPod'
        else:
            self._local_music_pending_focus_uri = None
            self._local_music_pending_focus_since = 0.0
            self._local_music_play_failed_uri = None
            self._local_music_play_failed_at = 0.0
            label = 'Local music'
        self._clear_manual_pause_lock('local_media_reload')
        self._user_activated_playback = True
        logger.info(f'{label} track restarted: {item.name}')
        self._play_local_media_item(item, from_beginning=True)

    def _restart_checkpod_episode(self):
        self._restart_local_media_episode()

    def _seek_local_media(self, delta_seconds: int):
        """Skip forward/back within the current local media track."""
        playing, paused, _, _, _, _ = self.local_playback.get_state()
        if not (playing or paused):
            return
        if not self.local_playback.seek_relative(delta_seconds):
            return
        _, _, position_ms, duration_ms, context_uri, track_name = self.local_playback.get_state()
        manager = self._manager_for_context_uri(context_uri) if context_uri else None
        if manager:
            manager.save_progress(
                context_uri, position_ms, duration_ms, track_name, force=True
            )
        self.renderer.invalidate()

    def _seek_checkpod(self, delta_seconds: int):
        self._seek_local_media(delta_seconds)

    def _toggle_local_music_play(self):
        """Toggle local music playback for the focused track."""
        items = self._display_items()
        if not items or self.selected_index >= len(items):
            return
        item = items[self.selected_index]
        playing, paused, _, _, context_uri, _ = self.local_playback.get_state()

        if playing:
            self._save_local_media_progress_now('local_music_pause_tap')
            self.local_playback.pause()
            self._set_manual_pause_lock('local_music_pause_tap')
            return

        if paused and context_uri == item.uri:
            self._clear_manual_pause_lock('local_music_play_tap')
            self._clear_local_media_launch_lock('play_tap')
            self.volume.unmute()
            self.local_playback.resume()
            return

        self._clear_manual_pause_lock('local_music_play_tap')
        from_beginning = bool(context_uri and context_uri != item.uri)
        self._play_local_music_item(item, from_beginning=from_beginning)

    def _toggle_checkpod_play(self):
        """Toggle CheckPod local playback for the focused episode."""
        items = self._display_items()
        if not items or self.selected_index >= len(items):
            return
        item = items[self.selected_index]
        playing, paused, _, _, context_uri, _ = self.local_playback.get_state()

        if playing:
            self._save_checkpod_progress_now('checkpod_pause_tap')
            self.local_playback.pause()
            self._set_manual_pause_lock('checkpod_pause_tap')
            return

        if paused and context_uri == item.uri:
            self._clear_manual_pause_lock('checkpod_play_tap')
            self._clear_checkpod_launch_lock('play_tap')
            self.volume.unmute()
            self.local_playback.resume()
            return

        self._clear_manual_pause_lock('checkpod_play_tap')
        from_beginning = bool(context_uri and context_uri != item.uri)
        self._play_checkpod_item(item, from_beginning=from_beginning)

    def _toggle_radio_play(self):
        """Toggle Radio TEDDY live stream playback."""
        items = self._display_items()
        if not items:
            return
        item = items[0]
        playing, paused, _, _, context_uri, _ = self.local_playback.get_state()

        if playing:
            self.local_playback.pause()
            self._set_manual_pause_lock('radio_pause_tap')
            return

        if paused and context_uri == item.uri:
            self._clear_manual_pause_lock('radio_play_tap')
            self._clear_radio_launch_lock('play_tap')
            self.volume.unmute()
            self.local_playback.resume()
            return

        self._clear_manual_pause_lock('radio_play_tap')
        self._play_radio_stream()
    
    def _toggle_mock_play(self):
        """Toggle mock playback (no real API calls)."""
        items = self.display_items
        self.playback.mock_playing = not self.playback.mock_playing
        if self.playback.mock_playing and items:
            item = items[self.selected_index]
            ct = item.current_track if isinstance(item.current_track, dict) else None
            self.now_playing = NowPlaying(
                playing=True,
                context_uri=item.uri,
                track_uri=ct.get('uri') if ct else None,
                track_name=ct.get('name', item.name) if ct else item.name,
                track_artist=ct.get('artist', item.artist) if ct else item.artist,
                position=self.playback.mock_position,
                duration=self.playback.mock_duration,
            )
        else:
            self.now_playing = NowPlaying(paused=True, context_uri=self.now_playing.context_uri)
    
    def _play_item(self, uri: str, from_beginning: bool = False):
        """Queue a play request via the playback controller."""
        logger.warning(f'PLAY enqueue | uri={uri[:40]} | epoch={self._focus_epoch} | from_beginning={from_beginning}')
        self._user_driving = True
        self._user_driving_since = time.time()
        if self.now_playing.context_uri and self.now_playing.context_uri != uri:
            self.playback.save_progress(self.now_playing, force=True)
        self.playback.play_item(uri, from_beginning, self._focus_epoch)
    
    def _on_wake(self):
        """Called when waking from sleep - reconnect and reset state."""
        self.bluetooth.resume_monitoring()
        self._user_driving = False
        self._reset_pending_focus('play_enqueued')
        self.tracker.on_wake()
        self._wake_at = time.time()
        self._status_unknown = False
        self._poll_wake_event.set()
        logger.info('=' * 40)
        logger.info('WAKE UP START')
        logger.info(f'  Connection state: {self.connected}')
        logger.info(f'  Fail count: {self._connection_fail_count}')
        logger.info(f'  Playing: {self.now_playing.playing}')
        
        # Mark disconnected so CONNECTION RESTORED fires on next successful poll,
        # which triggers retry_failed() for any play request that timed out.
        self._connection_fail_count = 0
        self.connected = False
        
        # Burst status refresh so play gate has a fresh snapshot immediately.
        def wake_refresh():
            try:
                for attempt in range(4):
                    self._refresh_status()
                    if self._last_status_ok_at >= self._wake_at:
                        logger.info(f'  Wake status refresh OK (attempt {attempt + 1})')
                        break
                    time.sleep(0.4)
                logger.info(f'  Post-refresh connected: {self.connected}')
                logger.info(f'  Post-refresh playing: {self.now_playing.playing}')
                logger.info(f'  Post-refresh status_age={time.time() - self._last_status_ok_at:.1f}s')
                logger.info('WAKE UP COMPLETE')
                logger.info('=' * 40)
            except Exception as e:
                logger.error(f'  Wake refresh failed: {e}')
                logger.info('WAKE UP FAILED')
                logger.info('=' * 40)
            finally:
                self._poll_wake_event.set()
        
        run_async(wake_refresh)

    def _wake_from_sleep(self, reason: str):
        """Wake from sleep and emit one high-signal diagnostic line."""
        if not self.sleep_manager.is_sleeping:
            return
        logger.info(
            f'Wake requested | reason={reason} | '
            f'touch_available={self.evdev_touch.is_available} | '
            f'touch_device={self.evdev_touch.device_name or "none"} | '
            f'touch_path={self.evdev_touch.device_path or "none"}'
        )
        self.sleep_manager.wake_up(reason)
        self._on_wake()

    def _log_sleep_wait_if_due(self):
        """Log periodic sleeping heartbeat so black-screen reports have context."""
        now = time.time()
        if now - self._last_sleep_wait_log < 60:
            return
        self._last_sleep_wait_log = now
        logger.info(
            f'Sleep wait | touch_available={self.evdev_touch.is_available} | '
            f'touch_device={self.evdev_touch.device_name or "none"} | '
            f'wake_event_set={self.evdev_touch.wake_event.is_set()}'
        )
        
    
    def _has_network_connection(self) -> bool:
        """Check if any network interface is connected via NetworkManager."""
        try:
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'STATE', 'general'],
                capture_output=True, text=True, timeout=3,
            )
            return result.stdout.strip().lower().startswith('connected')
        except Exception:
            return True

    def _active_wifi_connection_name(self) -> Optional[str]:
        """Return the active WiFi connection profile name, if NetworkManager knows one."""
        try:
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show', '--active'],
                capture_output=True, text=True, timeout=3,
            )
            for line in result.stdout.splitlines():
                if ':802-11-wireless' in line:
                    return line.split(':', 1)[0]
        except Exception as e:
            logger.debug(f'Could not read active WiFi connection: {e}')
        return None

    def _recover_sleep_network(self, reason: str):
        """Reconnect WiFi when the interface stays associated but traffic is dead during sleep."""
        now = time.time()
        if now - self._last_sleep_network_reconnect_at < 60:
            return
        self._last_sleep_network_reconnect_at = now

        con_name = self._active_wifi_connection_name()
        if not con_name:
            logger.warning(f'Sleep network recovery skipped ({reason}): no active WiFi profile')
            return

        logger.warning(
            f'Sleep network recovery ({reason}) | reconnecting WiFi profile "{con_name}" '
            f'| fail_count={self._connection_fail_count}'
        )

        def _reconnect():
            commands = [
                ['sudo', 'iw', 'dev', 'wlan0', 'set', 'power_save', 'off'],
                ['sudo', 'nmcli', 'device', 'set', 'wlan0', 'autoconnect', 'yes'],
                ['sudo', 'nmcli', 'con', 'modify', con_name, '802-11-wireless.band', 'bg'],
                ['sudo', 'nmcli', 'con', 'up', con_name],
            ]
            for cmd in commands:
                try:
                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12)
                except Exception as e:
                    logger.warning(f'Sleep network recovery command failed: {cmd[1]} ({e})')

        run_async(_reconnect)

    def _get_cached_network_status(self) -> bool:
        """Return cached network status, refreshing every 10 seconds."""
        now = time.time()
        if now - self._network_check_time >= 10:
            self._cached_has_network = self._has_network_connection()
            self._network_check_time = now
        return self._cached_has_network

    def _initial_connect(self):
        """Initial connection with fast retries then slower backoff.

        Tries quickly first (every 2s for ~20s) since go-librespot usually
        boots within 10s on Pi. Falls back to slower retries after that.
        """
        start_time = time.time()
        max_retries = 20
        for attempt in range(max_retries):
            try:
                self._refresh_status()
                if self.connected:
                    logger.info(f'Connected to librespot (attempt {attempt + 1})')
                    break
            except Exception as e:
                logger.warning(f'Connection attempt {attempt + 1}/{max_retries} failed: {e}')

            # Fast retries first (2s), then slow down (max 10s)
            delay = 2 if attempt < 10 else min(2 ** (attempt - 10), 10)
            time.sleep(delay)
        else:
            logger.error(f'Failed to connect to librespot after {max_retries} attempts')

        self._startup_ready = True

        # Give NetworkManager time to auto-connect to a known network
        elapsed = time.time() - start_time
        if elapsed < 10:
            time.sleep(10 - elapsed)

        if not self._has_network_connection():
            logger.info('No network connection detected, opening WiFi setup')
            self.setup_menu.show_wifi()
    
    def _save_temp_item(self):
        """Save the current temp item to catalog."""
        if self._saving:
            return
        self._saving = True
        
        try:
            with self._temp_item_lock:
                temp = self.temp_item
            
            if not temp:
                return
            
            if not temp.image:
                logger.warning(f'Cannot save item without image: {temp.name}')
                return
            
            logger.info(f'Saving: {temp.name}')
            
            item_data = {
                'type': temp.type,
                'uri': temp.uri,
                'name': temp.name,
                'artist': temp.artist,
                'image': temp.image,
            }
            
            success = self.catalog_manager.save_item(item_data)
            
            if success:
                self.catalog_manager.load()
                self._update_carousel_max_index()
                self.image_cache.preload_catalog(self.catalog_manager.items)
                with self._temp_item_lock:
                    if self.temp_item and self.temp_item.uri == temp.uri:
                        self.temp_item = None
                self.renderer.invalidate()
        finally:
            self._saving = False
    
    def _delete_current_item(self):
        """Delete the current item from catalog."""
        logger.info(
            f'Delete requested | delete_mode_id={self.delete_mode_id} '
            f'| deleting={self._deleting}'
        )
        if not self.delete_mode_id:
            logger.warning('Delete ignored | reason=no_delete_mode_id')
            return
        if self._deleting:
            logger.warning('Delete ignored | reason=already_deleting')
            return
        self._deleting = True
        
        try:
            item_id = self.delete_mode_id
            old_index = self.selected_index
            
            item = next((i for i in self.catalog_manager.items if i.id == item_id), None)
            if item:
                logger.info(f'Deleting: {item.name} | id={item.id} | uri={item.uri[:50]}')
            else:
                logger.warning(f'Delete target not found in catalog | id={item_id}')
            
            success = self.catalog_manager.delete_item(item_id)
            
            if success:
                self.catalog_manager.load()
                self._update_carousel_max_index()
                
                new_index = max(0, old_index - 1)
                if self.display_items:
                    new_index = min(new_index, len(self.display_items) - 1)
                    self.selected_index = new_index
                    self._bump_focus_epoch(f'delete select -> {new_index}')
                    self.carousel.scroll_x = float(new_index)
                    self.carousel.set_target(new_index)
                    
                    new_item = self.display_items[new_index]
                    logger.info(
                        f'Delete success | removed_id={item_id} | '
                        f'new_index={new_index} | new_item="{new_item.name}" | '
                        f'new_uri={new_item.uri[:50]}'
                    )
                    if not new_item.is_temp:
                        self._play_item(new_item.uri)
                else:
                    logger.info(f'Delete success | removed_id={item_id} | catalog empty')
            else:
                logger.warning(f'Delete failed | id={item_id}')
            
            self.delete_mode_id = None
            self._delete_button_rect = None
            self.renderer.invalidate()
        finally:
            self._deleting = False
    
    def _trigger_delete_mode(self):
        """Trigger delete mode for the currently selected item."""
        if self.app_screen != AppScreen.SPOTIFY:
            return
        items = self.display_items
        if not items or self.selected_index >= len(items):
            return
        
        item = items[self.selected_index]
        if item.is_temp:
            return
        
        logger.info(f'Delete mode: {item.name}')
        self.delete_mode_id = item.id
        self._delete_button_rect = None
        self.renderer.invalidate()
    
    def _save_progress_on_shutdown(self):
        """Save progress synchronously before shutdown."""
        if self.app_screen == AppScreen.CHECKPOD or self.local_playback.is_active:
            self._save_checkpod_progress_now('shutdown')
            self.local_playback.stop(save_progress=False)
        self.playback.save_progress_on_shutdown(self.now_playing)

    def _is_local_media_item_playing(self, item: CatalogItem) -> bool:
        playing, _, _, _, context_uri, _ = self.local_playback.get_state()
        return bool(item and context_uri == item.uri and playing)

    def _is_local_media_paused_same_focus(self, item: CatalogItem) -> bool:
        playing, paused, _, _, context_uri, _ = self.local_playback.get_state()
        return bool(item and context_uri == item.uri and paused and not playing)

    def _is_checkpod_item_playing(self, item: CatalogItem) -> bool:
        return self._is_local_media_item_playing(item)

    def _is_checkpod_paused_same_focus(self, item: CatalogItem) -> bool:
        return self._is_local_media_paused_same_focus(item)

    def _update_local_music_autoplay(self, focused_item: Optional[CatalogItem]):
        if focused_item is None:
            return
        ready = (
            self._user_activated_playback
            and not self._manual_pause_lock
            and not self._local_music_launch_lock
            and not self._local_music_play_in_progress
            and self.carousel.settled
            and not self.touch.dragging
        )
        if not ready:
            self._local_music_pending_focus_uri = None
            self._local_music_pending_focus_since = 0.0
            return

        if self._is_local_media_item_playing(focused_item):
            self._local_music_pending_focus_uri = None
            self._local_music_pending_focus_since = 0.0
            return

        if self._is_local_media_paused_same_focus(focused_item):
            self._local_music_pending_focus_uri = None
            self._local_music_pending_focus_since = 0.0
            return

        if (
            self._local_music_play_failed_uri == focused_item.uri
            and time.time() - self._local_music_play_failed_at < 30.0
        ):
            return

        focused_uri = focused_item.uri
        now = time.time()
        if self._local_music_pending_focus_uri != focused_uri:
            self._local_music_pending_focus_uri = focused_uri
            self._local_music_pending_focus_since = now
            logger.info(f'Local music focus stable timer start: {focused_item.name} (1s)')
        elif now - self._local_music_pending_focus_since >= 1.0:
            logger.info(f'Local music focus stable 1s -> play: {focused_item.name}')
            self._local_music_pending_focus_uri = None
            self._local_music_pending_focus_since = 0.0
            self._play_local_music_item(focused_item)

    def _update_checkpod_autoplay(self, focused_item: Optional[CatalogItem]):
        if focused_item is None:
            return
        checkpod_ready = (
            self._user_activated_playback
            and not self._manual_pause_lock
            and not self._checkpod_launch_lock
            and not self._checkpod_play_in_progress
            and self.carousel.settled
            and not self.touch.dragging
        )
        if not checkpod_ready:
            self._checkpod_pending_focus_uri = None
            self._checkpod_pending_focus_since = 0.0
            return

        if self._is_checkpod_item_playing(focused_item):
            self._checkpod_pending_focus_uri = None
            self._checkpod_pending_focus_since = 0.0
            return

        if self._is_checkpod_paused_same_focus(focused_item):
            self._checkpod_pending_focus_uri = None
            self._checkpod_pending_focus_since = 0.0
            return

        if (
            self._checkpod_play_failed_uri == focused_item.uri
            and time.time() - self._checkpod_play_failed_at < 30.0
        ):
            return

        focused_uri = focused_item.uri
        now = time.time()
        if self._checkpod_pending_focus_uri != focused_uri:
            self._checkpod_pending_focus_uri = focused_uri
            self._checkpod_pending_focus_since = now
            logger.info(f'CheckPod focus stable timer start: {focused_item.name} (1s)')
        elif now - self._checkpod_pending_focus_since >= 1.0:
            logger.info(f'CheckPod focus stable 1s -> play: {focused_item.name}')
            self._checkpod_pending_focus_uri = None
            self._checkpod_pending_focus_since = 0.0
            self._play_checkpod_item(focused_item)

    def _save_local_media_progress_if_due(self):
        if not self._is_local_media_screen():
            return
        now = time.time()
        if self.app_screen == AppScreen.CHECKPOD:
            last_save = self._last_checkpod_progress_save
        else:
            last_save = self._last_local_music_progress_save
        if now - last_save < PROGRESS_SAVE_INTERVAL:
            return
        playing, paused, position_ms, duration_ms, context_uri, track_name = self.local_playback.get_state()
        manager = self._manager_for_context_uri(context_uri) if context_uri else None
        if manager and (playing or paused):
            live_position = self.local_playback.get_live_position_ms()
            if live_position is not None:
                position_ms = live_position
            if position_ms > 0:
                manager.save_progress(
                    context_uri, position_ms, duration_ms, track_name, force=False
                )
                if context_uri.startswith('local:music:'):
                    self._last_local_music_context_uri = context_uri
                    self._last_local_music_progress_save = now
                else:
                    self._last_checkpod_context_uri = context_uri
                    self._last_checkpod_progress_save = now

    def _save_checkpod_progress_if_due(self):
        self._save_local_media_progress_if_due()
    
    def _collect_cover_async(self, context_uri: str, cover_url: str):
        """Collect playlist cover in background thread."""
        try:
            new_cover_added = self.catalog_manager.collect_cover_for_playlist(
                context_uri, cover_url
            )
            if new_cover_added:
                # Schedule UI update on next frame (thread-safe)
                self._update_temp_item()
                self.renderer.invalidate()
        except Exception as e:
            logger.debug(f'Cover collection failed: {e}')
    
    def _sync_to_playing(self):
        """Sync carousel focus to currently playing Spotify context.

        While _user_driving is True (user recently swiped/played), only accept
        confirmation of our own play request. While False, accept anything
        (external Spotify control, autoplay).
        """
        if self.app_screen != AppScreen.SPOTIFY:
            return
        items = self.display_items
        if not items:
            return

        context_uri = self.now_playing.context_uri
        if not context_uri:
            self._pending_external_focus_uri = None
            return

        focused = items[self.selected_index].name if self.selected_index < len(items) else '?'
        focused_uri = items[self.selected_index].uri if self.selected_index < len(items) else None
        logger.info(
            f'SYNC check | spotify={context_uri[:40]} | focused="{focused}" '
            f'| driving={self._user_driving} | epoch={self._focus_epoch}'
        )

        if focused_uri == context_uri:
            self._pending_external_focus_uri = None
            self.playback.last_context_uri = context_uri
            if (
                self.now_playing.playing
                and not self.playback.has_pending_play
                and not self._manual_pause_lock
                and not self.playback.pause_intent_active
            ):
                self.volume.unmute()
            elif self.now_playing.playing and (self._manual_pause_lock or self.playback.pause_intent_active):
                logger.info(
                    'unmute_guard_blocked | reason=pause_intent_or_manual_lock '
                    f'| manual_pause_lock={self._manual_pause_lock} | '
                    f'pause_intent_active={self.playback.pause_intent_active}'
                )
            logger.info('SYNC ok | focused context already matches Spotify')
            return

        if not self.now_playing.playing:
            self._pending_external_focus_uri = None
            logger.info('SYNC hold | spotify not playing, skip focus sync')
            return

        if self._has_active_user_focus_intent():
            self._pending_external_focus_uri = context_uri
            logger.info(
                'SYNC blocked | active user intent, deferring remote focus '
                f'ctx={context_uri[:40]}'
            )
            return

        # Safe remote sync path: move UI focus only, never pause/mute/stop playback.
        target_uri = self._pending_external_focus_uri or context_uri
        if self._focus_on_uri_without_interrupt(target_uri, reason='remote_sync'):
            return

        # If item not yet available (e.g. temp item not materialized), keep pending.
        self._pending_external_focus_uri = target_uri
        logger.info(
            'SYNC pending | remote context not in display_items yet '
            f'ctx={target_uri[:40]}'
        )
    
    def _update(self, dt: float):
        """Update application state."""
        self._check_touch_health()
        items = self._display_items()
        if items:
            self.selected_index = max(0, min(self.selected_index, len(items) - 1))
        
        # Update carousel
        was_animating = not self.carousel.settled
        self.carousel.update(dt)
        if self.app_screen == AppScreen.HOME:
            self.home_pager.update(dt)
        if self._voice_search_phase == VoiceSearchPhase.RESULTS:
            self.voice_search_carousel.update(dt)
        
        focused_item = items[self.selected_index] if self.selected_index < len(items) else None
        if self._manual_pause_lock and self._manual_pause_context_uri:
            active_ctx = self.now_playing.context_uri
            if active_ctx and active_ctx != self._manual_pause_context_uri:
                if self.playback.pause_intent_active:
                    logger.info(
                        'Manual pause lock retained (active_context_changed) | '
                        'reason=pause_intent_active'
                    )
                else:
                    self._clear_manual_pause_lock('active_context_changed')

        # Focus-stable request policy:
        # - mute immediately on swipe (_snap_to)
        # - only request play when drag is finished, carousel is settled,
        #   focus remained unchanged for 1s, and we're connected.
        now = time.time()
        status_ready = self._status_ready_for_play(now)
        paused_focused_context = (
            focused_item is not None
            and self.now_playing.paused
            and self.now_playing.context_uri == focused_item.uri
        )
        prioritize_remote_focus = self._should_prioritize_remote_focus(focused_item)
        if prioritize_remote_focus:
            # Prevent the focused auto-play loop from overriding active remote playback.
            self._reset_pending_focus('prioritize_remote_focus')
            if self._requested_focus_uri == (focused_item.uri if focused_item else None):
                self._requested_focus_epoch = None
                self._requested_focus_uri = None
                self._requested_focus_since = 0.0
        stable_ready = (
            self.app_screen == AppScreen.SPOTIFY
            and self._startup_ready
            and self.connected
            and (status_ready or paused_focused_context)
            and not prioritize_remote_focus
            and self._user_activated_playback
            and not self._manual_pause_lock
            and not self._spotify_launch_lock
            and not self.playback.pause_intent_active
            and self.carousel.settled
            and not self.touch.dragging
            and focused_item is not None
            and not focused_item.is_temp
        )

        if stable_ready:
            if self._is_item_playing(focused_item):
                self._reset_pending_focus('focused_item_already_playing')
                self._requested_focus_epoch = None
                self._requested_focus_uri = None
                self._requested_focus_since = 0.0
                self.volume.unmute()
            elif self._is_paused_same_focus_context(focused_item):
                logger.info(
                    'PLAY skip | paused on focused context, no auto resume '
                    f'(ctx={(self.now_playing.context_uri or "none")[:40]})'
                )
                self._reset_pending_focus('paused_same_focus_context')
                self._requested_focus_epoch = None
                self._requested_focus_uri = None
                self._requested_focus_since = 0.0
                self.volume.unmute()
            elif not self.playback.play_in_progress:
                focused_uri = focused_item.uri
                if (
                    self._manual_play_suppress_uri == focused_uri
                    and now < self._manual_play_suppress_until
                ):
                    self._reset_pending_focus('manual_play_suppress')
                else:
                    hold_current_request = False
                    if (self._requested_focus_epoch == self._focus_epoch and
                            self._requested_focus_uri == focused_uri):
                        # Already requested this exact focus/epoch; wait for status confirmation.
                        # If confirmation never arrives, allow a controlled retry.
                        request_age = now - self._requested_focus_since
                        if request_age < 12.0:
                            hold_current_request = True
                            if self._pending_focus_uri != focused_uri:
                                self._pending_focus_uri = focused_uri
                                self._pending_focus_since = now
                            if now - self._last_requested_hold_log > 2.5:
                                logger.warning(
                                    'PLAY hold | waiting status confirmation '
                                    f'age={request_age:.1f}s | focused_uri={focused_uri[:40]} '
                                    f'| epoch={self._focus_epoch} | spotify_ctx={(self.now_playing.context_uri or "none")[:40]} '
                                    f'| spotify_playing={self.now_playing.playing} | loading={self.playback.play_state.is_loading}'
                                )
                                self._last_requested_hold_log = now
                        else:
                            logger.warning(
                                f'PLAY request stale for {request_age:.1f}s, retrying same focus '
                                f'uri={focused_uri[:40]} epoch={self._focus_epoch}'
                            )
                            self._requested_focus_epoch = None
                            self._requested_focus_uri = None
                            self._requested_focus_since = 0.0
                    if not hold_current_request:
                        if self._pending_focus_uri != focused_uri:
                            self._pending_focus_uri = focused_uri
                            self._pending_focus_since = now
                            logger.info(f'Focus stable timer start: {focused_item.name} (1s)')
                        elif now - self._pending_focus_since >= 1.0:
                            logger.info(f'Focus stable 1s -> play request: {focused_item.name}')
                            self._requested_focus_epoch = self._focus_epoch
                            self._requested_focus_uri = focused_uri
                            self._requested_focus_since = now
                            self._play_item(focused_uri)
                            self._reset_pending_focus('request_sent_after_1s_dwell')
        else:
            # Throttled diagnostics: why focus-gate is blocking play requests.
            now = time.time()
            if self.app_screen == AppScreen.SPOTIFY and now - self._last_focus_gate_log > 3.0:
                reason = (
                    f'startup_ready={self._startup_ready}, connected={self.connected}, '
                    f'status_ready={self._status_ready_for_play(now)}, '
                    f'status_unknown={self._status_unknown}, '
                    f'user_activated={self._user_activated_playback}, '
                    f'manual_pause_lock={self._manual_pause_lock}, '
                    f'settled={self.carousel.settled}, dragging={self.touch.dragging}, '
                    f'focused_item={focused_item.name if focused_item else None}, '
                    f'is_temp={focused_item.is_temp if focused_item else None}'
                )
                logger.warning(f'PLAY gate blocked | {reason}')
                self._last_focus_gate_log = now
            elif self._startup_ready and self.connected and not self._status_ready_for_play(now):
                if now - self._last_status_not_ready_log > 3.0:
                    logger.warning(
                        'STATUS not ready for play | '
                        f'last_ok_age={now - self._last_status_ok_at:.1f}s | '
                        f'status_unknown={self._status_unknown} | '
                        f'focused_uri={(focused_item.uri if focused_item else "none")[:40]}'
                    )
                    self._last_status_not_ready_log = now
            keep_pending_feedback = (
                focused_item is not None
                and not focused_item.is_temp
                and not self._is_item_playing(focused_item)
                and not self._is_paused_same_focus_context(focused_item)
                and self._requested_focus_epoch == self._focus_epoch
                and self._requested_focus_uri == focused_item.uri
            )
            if keep_pending_feedback:
                if self._pending_focus_uri != focused_item.uri:
                    self._pending_focus_uri = focused_item.uri
                    self._pending_focus_since = now
                request_age = now - self._requested_focus_since
                if request_age >= 12.0 and not self.playback.play_in_progress:
                    logger.warning(
                        f'Clearing stale requested focus while gated (age={request_age:.1f}s, '
                        f'uri={focused_item.uri[:40]}, epoch={self._focus_epoch})'
                    )
                    self._requested_focus_epoch = None
                    self._requested_focus_uri = None
                    self._requested_focus_since = 0.0
            else:
                self._reset_pending_focus('stable_gate_blocked')

        if self.app_screen == AppScreen.CHECKPOD:
            self._update_checkpod_autoplay(focused_item)
            self._save_local_media_progress_if_due()
            if self.carousel.settled and not self.touch.dragging:
                self._maybe_load_more_checkpod_episodes()
        elif self.app_screen == AppScreen.LOCAL_MUSIC:
            self._update_local_music_autoplay(focused_item)
            self._save_local_media_progress_if_due()
        
        # Check long press for delete mode
        if self.touch.check_long_press():
            self._trigger_delete_mode()
        
        # Update interaction state
        self.user_interacting = (
            self.touch.dragging or 
            not self.carousel.settled or 
            self._pending_focus_uri is not None
        )
        
        self.setup_menu.update()
        self.voice_recorder.tick()
        self.voice_search_recorder.tick()
        self._tick_voice_search()
        self.mokibot_recorder.tick()
        self._tick_mokibot()

        # Volume hold detection: open menu after MENU_HOLD_TIME seconds
        if self._volume_hold_start is not None and not self._menu_hold_triggered:
            if time.time() - self._volume_hold_start >= MENU_HOLD_TIME:
                self._menu_hold_triggered = True
                self._volume_hold_start = None
                self._pressed_button = None
                self.setup_menu.open()
        
        # Keep volume button visually pressed while holding
        if self._volume_hold_start is not None:
            self._pressed_button = 'volume'
            self._pressed_time = time.time()
        
        if self._pressed_button and not self._volume_hold_start and time.time() - self._pressed_time > BUTTON_PRESS_DURATION:
            self._pressed_button = None
            self.renderer.invalidate()
        
        self._sync_to_playing()
        
        if not self._uses_mpv_playback_ui():
            self.playback.update_mock(dt, self.now_playing)
            self.playback.save_progress(self.now_playing)
        
        # Collect playlist covers in background (once per track change)
        # Guard: context_uri comes from WebSocket (instant) but track_cover comes
        # from HTTP /status (can lag). After a context switch, skip collection for
        # 2 seconds so we don't associate the old track's cover with the new playlist.
        np = self.now_playing
        if (np.playing and 'playlist' in (np.context_uri or '')):
            if np.context_uri != self._cover_collect_context:
                self._cover_collect_context = np.context_uri
                self._context_change_time = time.time()
                self._last_cover_collect_key = None
            elif time.time() - self._context_change_time > 2.0:
                track_key = (np.context_uri, np.track_cover)
                if track_key != self._last_cover_collect_key and np.track_cover:
                    self._last_cover_collect_key = track_key
                    run_async(self._collect_cover_async, np.context_uri, np.track_cover)
        else:
            self._cover_collect_context = None
        
        was_awake = not self.sleep_manager.is_sleeping
        menu_open = self.setup_menu.state != MenuState.CLOSED
        voice_active = (
            self.voice_recorder.is_recording
            or self.voice_recorder.is_preparing
            or self.voice_recorder.is_encoding
            or self.voice_search_recorder.is_recording
            or self.voice_search_recorder.is_preparing
            or self.voice_search_recorder.is_encoding
            or self._voice_search_phase != VoiceSearchPhase.CLOSED
            or self.mokibot_recorder.is_recording
            or self.mokibot_recorder.is_preparing
            or self.mokibot_recorder.is_encoding
            or self._mokibot_phase in (
                MokiBotPhase.PREPARING,
                MokiBotPhase.COUNTDOWN,
                MokiBotPhase.RECORDING,
                MokiBotPhase.THINKING,
                MokiBotPhase.SPEAKING,
            )
            or self._is_voice_test_playing()
        )
        playback_active = self._playback_blocks_sleep()
        self.sleep_manager.check_sleep(playback_active or menu_open or voice_active)
        if was_awake and self.sleep_manager.is_sleeping:
            self.bluetooth.pause_monitoring()
            idle = time.time() - self.sleep_manager.last_activity
            self.tracker.on_sleep(idle)
        
        self.playback.update_loading_state(
            self.now_playing, self.carousel.settled, self._pending_focus_uri is not None
        )
        if not self._is_local_media_screen():
            self._check_context_switch_watchdog(focused_item)

        # Root-cause detector: focus is stable and should auto-play, but no request path exists.
        if focused_item is not None and not focused_item.is_temp:
            focused_uri = focused_item.uri
            auto_intent_ready = (
                self.app_screen == AppScreen.SPOTIFY
                and self._startup_ready
                and self.connected
                and self._user_activated_playback
                and not self._manual_pause_lock
                and not self._spotify_launch_lock
                and not self.playback.pause_intent_active
                and self.carousel.settled
                and not self.touch.dragging
            )
            focus_is_playing = self._is_item_playing(focused_item)
            requested_current_focus = (
                self._requested_focus_epoch == self._focus_epoch
                and self._requested_focus_uri == focused_uri
            )
            has_active_play_path = (
                self.playback.play_in_progress
                or self._pending_focus_uri == focused_uri
                or requested_current_focus
            )
            if (
                auto_intent_ready
                and not focus_is_playing
                and not has_active_play_path
                and not self._is_paused_same_focus_context(focused_item)
            ):
                now = time.time()
                if self._autoplay_stall_since <= 0.0:
                    self._autoplay_stall_since = now
                stall_age = now - self._autoplay_stall_since
                if stall_age >= 1.5 and now - self._last_autoplay_stall_log > 2.0:
                    logger.warning(
                        'AUTOPLAY stall | focus stable but no active play path | '
                        f'stall_age={stall_age:.1f}s | focused="{focused_item.name}" | '
                        f'focused_uri={focused_uri[:40]} | spotify_ctx={(self.now_playing.context_uri or "none")[:40]} | '
                        f'spotify_playing={self.now_playing.playing} | spotify_paused={self.now_playing.paused} | '
                        f'loading={self.playback.play_state.is_loading} | play_in_progress={self.playback.play_in_progress} | '
                        f'pending_focus={(self._pending_focus_uri or "none")[:40]} | '
                        f'requested_uri={(self._requested_focus_uri or "none")[:40]} | '
                        f'requested_epoch={self._requested_focus_epoch} | focus_epoch={self._focus_epoch}'
                    )
                    self._last_autoplay_stall_log = now
            else:
                self._autoplay_stall_since = 0.0

        # Detect "should be loading but loader disappeared" condition.
        if focused_item is not None and not focused_item.is_temp:
            expected_loading = (
                not self._is_item_playing(focused_item)
                and (
                    self.playback.play_in_progress
                    or self._pending_focus_uri == focused_item.uri
                    or (
                        self._requested_focus_epoch == self._focus_epoch
                        and self._requested_focus_uri == focused_item.uri
                    )
                )
            )
            now = time.time()
            if (
                expected_loading
                and not self.playback.play_state.is_loading
                and now - self._last_loader_mismatch_log > 5.0
            ):
                self._last_loader_mismatch_log = now
                logger.warning(
                    'LOADER mismatch | expected_loading=True but is_loading=False | '
                    f'focused_uri={focused_item.uri[:40]} | pending_uri={(self._pending_focus_uri or "none")[:40]} | '
                    f'requested_uri={(self._requested_focus_uri or "none")[:40]} | '
                    f'play_in_progress={self.playback.play_in_progress} | epoch={self._focus_epoch}'
                )

        # Diagnostics for "title above context is wrong while loading/resume"
        # Keep throttled to avoid log spam.
        now = time.time()
        if (
            self.app_screen == AppScreen.SPOTIFY
            and now - self._last_title_diag_log > 2.0
            and focused_item is not None
        ):
            title_source, title_text = self._display_title_for_item(focused_item)
            logger.warning(
                'TITLE diag | '
                f'focused="{focused_item.name}" | title="{title_text}" | source={title_source} | '
                f'focused_uri={(focused_item.uri or "none")[:40]} | spotify_ctx={(self.now_playing.context_uri or "none")[:40]} | '
                f'spotify_track="{self.now_playing.track_name or "none"}" | loading={self.playback.play_state.is_loading} | '
                f'play_in_progress={self.playback.play_in_progress} | pending_focus={(self._pending_focus_uri or "none")[:40]} | '
                f'requested_uri={(self._requested_focus_uri or "none")[:40]} | requested_epoch={self._requested_focus_epoch}'
            )
            if not (self.now_playing.playing or self.now_playing.paused) and title_source != 'none':
                logger.warning(
                    'TITLE mismatch | expected_source=none while inactive | '
                    f'actual_source={title_source} | focused="{focused_item.name}" | title="{title_text}"'
                )
            self._last_title_diag_log = now
    
    # ============================================
    # SETUP MENU
    # ============================================
    
    def _handle_button_up(self):
        """Handle MOUSEBUTTONUP for volume hold: short tap → toggle, long hold → already opened menu."""
        if self._volume_hold_start is None:
            return
        if self._menu_hold_triggered:
            self._volume_hold_start = None
            self._menu_hold_triggered = False
            return
        # Short tap: toggle volume
        self.volume.toggle()
        # Also set BT sink volume when BT audio is active
        if self._bt_audio_active:
            self.bluetooth.set_volume(self.volume.bt_level)
        self._last_action_time = time.time()
        self._volume_hold_start = None
    
    
    def _draw(self):
        """Draw the UI."""
        items = self._display_items()
        np = self.now_playing
        focused_item = items[self.selected_index] if self.selected_index < len(items) else None
        focused_uri = focused_item.uri if focused_item else None
        if self._uses_mpv_playback_ui():
            playing, paused, _, _, context_uri, _ = self.local_playback.get_state()
            focused_context_playing = bool(
                focused_item and playing and context_uri == focused_uri
            )
            if self.app_screen == AppScreen.CHECKPOD:
                is_loading = self._checkpod_play_in_progress
                play_in_progress = self._checkpod_play_in_progress
                pending_focus_uri = self._checkpod_pending_focus_uri
            elif self.app_screen == AppScreen.LOCAL_MUSIC:
                is_loading = self._local_music_play_in_progress
                play_in_progress = self._local_music_play_in_progress
                pending_focus_uri = self._local_music_pending_focus_uri
            else:
                is_loading = self._radio_play_in_progress
                play_in_progress = self._radio_play_in_progress
                pending_focus_uri = None
            is_playing = bool(playing and context_uri == focused_uri)
            requested_focus_uri = None
        else:
            focused_context_playing = bool(
                focused_item
                and np.playing
                and np.context_uri == focused_uri
            )
            is_loading = self.playback.play_state.is_loading and not focused_context_playing
            is_playing = self.playback.play_state.display_playing(np.playing)
            play_in_progress = self.playback.play_in_progress
            pending_focus_uri = self._pending_focus_uri
            requested_focus_uri = self._requested_focus_uri

        recent_focus_commit = bool(
            focused_uri
            and self._last_play_commit_uri == focused_uri
            and (time.time() - self._last_play_commit_at) < 1.25
        )
        if self._uses_mpv_playback_ui():
            recent_focus_commit = False
            is_loading = is_loading and not focused_context_playing
        else:
            is_loading = is_loading and not (focused_context_playing or recent_focus_commit)

        # Snapshot BT state once to avoid race with monitor thread
        bt_dev = self.bluetooth.connected_device

        if self._voice_search_phase == VoiceSearchPhase.RESULTS:
            voice_dragging = self._voice_search_touch_active and self.touch.dragging
            voice_drag_offset = self.touch.drag_offset if self._voice_search_touch_active else 0.0
        else:
            voice_dragging = False
            voice_drag_offset = 0.0

        ctx = RenderContext(
            items=items,
            selected_index=self.selected_index,
            now_playing=np,
            scroll_x=self.carousel.scroll_x,
            drag_offset=voice_drag_offset if self._voice_search_phase == VoiceSearchPhase.RESULTS else self.touch.drag_offset,
            dragging=voice_dragging if self._voice_search_phase == VoiceSearchPhase.RESULTS else self.touch.dragging,
            is_sleeping=self.sleep_manager.is_sleeping,
            volume_index=self.volume.index,
            delete_mode_id=self.delete_mode_id,
            pressed_button=self._pressed_button,
            is_loading=is_loading,
            is_playing=is_playing,
            pending_focus_uri=pending_focus_uri,
            requested_focus_uri=requested_focus_uri,
            play_in_progress=play_in_progress,
            toast_message=self._active_toast,
            menu_state=self.setup_menu.state,
            menu_known_networks=self.setup_menu.known_networks,
            menu_current_network=self.setup_menu.current_network,
            menu_wifi_now_band=self.setup_menu.wifi_now_band,
            menu_wifi_link_detail=self.setup_menu.wifi_link_detail,
            menu_wifi_band_label=self.setup_menu.wifi_band_label,
            auto_pause_minutes=self.settings.auto_pause_minutes,
            progress_expiry_hours=self.settings.progress_expiry_hours,
            app_version_label=self.app_version_label,
            bt_connected=bt_dev is not None,
            bt_audio_active=self._bt_audio_active,
            bt_connected_name=bt_dev.name if bt_dev else None,
            bt_paired_devices=self.bluetooth.paired_devices,
            bt_discovered_devices=self.bluetooth.discovered_devices,
            bt_scanning=self.bluetooth.scanning,
            bt_pairing_mac=self.bluetooth.pairing_mac,
            volume_levels=self.settings.get_volume_levels(),
            menu_scroll_offset=self.setup_menu.scroll_offset,
            update_checking=self.setup_menu._update_checking,
            update_available=self.setup_menu._update_available,
            update_running=self.setup_menu._update_running,
            reset_confirm_pending=self.setup_menu._reset_confirm_pending,
            shutdown_confirm_pending=self.setup_menu._shutdown_confirm_pending,
            reboot_confirm_pending=self.setup_menu._reboot_confirm_pending,
            has_network=self._get_cached_network_status(),
            app_screen=self.app_screen,
            home_page_scroll=self.home_pager.scroll_x,
            home_page_index=int(round(self.home_pager.scroll_x)),
            home_drag_offset=self._home_touch.drag_offset if self._home_touch_active else 0.0,
            home_dragging=self._home_touch_active and self._home_touch.dragging,
            pin_buffer=self.setup_menu.pin_buffer,
            change_pin_step=self.setup_menu.change_pin_step,
            voice_recording=self.voice_recorder.is_recording,
            voice_preparing=self.voice_recorder.is_preparing,
            voice_encoding=self.voice_recorder.is_encoding,
            voice_recording_elapsed=self.voice_recorder.recording_elapsed,
            voice_has_recording=self.voice_recorder.has_recording(),
            voice_playing=self._is_voice_test_playing(),
            voice_transcript=self._voice_transcript,
            voice_transcribing=self._voice_transcribing,
            voice_transcribe_error=self._voice_transcribe_error,
            search_query=self._search_query,
            search_results=self._search_results,
            search_loading=self._search_loading,
            search_error=self._search_error,
            voice_search_phase=self._voice_search_phase,
            voice_search_query=self._voice_search_query,
            voice_search_results=self._voice_search_results,
            voice_search_scroll_x=self.voice_search_carousel.scroll_x,
            voice_search_selected_index=self._voice_search_selected_index,
            voice_search_recording=self.voice_search_recorder.is_recording,
            voice_search_preparing=self.voice_search_recorder.is_preparing,
            voice_search_elapsed=self.voice_search_recorder.recording_elapsed,
            voice_search_countdown_label=self._voice_search_countdown_label(),
            mokibot_phase=self._mokibot_phase,
            mokibot_status_text=self._mokibot_status_text(),
            mokibot_reply_text=self._mokibot_reply_text,
            mokibot_recording=self.mokibot_recorder.is_recording,
            mokibot_preparing=self.mokibot_recorder.is_preparing,
            mokibot_elapsed=self.mokibot_recorder.recording_elapsed,
            mokibot_countdown_label=self._mokibot_countdown_label(),
            mokibot_play_name=self._mokibot_play_name,
        )
        dirty_rects = self.renderer.draw(ctx)
        if self.delete_mode_id and self.renderer.delete_button_rect:
            self._delete_button_rect = self.renderer.delete_button_rect
        elif not self.delete_mode_id:
            self._delete_button_rect = None
        return dirty_rects
