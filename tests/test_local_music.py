"""
Tests for local MP3 player integration.
"""
import json
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

from mello.app import Mello
from mello.models import AppScreen, CatalogItem, NowPlaying
from mello.managers.local_music_manager import LocalMusicManager, _uri_for_relative_path


def _write_fake_mp3(path: Path, title: str = 'Test Song', artist: str = 'Test Artist'):
    """Create a minimal valid MP3 file for mutagen."""
    from mutagen.id3 import ID3, TIT2, TPE1
    from mutagen.mp3 import MP3

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'\x00' * 128)
    audio = MP3(path)
    audio.tags = ID3()
    audio.tags.add(TIT2(encoding=3, text=title))
    audio.tags.add(TPE1(encoding=3, text=artist))
    audio.save()


def test_scan_finds_mp3_files(tmp_path, monkeypatch):
    music_dir = tmp_path / 'local_music'
    music_dir.mkdir()
    mp3 = music_dir / 'album' / 'song.mp3'
    mp3.parent.mkdir(parents=True)
    mp3.write_bytes(b'fake')

    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_DIR', music_dir)
    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_CATALOG_PATH', music_dir / 'catalog.json')
    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_PROGRESS_PATH', music_dir / 'progress.json')
    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_IMAGES_DIR', music_dir / 'images')

    manager = LocalMusicManager()
    with patch.object(
        manager,
        '_read_media_metadata',
        return_value={'title': 'Mein Song', 'artist': 'Mein Künstler', 'duration_ms': 180000, 'cover_bytes': None},
    ):
        assert manager.refresh_catalog() is True
    items = manager.get_display_items()
    assert len(items) == 1
    assert items[0].name == 'Mein Song'
    assert items[0].artist == 'Mein Künstler'
    assert items[0].uri == _uri_for_relative_path('album/song.mp3')
    assert manager.get_media_path(items[0]) == mp3


def test_scan_finds_m4b_files(tmp_path, monkeypatch):
    music_dir = tmp_path / 'local_music'
    music_dir.mkdir()
    m4b = music_dir / 'hoerbuch.m4b'
    m4b.write_bytes(b'fake')

    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_DIR', music_dir)
    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_CATALOG_PATH', music_dir / 'catalog.json')
    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_PROGRESS_PATH', music_dir / 'progress.json')
    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_IMAGES_DIR', music_dir / 'images')

    manager = LocalMusicManager()
    with patch.object(
        manager,
        '_read_m4b_metadata',
        return_value={'title': 'Mein Hörbuch', 'artist': 'Autor', 'duration_ms': 3600000, 'cover_bytes': None},
    ):
        assert manager.refresh_catalog() is True
    items = manager.get_display_items()
    assert len(items) == 1
    assert items[0].name == 'Mein Hörbuch'
    assert items[0].uri == _uri_for_relative_path('hoerbuch.m4b')
    assert manager.get_media_path(items[0]) == m4b


def test_progress_rejects_regression(tmp_path, monkeypatch):
    music_dir = tmp_path / 'local_music'
    music_dir.mkdir(parents=True)
    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_DIR', music_dir)
    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_CATALOG_PATH', music_dir / 'catalog.json')
    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_PROGRESS_PATH', music_dir / 'progress.json')
    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_IMAGES_DIR', music_dir / 'images')

    manager = LocalMusicManager()
    uri = 'local:music:album/song.mp3'
    manager.save_progress(uri, 120000, 300000, 'Song', force=True)
    assert manager.save_progress(uri, 60000, 300000, 'Song', force=False) is False
    assert manager.get_progress(uri)['position'] == 120000


