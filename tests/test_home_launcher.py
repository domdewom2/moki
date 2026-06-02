"""Tests for home screen grid layout."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from moki.ui.home_launcher import home_page_count, slot_center, app_center
from moki.config import HOME_APPS_PER_PAGE, SCREEN_HEIGHT


def test_home_page_count():
    assert home_page_count(0) == 1
    assert home_page_count(5) == 1
    assert home_page_count(8) == 1
    assert home_page_count(9) == 2


def test_slot_centers_are_unique_on_page():
    centers = [slot_center(i) for i in range(HOME_APPS_PER_PAGE)]
    assert len(set(centers)) == HOME_APPS_PER_PAGE


def test_app_center_moves_with_page():
    x0, y0 = app_center(0, page_scroll=0.0)
    x1, y1 = app_center(8, page_scroll=0.0)
    assert x0 == x1
    assert y1 - y0 == SCREEN_HEIGHT
