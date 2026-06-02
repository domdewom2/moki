"""
Search cover prefetch — download Spotify images and cache rotated variants locally.
"""
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import List, Tuple

import requests
from PIL import Image

from ..config import (
    COVER_SIZE,
    COVER_SIZE_SMALL,
    SEARCH_CACHE_DIR,
    SEARCH_CACHE_PATH_PREFIX,
)
from ..models import SearchResult
from .catalog import apply_rounded_corners_pil, apply_dimming

logger = logging.getLogger(__name__)

PREFETCH_MAX_WORKERS = 3


def _download_image(image_url: str) -> Tuple[str, Image.Image]:
    """Download image and return (hash_short, RGBA PIL Image)."""
    response = requests.get(image_url, timeout=10)
    response.raise_for_status()
    buffer = response.content
    hash_short = hashlib.md5(buffer).hexdigest()[:8]
    img = Image.open(BytesIO(buffer)).convert('RGBA')
    return hash_short, img


def _save_search_cover(hash_short: str, img: Image.Image) -> str:
    """Save rotated cover variants and return local preview path."""
    SEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    local_path = f'{SEARCH_CACHE_PATH_PREFIX}{hash_short}.png'
    main_path = SEARCH_CACHE_DIR / f'{hash_short}.png'
    if main_path.exists():
        return local_path

    img = img.transpose(Image.Transpose.ROTATE_270)
    for size, suffix in ((COVER_SIZE, ''), (COVER_SIZE_SMALL, '_small')):
        resized = img.resize((size, size), Image.Resampling.LANCZOS)
        radius = max(12, size // 25)
        processed = apply_rounded_corners_pil(resized, radius)
        processed.save(SEARCH_CACHE_DIR / f'{hash_short}{suffix}.png', 'PNG')
        apply_dimming(processed).save(
            SEARCH_CACHE_DIR / f'{hash_short}{suffix}_dim.png', 'PNG'
        )

    logger.info(f'Search cover cached: {local_path}')
    return local_path


def _prefetch_one(result: SearchResult) -> SearchResult:
    """Download and cache cover for a single search result."""
    if not result.image_url or not result.image_url.startswith('http'):
        return result
    try:
        hash_short, img = _download_image(result.image_url)
        preview = _save_search_cover(hash_short, img)
        return SearchResult(
            uri=result.uri,
            name=result.name,
            artist=result.artist,
            type=result.type,
            image_url=result.image_url,
            preview_image=preview,
            is_playable=result.is_playable,
        )
    except Exception as e:
        logger.warning(
            f'Search cover prefetch failed for {result.name!r}: {e}',
            exc_info=True,
        )
        return result


def prefetch_covers_incremental(
    results: List[SearchResult],
    on_update=None,
) -> List[SearchResult]:
    """Prefetch covers one at a time, calling on_update after each."""
    updated: List[SearchResult] = list(results)
    for index, result in enumerate(results):
        updated[index] = _prefetch_one(result)
        if on_update:
            on_update(list(updated))
    return updated


def prefetch_covers(results: List[SearchResult]) -> List[SearchResult]:
    """Download and cache cover images for search results."""
    if not results:
        return []
    if len(results) == 1:
        return [_prefetch_one(results[0])]
    with ThreadPoolExecutor(max_workers=PREFETCH_MAX_WORKERS) as pool:
        return list(pool.map(_prefetch_one, results))
