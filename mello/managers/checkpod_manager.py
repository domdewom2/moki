"""
CheckPod manager — ARD episode catalog, MP3 cache, and progress.
"""
import json
import logging
import os
import threading
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Callable, List, Optional

import requests
from PIL import Image

from ..api.ard_audiothek import ArdEpisode, fetch_episodes
from ..api.catalog import apply_dimming, apply_rounded_corners_pil
from ..config import (
    CHECKPOD_CACHE_DIR,
    CHECKPOD_CATALOG_PATH,
    CHECKPOD_DOWNLOAD_RETENTION_DAYS,
    CHECKPOD_IMAGES_DIR,
    CHECKPOD_IMAGE_PATH_PREFIX,
    CHECKPOD_PROGRESS_PATH,
    COVER_SIZE,
    COVER_SIZE_SMALL,
    PROGRESS_EXPIRY_HOURS,
    COLORS,
)
from ..models import CatalogItem

logger = logging.getLogger(__name__)

# Bump when cover processing changes so existing caches are regenerated once.
CHECKPOD_IMAGE_VERSION = 6


def _fit_cover_to_square(img: Image.Image, size: int) -> Image.Image:
    """Scale image to fit inside size×size without cropping (letterbox)."""
    bg = (*COLORS['bg_primary'], 255)
    img_w, img_h = img.size
    if img_w <= 0 or img_h <= 0:
        return Image.new('RGBA', (size, size), bg)
    scale = min(size / img_w, size / img_h)
    new_w = max(1, int(img_w * scale))
    new_h = max(1, int(img_h * scale))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    canvas = Image.new('RGBA', (size, size), bg)
    paste_x = (size - new_w) // 2
    paste_y = (size - new_h) // 2
    canvas.paste(resized, (paste_x, paste_y), resized)
    return canvas


