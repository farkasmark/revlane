"""Colours and metrics.

Everything is expressed in fractions of the window and multiplied by `scale`,
so the same layout fills a 1920x1080 desktop, a 800x480 LCD panel, or a phone
in landscape without a second stylesheet.
"""

from __future__ import annotations

import pygame

# palette - dark asphalt / safety amber / racing red
BG = (16, 18, 24)
PANEL = (27, 31, 40)
PANEL_2 = (35, 40, 52)
LINE = (52, 60, 78)
TEXT = (232, 236, 244)
DIM = (150, 162, 182)
FAINT = (96, 106, 126)
GOOD = (67, 160, 71)
WARN = (255, 179, 0)
BAD = (229, 57, 53)
IDLE_BTN = (46, 53, 68)
IDLE_BTN_UP = (58, 66, 84)
SHADOW = (8, 9, 12)


def mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def battery_color(pct: int) -> tuple[int, int, int]:
    if pct >= 60:
        return GOOD
    if pct >= 30:
        return WARN
    return BAD


class Theme:
    """Scales with the window. Re-create (or call resize) on window resize."""

    def __init__(self, width: int, height: int):
        self.resize(width, height)

    def resize(self, width: int, height: int) -> None:
        self.w, self.h = width, height
        self.scale = max(0.75, min(2.2, min(width, height) / 480.0))
        self.pad = int(16 * self.scale)
        self.radius = int(14 * self.scale)
        self.font = _FontCache()

    def fs(self, base: int) -> int:
        return max(11, int(base * self.scale))

    # handy hit target: at least ~48 css px for a finger
    def touch_min(self) -> int:
        return int(48 * self.scale)

    def inset(self, rect: pygame.Rect, by: int | None = None) -> pygame.Rect:
        return rect.inflate(-(by or self.pad) * 2, -(by or self.pad) * 2)


class _FontCache:
    def __init__(self) -> None:
        self._cache: dict[int, pygame.font.Font] = {}

    def get(self, size: int) -> pygame.font.Font:
        # pygame's built-in font needs no asset, which keeps the app
        # self-contained on Android / Raspberry Pi / whatever
        f = self._cache.get(size)
        if f is None:
            f = pygame.font.Font(None, size)
            self._cache[size] = f
        return f
