"""Scene base class: a list of widgets plus the hooks the app calls."""

from __future__ import annotations

import pygame


class Scene:
    def __init__(self, app) -> None:
        self.app = app
        self.widgets: list = []

    # lifecycle
    def enter(self) -> None:
        pass

    def leave(self) -> None:
        pass

    def layout(self, th) -> None:
        pass

    def update(self, dt: float) -> None:
        pass

    # input; return True if consumed
    def pointer_down(self, pos) -> bool:
        for w in self.widgets:
            if getattr(w, "on_down", None) and w.on_down(pos):
                return True
        return False

    def pointer_up(self, pos) -> bool:
        for w in self.widgets:
            if getattr(w, "on_up", None) and w.on_up(pos):
                return True
        return False

    def pointer_motion(self, pos) -> None:
        for w in self.widgets:
            if getattr(w, "on_motion", None):
                w.on_motion(pos)

    def release_inputs(self) -> None:
        """Release anything the user might be holding (focus loss, scene change)."""
        for w in self.widgets:
            force = getattr(w, "force_release", None)
            if force:
                force()

    def key_down(self, ev) -> bool:
        return False

    def key_up(self, ev) -> bool:
        return False

    # output
    def draw(self, surf, th) -> None:
        surf.fill(th.BG)


def rect_grid(area: pygame.Rect, cols: int, rows: int, gap: int = 0):
    """Split `area` into cols x rows rects."""
    w = (area.w - (cols - 1) * gap) / cols
    h = (area.h - (rows - 1) * gap) / rows
    for r in range(rows):
        for c in range(cols):
            yield pygame.Rect(int(area.x + c * (w + gap)), int(area.y + r * (h + gap)), int(w), int(h))
