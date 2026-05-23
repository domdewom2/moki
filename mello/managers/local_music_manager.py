"""
Local music manager — scan on-disk audio library, covers, and playback progress.
"""
import hashlib
import json
import logging
import os
import threading
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Callable, List, Optional, Set

from PIL import Image
from mutagen import MutagenError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from ..api.catalog import apply_dimming, apply_rounded_corners_pil
from ..config import (
    COLORS,
    COVER_SIZE,
    COVER_SIZE_SMALL,
    LOCAL_MUSIC_CATALOG_PATH,
    LOCAL_MUSIC_DIR,
    LOCAL_MUSIC_IMAGES_DIR,
    LOCAL_MUSIC_IMAGE_PATH_PREFIX,
    LOCAL_MUSIC_PROGRESS_PATH,
    PROGRESS_EXPIRY_HOURS,
)
from ..models import CatalogItem
from .checkpod_manager import _fit_cover_to_square

logger = logging.getLogger(__name__)

LOCAL_MUSIC_IMAGE_VERSION = 2
URI_PREFIX = 'local:music:'
LOCAL_MUSIC_EXTENSIONS: Set[str] = {'.mp3', '.m4b'}


def _track_id_for_relative_path(relative_path: str) -> str:
    return hashlib.sha256(relative_path.encode('utf-8')).hexdigest()[:16]


def _uri_for_relative_path(relative_path: str) -> str:
    return f'{URI_PREFIX}{relative_path}'


def _relative_path_for_uri(uri: str) -> Optional[str]:
    if uri.startswith(URI_PREFIX):
        return uri[len(URI_PREFIX):]
    return None