def _fit_width_letterbox(img: Image.Image, size: int) -> Image.Image:
    """Scale to full square width; pad top/bottom (no horizontal crop)."""
    bg = (*COLORS['bg_primary'], 255)
    img_w, img_h = img.size
    if img_w <= 0 or img_h <= 0:
        return Image.new('RGBA', (size, size), bg)
    scale = size / img_w
    new_w = size
    new_h = max(1, int(img_h * scale))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    canvas = Image.new('RGBA', (size, size), bg)
    paste_y = max(0, (size - new_h) // 2)
    canvas.paste(resized, (0, paste_y), resized)
    return canvas


def _prepare_checkpod_cover(img: Image.Image, size: int) -> Image.Image:
    """Letterbox landscape ARD art at full width, then rotate for portrait display."""
    return _fit_width_letterbox(img, size).transpose(Image.Transpose.ROTATE_270)


class CheckPodManager:
    """Owns CheckPod episode list, on-disk cache, and playback progress."""

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
        self._episode_audio_urls: dict[str, str] = {}
        self._refreshing = False

        CHECKPOD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CHECKPOD_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        self._load_catalog_from_disk()
        self.cleanup_stale_downloads()

    @property
    def items(self) -> List[CatalogItem]:
        with self._lock:
            return list(self._items)

    def get_display_items(self) -> List[CatalogItem]:
        return self.items

    def get_audio_url(self, episode_id: str) -> Optional[str]:
        with self._lock:
            return self._episode_audio_urls.get(episode_id)

    def get_episode_id_for_uri(self, uri: str) -> Optional[str]:
        prefix = 'urn:ard:episode:'
        if uri.startswith(prefix):
            return uri[len(prefix):]
        return None

    def cached_mp3_path(self, episode_id: str) -> Path:
        return CHECKPOD_CACHE_DIR / f'{episode_id}.mp3'

    def is_cached(self, episode_id: str) -> bool:
        return self.cached_mp3_path(episode_id).exists()

    def ensure_cached(self, episode_id: str, audio_url: Optional[str] = None) -> Optional[Path]:
        """Download episode MP3 if missing. Returns local path or None on failure."""
        path = self.cached_mp3_path(episode_id)
        if path.exists():
            return path

        url = audio_url or self.get_audio_url(episode_id)
        if not url:
            logger.warning(f'No audio URL for episode {episode_id}')
            return None

        self._on_toast('Lädt Folge…')
        try:
            resp = requests.get(url, timeout=(10, 25), stream=True)
            resp.raise_for_status()
            temp_path = path.with_suffix('.mp3.tmp')
            with open(temp_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            os.replace(temp_path, path)
            logger.info(f'Cached CheckPod episode {episode_id} -> {path}')
            return path
        except (requests.RequestException, OSError) as e:
            logger.warning(f'Failed to cache episode {episode_id}: {e}', exc_info=True)
            temp_path = path.with_suffix('.mp3.tmp')
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            self._on_toast('Download fehlgeschlagen')
            return None

    def refresh_episodes(self) -> bool:
        """Fetch latest episodes from ARD and update local catalog."""
        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True
        try:
            episodes = fetch_episodes()
            if not episodes:
                logger.info('CheckPod refresh returned no episodes (keeping cache)')
                return False
            force_images = self._should_regenerate_images()
            items = []
            audio_urls = {}
            for ep in episodes:
                image_path = self._ensure_episode_image(ep, force=force_images)
                items.append(CatalogItem(
                    id=ep.id,
                    uri=ep.uri,
                    name=ep.title,
                    type='episode',
                    artist='Checker Tobi',
                    image=image_path,
                ))
                audio_urls[ep.id] = ep.audio_url
            catalog = {
                'updated_at': datetime.now().isoformat(),
                'image_version': CHECKPOD_IMAGE_VERSION,
                'episodes': [
                    {
                        'id': ep.id,
                        'uri': ep.uri,
                        'title': ep.title,
                        'audio_url': ep.audio_url,
                        'image_url': ep.image_url,
                        'duration_ms': ep.duration_ms,
                        'published_at': ep.published_at,
                        'image': items[i].image,
                    }
                    for i, ep in enumerate(episodes)
                ],
            }
            self._save_catalog(catalog)
            with self._lock:
                self._items = items
                self._episode_audio_urls = audio_urls
            self._on_invalidate()
            logger.info(f'CheckPod catalog refreshed ({len(items)} episodes)')
            return True
        finally:
            with self._lock:
                self._refreshing = False

    def _image_path_exists(self, image_path: Optional[str]) -> bool:
        if not image_path or not image_path.startswith(CHECKPOD_IMAGE_PATH_PREFIX):
            return bool(image_path)
        base = image_path.replace(CHECKPOD_IMAGE_PATH_PREFIX, '').replace('.png', '')
        return (CHECKPOD_IMAGES_DIR / f'{base}.png').exists()

    def _load_catalog_from_disk(self):
        if not CHECKPOD_CATALOG_PATH.exists():
            return
        try:
            data = json.loads(CHECKPOD_CATALOG_PATH.read_text())
            episodes = data.get('episodes') or []
            items = []
            audio_urls = {}
            for entry in episodes:
                if not isinstance(entry, dict):
                    continue
                episode_id = str(entry.get('id') or '')
                uri = entry.get('uri') or f'urn:ard:episode:{episode_id}'
                title = entry.get('title') or 'Folge'
                image = entry.get('image')
                if image and not self._image_path_exists(image):
                    image = None
                if episode_id:
                    items.append(CatalogItem(
                        id=episode_id,
                        uri=uri,
                        name=title,
                        type='episode',
                        artist='Checker Tobi',
                        image=image,
                    ))
                    if entry.get('audio_url'):
                        audio_urls[episode_id] = entry['audio_url']
            with self._lock:
                self._items = items
                self._episode_audio_urls = audio_urls
            logger.info(f'CheckPod catalog loaded from disk ({len(items)} episodes)')
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f'Failed to load CheckPod catalog: {e}', exc_info=True)

    def _save_catalog(self, data: dict):
        temp = CHECKPOD_CATALOG_PATH.with_suffix('.json.tmp')
        temp.write_text(json.dumps(data, indent=2))
        os.replace(temp, CHECKPOD_CATALOG_PATH)

    def _should_regenerate_images(self) -> bool:
        if not CHECKPOD_CATALOG_PATH.exists():
            return True
        try:
            data = json.loads(CHECKPOD_CATALOG_PATH.read_text())
            return int(data.get('image_version', 1)) < CHECKPOD_IMAGE_VERSION
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return True

    def _delete_episode_images(self, base_name: str):
        for suffix in ('', '_small', '_dim', '_small_dim'):
            path = CHECKPOD_IMAGES_DIR / f'{base_name}{suffix}.png'
            if path.exists():
                path.unlink()

    def _ensure_episode_image(self, episode: ArdEpisode, force: bool = False) -> Optional[str]:
        base_name = episode.id
        if force:
            self._delete_episode_images(base_name)
        main_path = CHECKPOD_IMAGES_DIR / f'{base_name}.png'
        if main_path.exists():
            return f'{CHECKPOD_IMAGE_PATH_PREFIX}{base_name}.png'
        if not episode.image_url:
            return None
        try:
            resp = requests.get(episode.image_url, timeout=15)
            resp.raise_for_status()
            img = Image.open(BytesIO(resp.content)).convert('RGBA')
            sizes = [
                (COVER_SIZE, ''),
                (COVER_SIZE_SMALL, '_small'),
            ]
            for size, suffix in sizes:
                fitted = _prepare_checkpod_cover(img, size)
                radius = max(12, size // 25)
                processed = apply_rounded_corners_pil(fitted, radius)
                processed.save(CHECKPOD_IMAGES_DIR / f'{base_name}{suffix}.png', 'PNG')
                dimmed = apply_dimming(processed)
                dimmed.save(CHECKPOD_IMAGES_DIR / f'{base_name}{suffix}_dim.png', 'PNG')
            return f'{CHECKPOD_IMAGE_PATH_PREFIX}{base_name}.png'
        except (requests.RequestException, OSError, ValueError) as e:
            logger.warning(f'Failed to download CheckPod cover {episode.id}: {e}')
            return None

    def _load_progress_data(self) -> dict:
        with self._progress_lock:
            try:
                if CHECKPOD_PROGRESS_PATH.exists():
                    return json.loads(CHECKPOD_PROGRESS_PATH.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f'Error reading CheckPod progress: {e}')
            return {}

    def _save_progress_data(self, data: dict):
        with self._progress_lock:
            temp = CHECKPOD_PROGRESS_PATH.with_suffix('.json.tmp')
            temp.write_text(json.dumps(data, indent=2))
            os.replace(temp, CHECKPOD_PROGRESS_PATH)

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
            logger.warning(f'Error reading CheckPod progress: {e}')
            return None

    def save_progress(self, context_uri: str, position_ms: int, duration_ms: int, name: str = ''):
        if not context_uri or position_ms < 0:
            return
        try:
            data = self._load_progress_data()
            entry = {
                'uri': context_uri,
                'position': position_ms,
                'duration': duration_ms,
                'name': name,
                'updatedAt': datetime.now().isoformat(),
            }
            data[context_uri] = entry
            self._save_progress_data(data)
            logger.debug(f'CheckPod progress saved: {name} @ {position_ms // 1000}s')
        except OSError as e:
            logger.warning(f'Error saving CheckPod progress: {e}', exc_info=True)

    def clear_progress(self, context_uri: str):
        try:
            data = self._load_progress_data()
            if context_uri in data:
                del data[context_uri]
                self._save_progress_data(data)
        except OSError as e:
            logger.warning(f'Error clearing CheckPod progress: {e}')

    def cleanup_stale_downloads(
        self,
        active_context_uri: Optional[str] = None,
        max_age_days: Optional[int] = None,
    ) -> int:
        """Delete cached MP3s not played within max_age_days."""
        if max_age_days is None:
            max_age_days = CHECKPOD_DOWNLOAD_RETENTION_DAYS

        active_episode_id = self.get_episode_id_for_uri(active_context_uri) if active_context_uri else None
        progress_data = self._load_progress_data()
        progress_changed = False
        deleted = 0
        now = datetime.now()

        for mp3_path in sorted(CHECKPOD_CACHE_DIR.glob('*.mp3')):
            episode_id = mp3_path.stem
            if episode_id == active_episode_id:
                continue

            context_uri = f'urn:ard:episode:{episode_id}'
            entry = progress_data.get(context_uri)
            try:
                if entry and entry.get('updatedAt'):
                    last_used = datetime.fromisoformat(entry['updatedAt'])
                else:
                    last_used = datetime.fromtimestamp(mp3_path.stat().st_mtime)
            except (OSError, ValueError, TypeError):
                continue

            age_days = (now - last_used).total_seconds() / 86400
            if age_days < max_age_days:
                continue

            try:
                mp3_path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning(f'CheckPod cleanup failed to delete {mp3_path}: {e}')
                continue

            self._delete_episode_images(episode_id)
            if context_uri in progress_data:
                del progress_data[context_uri]
                progress_changed = True
            deleted += 1
            logger.info(
                f'CheckPod cleanup: deleted {episode_id}.mp3 '
                f'(last used {last_used.isoformat()}, age={age_days:.1f}d)'
            )

        for tmp_path in CHECKPOD_CACHE_DIR.glob('*.mp3.tmp'):
            try:
                age_days = (now - datetime.fromtimestamp(tmp_path.stat().st_mtime)).total_seconds() / 86400
                if age_days >= max_age_days:
                    tmp_path.unlink(missing_ok=True)
                    logger.info(f'CheckPod cleanup: deleted stale temp {tmp_path.name}')
            except OSError as e:
                logger.warning(f'CheckPod cleanup failed to delete temp {tmp_path}: {e}')

        if progress_changed:
            self._save_progress_data(progress_data)

        if deleted:
            logger.info(f'CheckPod cleanup complete: removed {deleted} episode(s)')
        return deleted
