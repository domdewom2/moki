"""
Home screen app grid — iPhone-style 4×2 launcher with paginated swipe.
"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

import pygame

from ..config import (
    SCREEN_WIDTH,
    SCREEN_HEIGHT,
    HOME_APPS_PER_PAGE,
    HOME_GRID_COLS,
    HOME_GRID_ROWS,
    HOME_ICON_SIZE,
    HOME_ICON_GAP_X,
    HOME_ICON_GAP_Y,
    HOME_ICON_HIT_PADDING,
)


@dataclass
class HomeAppEntry:
    """One tappable app on the home screen."""
    app_id: str
    icon: Optional[pygame.Surface]


def home_page_count(app_count: int) -> int:
    return max(1, (app_count + HOME_APPS_PER_PAGE - 1) // HOME_APPS_PER_PAGE)


def slot_center(slot: int) -> Tuple[int, int]:
    """Center of a grid slot (0–7) on one page. X = user vertical, Y = user horizontal."""
    col = slot % HOME_GRID_COLS
    row = slot // HOME_GRID_COLS

    row_span = HOME_GRID_ROWS * HOME_ICON_SIZE + (HOME_GRID_ROWS - 1) * HOME_ICON_GAP_X
    x_top = (SCREEN_WIDTH + row_span) // 2 - HOME_ICON_SIZE // 2
    x = x_top - row * (HOME_ICON_SIZE + HOME_ICON_GAP_X)

    col_span = HOME_GRID_COLS * HOME_ICON_SIZE + (HOME_GRID_COLS - 1) * HOME_ICON_GAP_Y
    y_start = (SCREEN_HEIGHT - col_span) // 2 + HOME_ICON_SIZE // 2
    y = y_start + col * (HOME_ICON_SIZE + HOME_ICON_GAP_Y)
    return (x, y)


def app_center(
    app_index: int,
    page_scroll: float,
    drag_offset: float = 0.0,
) -> Tuple[int, int]:
    """Screen center for app index, accounting for page scroll and drag."""
    page = app_index // HOME_APPS_PER_PAGE
    slot = app_index % HOME_APPS_PER_PAGE
    x, y = slot_center(slot)
    page_delta = page - page_scroll
    y += int(round(page_delta * SCREEN_HEIGHT)) + int(drag_offset)
    return (x, y)


def icon_hit_rect(center: Tuple[int, int]) -> pygame.Rect:
    size = HOME_ICON_SIZE + HOME_ICON_HIT_PADDING * 2
    rect = pygame.Rect(0, 0, size, size)
    rect.center = center
    return rect


def visible_app_indices(app_count: int, page_scroll: float) -> List[int]:
    """App indices that may be visible around the current scroll position."""
    if app_count <= 0:
        return []
    low_page = int(page_scroll - 0.6)
    high_page = int(page_scroll + 0.6)
    indices: List[int] = []
    for page in range(max(0, low_page), high_page + 1):
        start = page * HOME_APPS_PER_PAGE
        end = min(start + HOME_APPS_PER_PAGE, app_count)
        indices.extend(range(start, end))
    return indices