class LocalMusicManager:
    """Owns the on-device local audio catalog and playback progress."""

    def __init__(
        self,
        on_toast: Optional[Callable[[str], None]] = None,
        on_invalidate: Optional[Callable[[], None]] = None,
        get_progress_expiry: Optional[Callable[[], int]] = None,
    ):
        self._on_toast = on_toast or (lambda msg: None)
        self._on_invalidate = on_invalidate or (lambda: None)
        self._get_progress_expiry = get_progress_expiry or (lambda: PROGRESS_EXPIRY_HOURS)
        self._lock = threading.Lock()
        self._progress_lock = threading.Lock()
        self._items: List[CatalogItem] = []
        self._track_paths: dict[str, Path] = {}
        self._refreshing = False

        LOCAL_MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        LOCAL_MUSIC_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        self._load_catalog_from_disk()

    @property
    def items(self) -> List[CatalogItem]:
        with self._lock:
            return list(self._items)

    def get_display_items(self) -> List[CatalogItem]:
        return self.items

    def get_media_path(self, item: CatalogItem) -> Optional[Path]:
        with self._lock:
            path = self._track_paths.get(item.id)
        if path and path.exists():
            return path
        candidate = LOCAL_MUSIC_DIR / _relative_path_for_uri(item.uri or '')
        if candidate.exists():
            return candidate
        return None

    def refresh_catalog(self) -> bool:
        """Rescan LOCAL_MUSIC_DIR for supported audio files and rebuild catalog."""
        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True
        try:
            entries = self._scan_media_files()
            force_images = self._should_regenerate_images()
            items: List[CatalogItem] = []
            track_paths: dict[str, Path] = {}
            catalog_entries = []

            for rel_path, abs_path in entries:
                try:
                    meta = self._read_media_metadata(abs_path, rel_path)
                except (OSError, ValueError, TypeError, MutagenError) as e:
                    logger.warning(f'Skipping local media file {rel_path}: {e}')
                    continue
                track_id = _track_id_for_relative_path(rel_path)
                uri = _uri_for_relative_path(rel_path)
                image_path = self._ensure_track_image(track_id, meta.get('cover_bytes'), force=force_images)
                item = CatalogItem(
                    id=track_id,
                    uri=uri,
                    name=meta['title'],
                    type='track',
                    artist=meta['artist'],
                    image=image_path,
                )
                items.append(item)
                track_paths[track_id] = abs_path
                catalog_entries.append({
                    'id': track_id,
                    'uri': uri,
                    'relative_path': rel_path,
                    'title': meta['title'],
                    'artist': meta['artist'],
                    'duration_ms': meta['duration_ms'],
                    'image': image_path,
                })

            catalog = {
                'updated_at': datetime.now().isoformat(),
                'image_version': LOCAL_MUSIC_IMAGE_VERSION,
                'tracks': catalog_entries,
            }
            self._save_catalog(catalog)
            with self._lock:
                self._items = items
                self._track_paths = track_paths
            self._on_invalidate()
            logger.info(f'Local music catalog refreshed ({len(items)} tracks)')
            return True
        finally:
            with self._lock:
                self._refreshing = False

    def _scan_media_files(self) -> List[tuple[str, Path]]:
        found: List[tuple[str, Path]] = []
        if not LOCAL_MUSIC_DIR.exists():
            return found
        for path in sorted(LOCAL_MUSIC_DIR.rglob('*')):
            if not path.is_file():
                continue
            if path.suffix.lower() not in LOCAL_MUSIC_EXTENSIONS:
                continue
            rel = path.relative_to(LOCAL_MUSIC_DIR).as_posix()
            if rel.startswith('images/') or rel in ('catalog.json', 'progress.json'):
                continue
            found.append((rel, path))
        return found

    def _read_media_metadata(self, path: Path, rel_path: str) -> dict:
        suffix = path.suffix.lower()
        if suffix == '.mp3':
            return self._read_mp3_metadata(path, rel_path)
        if suffix == '.m4b':
            return self._read_m4b_metadata(path, rel_path)
        return {
            'title': path.stem.replace('_', ' ') or path.stem,
            'artist': 'Unbekannt',
            'duration_ms': 0,
            'cover_bytes': None,
        }

    def _read_mp3_metadata(self, path: Path, rel_path: str) -> dict:
        title = path.stem.replace('_', ' ')
        artist = 'Unbekannt'
        duration_ms = 0
        cover_bytes = None
        try:
            audio = MP3(path)
            if audio.info and audio.info.length:
                duration_ms = int(audio.info.length * 1000)
            tags = audio.tags
            if tags:
                if tags.get('TIT2'):
                    title = str(tags.get('TIT2'))
                if tags.get('TPE1'):
                    artist = str(tags.get('TPE1'))
                for key in tags.keys():
                    if key.startswith('APIC'):
                        cover_bytes = tags[key].data
                        break
        except (OSError, ValueError, TypeError, MutagenError) as e:
            logger.warning(f'Failed to read MP3 metadata for {rel_path}: {e}')
        return {
            'title': title or path.stem,
            'artist': artist,
            'duration_ms': duration_ms,
            'cover_bytes': cover_bytes,
        }

    def _read_m4b_metadata(self, path: Path, rel_path: str) -> dict:
        title = path.stem.replace('_', ' ')
        artist = 'Unbekannt'
        duration_ms = 0
        cover_bytes = None
        try:
            audio = MP4(path)
            if audio.info and audio.info.length:
                duration_ms = int(audio.info.length * 1000)
            tags = audio.tags
            if tags:
                if tags.get('\xa9nam'):
                    title = str(tags.get('\xa9nam')[0])
                if tags.get('\xa9ART'):
                    artist = str(tags.get('\xa9ART')[0])
                elif tags.get('aART'):
                    artist = str(tags.get('aART')[0])
                covers = tags.get('covr')
                if covers:
                    cover_bytes = bytes(covers[0])
        except (OSError, ValueError, TypeError, MutagenError) as e:
            logger.warning(f'Failed to read M4B metadata for {rel_path}: {e}')
        return {
            'title': title or path.stem,
            'artist': artist,
            'duration_ms': duration_ms,
            'cover_bytes': cover_bytes,
        }

    def _image_path_exists(self, image_path: Optional[str]) -> bool:
        if not image_path or not image_path.startswith(LOCAL_MUSIC_IMAGE_PATH_PREFIX):
            return bool(image_path)
        base = image_path.replace(LOCAL_MUSIC_IMAGE_PATH_PREFIX, '').replace('.png', '')
        return (LOCAL_MUSIC_IMAGES_DIR / f'{base}.png').exists()

    def _load_catalog_from_disk(self):
        if not LOCAL_MUSIC_CATALOG_PATH.exists():
            return
        try:
            data = json.loads(LOCAL_MUSIC_CATALOG_PATH.read_text())
            tracks = data.get('tracks') or []
            items = []
            track_paths = {}
            for entry in tracks:
                if not isinstance(entry, dict):
                    continue
                track_id = str(entry.get('id') or '')
                rel_path = entry.get('relative_path') or _relative_path_for_uri(entry.get('uri') or '')
                uri = entry.get('uri') or _uri_for_relative_path(rel_path)
                title = entry.get('title') or 'Track'
                artist = entry.get('artist') or 'Unbekannt'
                image = entry.get('image')
                if image and not self._image_path_exists(image):
                    image = None
                abs_path = LOCAL_MUSIC_DIR / rel_path
                if track_id and abs_path.exists():
                    items.append(CatalogItem(
                        id=track_id,
                        uri=uri,
                        name=title,
                        type='track',
                        artist=artist,
                        image=image,
                    ))
                    track_paths[track_id] = abs_path
            with self._lock:
                self._items = items
                self._track_paths = track_paths
            logger.info(f'Local music catalog loaded from disk ({len(items)} tracks)')
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f'Failed to load local music catalog: {e}', exc_info=True)

    def _save_catalog(self, data: dict):
        temp = LOCAL_MUSIC_CATALOG_PATH.with_suffix('.json.tmp')
        temp.write_text(json.dumps(data, indent=2))
        os.replace(temp, LOCAL_MUSIC_CATALOG_PATH)

    def _should_regenerate_images(self) -> bool:
        if not LOCAL_MUSIC_CATALOG_PATH.exists():
            return True
        try:
            data = json.loads(LOCAL_MUSIC_CATALOG_PATH.read_text())
            return int(data.get('image_version', 1)) < LOCAL_MUSIC_IMAGE_VERSION
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return True

    def _delete_track_images(self, base_name: str):
        for suffix in ('', '_small', '_dim', '_small_dim'):
            path = LOCAL_MUSIC_IMAGES_DIR / f'{base_name}{suffix}.png'
            if path.exists():
                path.unlink()

    def _ensure_track_image(
        self,
        track_id: str,
        cover_bytes: Optional[bytes],
        force: bool = False,
    ) -> Optional[str]:
        if force:
            self._delete_track_images(track_id)
        main_path = LOCAL_MUSIC_IMAGES_DIR / f'{track_id}.png'
        if main_path.exists():
            return f'{LOCAL_MUSIC_IMAGE_PATH_PREFIX}{track_id}.png'
        if not cover_bytes:
            return None
        try:
            img = Image.open(BytesIO(cover_bytes)).convert('RGBA')
            sizes = [
                (COVER_SIZE, ''),
                (COVER_SIZE_SMALL, '_small'),
            ]
            for size, suffix in sizes:
                fitted = _fit_cover_to_square(img, size)
                fitted = fitted.transpose(Image.Transpose.ROTATE_270)
                radius = max(12, size // 25)
                processed = apply_rounded_corners_pil(fitted, radius)
                processed.save(LOCAL_MUSIC_IMAGES_DIR / f'{track_id}{suffix}.png', 'PNG')
                dimmed = apply_dimming(processed)
                dimmed.save(LOCAL_MUSIC_IMAGES_DIR / f'{track_id}{suffix}_dim.png', 'PNG')
            return f'{LOCAL_MUSIC_IMAGE_PATH_PREFIX}{track_id}.png'
        except (OSError, ValueError) as e:
            logger.warning(f'Failed to process cover for track {track_id}: {e}')
            return None

    def _load_progress_data(self) -> dict:
        with self._progress_lock:
            try:
                if LOCAL_MUSIC_PROGRESS_PATH.exists():
                    return json.loads(LOCAL_MUSIC_PROGRESS_PATH.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f'Error reading local music progress: {e}')
            return {}

    def _save_progress_data(self, data: dict):
        with self._progress_lock:
            temp = LOCAL_MUSIC_PROGRESS_PATH.with_suffix('.json.tmp')
            temp.write_text(json.dumps(data, indent=2))
            os.replace(temp, LOCAL_MUSIC_PROGRESS_PATH)

    def get_progress(self, context_uri: str) -> Optional[dict]:
        try:
            entry = self._load_progress_data().get(context_uri)
            if not entry:
                return None
            updated_at = entry.get('updatedAt')
            if updated_at:
                updated = datetime.fromisoformat(updated_at)
                age_hours = (datetime.now() - updated).total_seconds() / 3600
                if age_hours > self._get_progress_expiry():
                    self.clear_progress(context_uri)
                    return None
            return entry
        except (ValueError, TypeError) as e:
            logger.warning(f'Error reading local music progress: {e}')
            return None

    def get_last_played_uri(self, fallback_uri: Optional[str] = None) -> Optional[str]:
        try:
            data = self._load_progress_data()
            if not data:
                return fallback_uri

            best_uri = None
            best_time = None
            for uri, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                position = max(0, int(entry.get('position') or 0))
                duration = max(0, int(entry.get('duration') or 0))
                if duration > 0 and position >= duration * 0.95:
                    continue
                updated_at = entry.get('updatedAt')
                if not updated_at:
                    continue
                try:
                    updated = datetime.fromisoformat(updated_at)
                except (ValueError, TypeError):
                    continue
                if best_time is None or updated > best_time:
                    best_time = updated
                    best_uri = uri

            return best_uri or fallback_uri
        except (ValueError, TypeError, OSError) as e:
            logger.warning(f'Error reading last played local music URI: {e}')
            return fallback_uri

    def save_progress(
        self,
        context_uri: str,
        position_ms: int,
        duration_ms: int,
        name: str = '',
        force: bool = False,
    ) -> bool:
        if not context_uri or position_ms < 0:
            return False
        try:
            position_ms = max(0, int(position_ms or 0))
            duration_ms = max(0, int(duration_ms or 0))
            data = self._load_progress_data()
            existing = data.get(context_uri)
            if not force and isinstance(existing, dict):
                existing_position = max(0, int(existing.get('position', 0) or 0))
                if position_ms >= existing_position:
                    pass
                elif position_ms <= 3000 and existing_position > 60000:
                    logger.info(
                        'Local music progress_write_rejected | reason=stale_zero | '
                        f'uri={context_uri[:40]} | '
                        f'old_pos={existing_position // 1000}s | new_pos={position_ms // 1000}s'
                    )
                    return False
                elif existing_position - position_ms > 2000:
                    logger.info(
                        'Local music progress_write_rejected | reason=position_regression | '
                        f'uri={context_uri[:40]} | '
                        f'old_pos={existing_position // 1000}s | new_pos={position_ms // 1000}s'
                    )
                    return False

            entry = {
                'uri': context_uri,
                'position': position_ms,
                'duration': duration_ms,
                'name': name,
                'updatedAt': datetime.now().isoformat(),
            }
            data[context_uri] = entry
            self._save_progress_data(data)
            logger.info(f'Local music progress saved: {name} @ {position_ms // 1000}s')
            return True
        except OSError as e:
            logger.warning(f'Error saving local music progress: {e}', exc_info=True)
            return False

    def clear_progress(self, context_uri: str):
        try:
            data = self._load_progress_data()
            if context_uri in data:
                del data[context_uri]
                self._save_progress_data(data)
        except OSError as e:
            logger.warning(f'Error clearing local music progress: {e}')
