"""
Render Context - Bundles all state needed for rendering.
"""
from dataclasses import dataclass, field
from typing import Optional, List

from ..models import CatalogItem, MenuState, NowPlaying, AppScreen, VoiceSearchPhase, MokiBotPhase, SearchResult
from ..managers.bluetooth import BluetoothDevice


@dataclass
class RenderContext:
    """All state needed to render a frame."""
    items: List[CatalogItem]
    selected_index: int
    now_playing: NowPlaying
    scroll_x: float
    drag_offset: float
    dragging: bool
    is_sleeping: bool
    volume_index: int
    delete_mode_id: Optional[str]
    pressed_button: Optional[str]
    is_loading: bool
    is_playing: bool  # What to show for play/pause button
    pending_focus_uri: Optional[str] = None
    requested_focus_uri: Optional[str] = None
    play_in_progress: bool = False
    toast_message: Optional[str] = None
    menu_state: MenuState = MenuState.CLOSED
    menu_known_networks: List[str] = field(default_factory=list)
    menu_current_network: Optional[str] = None
    menu_wifi_now_band: str = ''
    menu_wifi_link_detail: str = ''
    menu_wifi_band_label: str = ''
    auto_pause_minutes: int = 30
    progress_expiry_hours: int = 96
    app_version_label: str = ''
    bt_connected: bool = False          # A BT audio device is connected
    bt_audio_active: bool = False       # Audio is routed to BT (headphone icon purple)
    bt_connected_name: Optional[str] = None
    bt_paired_devices: List[BluetoothDevice] = field(default_factory=list)
    bt_discovered_devices: List[BluetoothDevice] = field(default_factory=list)
    bt_scanning: bool = False
    bt_pairing_mac: Optional[str] = None
    volume_levels: list = field(default_factory=list)  # For volume settings screen
    menu_scroll_offset: int = 0
    reset_confirm_pending: bool = False
    shutdown_confirm_pending: bool = False
    reboot_confirm_pending: bool = False
    update_checking: bool = False
    update_available: bool = False
    update_running: bool = False
    has_network: bool = True
    app_screen: AppScreen = AppScreen.HOME
    home_page_scroll: float = 0.0
    home_page_index: int = 0
    home_drag_offset: float = 0.0
    home_dragging: bool = False
    pin_buffer: str = ''
    change_pin_step: int = 0
    voice_recording: bool = False
    voice_preparing: bool = False
    voice_encoding: bool = False
    voice_recording_elapsed: int = 0
    voice_has_recording: bool = False
    voice_playing: bool = False
    voice_transcript: Optional[str] = None
    voice_transcribing: bool = False
    voice_transcribe_error: Optional[str] = None
    search_query: str = ''
    search_results: List[SearchResult] = field(default_factory=list)
    search_loading: bool = False
    search_error: Optional[str] = None
    voice_search_phase: VoiceSearchPhase = VoiceSearchPhase.CLOSED
    voice_search_query: str = ''
    voice_search_results: List[SearchResult] = field(default_factory=list)
    voice_search_scroll_x: float = 0.0
    voice_search_selected_index: int = 0
    voice_search_recording: bool = False
    voice_search_preparing: bool = False
    voice_search_elapsed: int = 0
    voice_search_countdown_label: str = ''
    mokibot_phase: MokiBotPhase = MokiBotPhase.IDLE
    mokibot_status_text: str = ''
    mokibot_reply_text: str = ''
    mokibot_recording: bool = False
    mokibot_preparing: bool = False
    mokibot_elapsed: int = 0
    mokibot_countdown_label: str = ''
    mokibot_play_name: Optional[str] = None
    checkpod_refreshing: bool = False

