"""
Tests for search cover prefetch.
"""
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))

from moki.api.search_covers import prefetch_covers, _save_search_cover
from moki.models import SearchResult


def _sample_png_bytes() -> bytes:
    img = Image.new('RGBA', (64, 64), (120, 80, 200, 255))
    buf = BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


class TestSearchCovers:
    def test_prefetch_sets_preview_image(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / 'search_cache'
        monkeypatch.setattr('moki.api.search_covers.SEARCH_CACHE_DIR', cache_dir)
        monkeypatch.setattr('moki.api.search_covers.SEARCH_CACHE_PATH_PREFIX', '/search_cache/')

        result = SearchResult(
            uri='spotify:album:test',
            name='Test Album',
            artist='Artist',
            type='album',
            image_url='https://example.com/cover.png',
        )
        response = MagicMock()
        response.content = _sample_png_bytes()
        response.raise_for_status = MagicMock()

        with patch('moki.api.search_covers.requests.get', return_value=response):
            updated = prefetch_covers([result])

        assert len(updated) == 1
        assert updated[0].preview_image
        assert updated[0].preview_image.startswith('/search_cache/')
        filename = updated[0].preview_image.replace('/search_cache/', '')
        assert (cache_dir / filename).exists()

    def test_save_search_cover_skips_existing(self, tmp_path, monkeypatch):
        cache_dir = tmp_path / 'search_cache'
        cache_dir.mkdir()
        monkeypatch.setattr('moki.api.search_covers.SEARCH_CACHE_DIR', cache_dir)
        monkeypatch.setattr('moki.api.search_covers.SEARCH_CACHE_PATH_PREFIX', '/search_cache/')

        img = Image.new('RGBA', (64, 64), (255, 0, 0, 255))
        (cache_dir / 'abcd1234.png').write_bytes(b'existing')

        path = _save_search_cover('abcd1234', img)
        assert path == '/search_cache/abcd1234.png'
        assert (cache_dir / 'abcd1234.png').read_bytes() == b'existing'