def test_get_last_played_uri(tmp_path, monkeypatch):
    music_dir = tmp_path / 'local_music'
    music_dir.mkdir(parents=True)
    progress_path = music_dir / 'progress.json'
    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_DIR', music_dir)
    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_CATALOG_PATH', music_dir / 'catalog.json')
    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_PROGRESS_PATH', progress_path)
    monkeypatch.setattr('mello.managers.local_music_manager.LOCAL_MUSIC_IMAGES_DIR', music_dir / 'images')

    manager = LocalMusicManager()
    manager.save_progress('local:music:a.mp3', 10000, 200000, 'A', force=True)
    manager.save_progress('local:music:b.mp3', 20000, 200000, 'B', force=True)
    assert manager.get_last_played_uri() == 'local:music:b.mp3'


def test_open_local_music_screen_sets_launch_lock():
    app = Mello.__new__(Mello)
    app.app_screen = AppScreen.HOME
    app._local_music_launch_lock = False
    app._spotify_launch_lock = False
    app._local_music_play_in_progress = False
    app._local_music_pending_focus_uri = 'x'
    app._local_music_pending_focus_since = 1.0
    app._pressed_button = 'x'
    app.renderer = SimpleNamespace(invalidate=MagicMock())
    app._pause_active_playback = MagicMock()
    app._set_manual_pause_lock = MagicMock()
    app._reset_checkpod_screen_state = MagicMock()
    app._restore_local_media_carousel_focus = MagicMock()
    app._update_carousel_max_index = MagicMock()
    app.local_playback = SimpleNamespace(warm_up=MagicMock())

    with patch('mello.app.run_async') as mock_async:
        Mello._open_local_music_screen(app)

    assert app.app_screen == AppScreen.LOCAL_MUSIC
    assert app._local_music_launch_lock is True
    app._pause_active_playback.assert_called_once_with('local_music_open')
    mock_async.assert_called_once_with(app._refresh_local_music_catalog)


def test_restart_local_media_episode_clears_progress_and_plays_from_start():
    app = Mello.__new__(Mello)
    app.app_screen = AppScreen.LOCAL_MUSIC
    item = CatalogItem(id='1', uri='local:music:song.mp3', name='Song', artist='Artist', images=[])
    app.selected_index = 0
    app._display_items = lambda: [item]
    app.local_playback = SimpleNamespace(is_active=True, stop=MagicMock())
    app.local_music_manager = SimpleNamespace(clear_progress=MagicMock())
    app._local_music_pending_focus_uri = 'x'
    app._local_music_pending_focus_since = 1.0
    app._local_music_play_failed_uri = 'x'
    app._local_music_play_failed_at = 1.0
    app._clear_manual_pause_lock = MagicMock()
    app._play_local_media_item = MagicMock()
    app._local_media_manager = lambda: app.local_music_manager

    Mello._restart_local_media_episode(app)

    app.local_playback.stop.assert_called_once_with(save_progress=False)
    app.local_music_manager.clear_progress.assert_called_once_with('local:music:song.mp3')
    app._play_local_media_item.assert_called_once_with(item, from_beginning=True)


def test_seek_local_media_saves_progress():
    app = Mello.__new__(Mello)
    app.local_playback = SimpleNamespace(
        get_state=lambda: (True, False, 90000, 200000, 'local:music:song.mp3', 'Song'),
        seek_relative=lambda _delta: True,
    )
    app.local_music_manager = SimpleNamespace(save_progress=MagicMock())
    app._manager_for_context_uri = lambda uri: app.local_music_manager
    app.renderer = SimpleNamespace(invalidate=MagicMock())

    Mello._seek_local_media(app, 30)

    app.local_music_manager.save_progress.assert_called_once_with(
        'local:music:song.mp3', 90000, 200000, 'Song', force=True
    )


def test_is_local_media_screen():
    app = Mello.__new__(Mello)
    app.app_screen = AppScreen.LOCAL_MUSIC
    assert Mello._is_local_media_screen(app) is True
    app.app_screen = AppScreen.CHECKPOD
    assert Mello._is_local_media_screen(app) is True
    app.app_screen = AppScreen.SPOTIFY
    assert Mello._is_local_media_screen(app) is False
