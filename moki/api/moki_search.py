"""
Moki Search API client — Spotify album/playlist search via api.mokikids.de.
"""
import logging
from typing import List, Optional

import requests

from ..config import MOKI_SEARCH_URL, MOKI_SEARCH_LIMIT, MOKI_SEARCH_TYPES
from ..models import SearchResult

logger = logging.getLogger(__name__)

SEARCH_TIMEOUT = 10


def _image_url(item: dict) -> Optional[str]:
    images = item.get('images') or []
    if not images:
        return None
    url = images[0].get('url') if isinstance(images[0], dict) else None
    return url or None


def _parse_album(item: dict) -> Optional[SearchResult]:
    if not item.get('is_playable', True):
        return None
    uri = item.get('uri') or ''
    name = item.get('name') or ''
    if not uri or not name:
        return None
    artists = item.get('artists') or []
    artist = ''
    if artists and isinstance(artists[0], dict):
        artist = artists[0].get('name') or ''
    return SearchResult(
        uri=uri,
        name=name,
        artist=artist,
        type='album',
        image_url=_image_url(item),
        is_playable=True,
    )


def _parse_playlist(item: dict) -> Optional[SearchResult]:
    uri = item.get('uri') or ''
    name = item.get('name') or ''
    if not uri or not name:
        return None
    owner = item.get('owner') or {}
    artist = owner.get('display_name') or '' if isinstance(owner, dict) else ''
    return SearchResult(
        uri=uri,
        name=name,
        artist=artist,
        type='playlist',
        image_url=_image_url(item),
        is_playable=True,
    )


def parse_search_response(data: dict) -> List[SearchResult]:
    """Parse raw API JSON into normalized search results (albums first, then playlists)."""
    results: List[SearchResult] = []
    albums = (data.get('albums') or {}).get('items') or []
    for item in albums:
        if not isinstance(item, dict):
            continue
        parsed = _parse_album(item)
        if parsed:
            results.append(parsed)

    playlists = (data.get('playlists') or {}).get('items') or []
    for item in playlists:
        if not isinstance(item, dict):
            continue
        parsed = _parse_playlist(item)
        if parsed:
            results.append(parsed)

    return results


def search(query: str, limit: Optional[int] = None) -> List[SearchResult]:
    """Search Spotify albums and playlists via the Moki proxy API."""
    q = (query or '').strip()
    if not q:
        return []

    params = {
        'q': q,
        'type': MOKI_SEARCH_TYPES,
        'limit': limit if limit is not None else MOKI_SEARCH_LIMIT,
    }
    logger.info(f'Moki search: q="{q}" limit={params["limit"]}')

    response = requests.get(MOKI_SEARCH_URL, params=params, timeout=SEARCH_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    results = parse_search_response(data)
    logger.info(f'Moki search: {len(results)} results for q="{q}"')
    return results
