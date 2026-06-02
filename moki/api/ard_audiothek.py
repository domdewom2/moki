"""
ARD Audiothek GraphQL client for CheckPod episodes.
"""
import logging
from dataclasses import dataclass
from typing import List, Optional

import requests

from ..config import CHECKPOD_ARD_SHOW_ID, CHECKPOD_EPISODE_LIMIT

logger = logging.getLogger(__name__)

GRAPHQL_URL = 'https://api.ardaudiothek.de/graphql'

EPISODES_QUERY = """
query CheckPodEpisodes($id: ID!, $count: Int!, $after: Cursor) {
  result: programSet(id: $id) {
    title
    items(
      first: $count
      after: $after
      orderBy: PUBLISH_DATE_DESC
      filter: {
        isPublished: { equalTo: true }
        itemType: { notEqualTo: EVENT_LIVESTREAM }
      }
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        id
        title
        duration
        publishDate
        image { url }
        audios { url }
      }
    }
  }
}
"""


@dataclass
class ArdEpisode:
    """Parsed episode from ARD Audiothek."""
    id: str
    title: str
    audio_url: str
    image_url: Optional[str]
    duration_ms: int
    published_at: Optional[str] = None

    @property
    def uri(self) -> str:
        return f'urn:ard:episode:{self.id}'


@dataclass
class ArdEpisodePage:
    """One page of episodes from ARD GraphQL pagination."""
    episodes: List[ArdEpisode]
    has_next_page: bool
    end_cursor: Optional[str]


def _pick_mp3_url(audios: list) -> Optional[str]:
    """Return the MP3 stream URL from ARD audio variants."""
    if not audios:
        return None
    for entry in audios:
        url = (entry or {}).get('url') or ''
        if url.endswith('.mp3'):
            return url
    return (audios[0] or {}).get('url')


def _normalize_image_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return url.replace('{width}', '800')


def parse_episode_page(data: dict) -> ArdEpisodePage:
    """Parse GraphQL response into episodes plus pagination info."""
    result = (data or {}).get('data', {}).get('result')
    if not result:
        return ArdEpisodePage(episodes=[], has_next_page=False, end_cursor=None)

    items = result.get('items') or {}
    page_info = items.get('pageInfo') or {}
    episodes = parse_episodes({'data': {'result': {'items': {'nodes': items.get('nodes') or []}}}})
    return ArdEpisodePage(
        episodes=episodes,
        has_next_page=bool(page_info.get('hasNextPage')),
        end_cursor=page_info.get('endCursor'),
    )


def parse_episodes(data: dict) -> List[ArdEpisode]:
    """Parse GraphQL response into episode list."""
    result = (data or {}).get('data', {}).get('result')
    if not result:
        return []

    nodes = ((result.get('items') or {}).get('nodes')) or []
    episodes: List[ArdEpisode] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        episode_id = str(node.get('id') or '').strip()
        title = (node.get('title') or '').strip()
        audio_url = _pick_mp3_url(node.get('audios') or [])
        if not episode_id or not title or not audio_url:
            logger.warning(f'Skipping incomplete ARD episode node: id={episode_id!r}')
            continue
        duration_sec = int(node.get('duration') or 0)
        image = node.get('image') if isinstance(node.get('image'), dict) else {}
        episodes.append(ArdEpisode(
            id=episode_id,
            title=title,
            audio_url=audio_url,
            image_url=_normalize_image_url(image.get('url')),
            duration_ms=max(0, duration_sec * 1000),
            published_at=node.get('publishDate'),
        ))
    return episodes


def fetch_episodes_page(
    show_id: str = CHECKPOD_ARD_SHOW_ID,
    limit: int = CHECKPOD_EPISODE_LIMIT,
    after: Optional[str] = None,
    timeout: float = 15.0,
) -> ArdEpisodePage:
    """Fetch one page of published episodes (newest first)."""
    variables = {'id': show_id, 'count': limit}
    if after:
        variables['after'] = after
    payload = {
        'query': EPISODES_QUERY,
        'variables': variables,
    }
    try:
        resp = requests.post(GRAPHQL_URL, json=payload, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
        if body.get('errors'):
            logger.warning(f'ARD GraphQL errors: {body["errors"]}')
        page = parse_episode_page(body)
        logger.info(
            f'ARD fetched {len(page.episodes)} episodes for show {show_id} '
            f'(after={"set" if after else "none"}, has_next={page.has_next_page})'
        )
        return page
    except requests.RequestException as e:
        logger.warning(f'ARD fetch failed: {e}', exc_info=True)
        return ArdEpisodePage(episodes=[], has_next_page=False, end_cursor=None)


def fetch_episodes(
    show_id: str = CHECKPOD_ARD_SHOW_ID,
    limit: int = CHECKPOD_EPISODE_LIMIT,
    timeout: float = 15.0,
) -> List[ArdEpisode]:
    """Fetch latest published episodes for an ARD show (first page only)."""
    return fetch_episodes_page(show_id=show_id, limit=limit, timeout=timeout).episodes
