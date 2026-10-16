"""A tiny retained-mode widget set for pygame.

Deliberately small: a click is a down followed by an up inside the same widget,
and a *hold* is a down that sets a flag until the up.  Touch and mouse are
normalised into the same "pointer" stream by `Pointer`, so every widget works
identically under a finger or a cursor - the reason `HoldButton` can give true
hold-to-drive on a phone where a TTY keyboard never could.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import pygame

from . import theme as T


def rounded(surf, rect, color, radius, border=0, border_color=None):
    if border:
        pygame.draw.rect(surf, border_color or T.LINE, rect, border, border_radius=radius)
        inner = rect.inflate(-border * 2, -border * 2)
        pygame.draw.rect(surf, color, inner, 0, border_radius=max(0, radius - border))
    else:
        pygame.draw.rect(surf, color, rect, 0, border_radius=radius)


class Widget:
    def __init__(self, rect):
        self.rect = pygame.Rect(rect)
        self.enabled = True
        self.visible = True

    def hit(self, pos) -> bool:
        return self.visible and self.rect.collidepoint(pos)

    def on_down(self, pos) -> bool:  # pragma: no cover - default
        return False

    def on_up(self, pos) -> bool:  # pragma: no cover - default
        return False

    def on_motion(self, pos) -> None:  # pragma: no cover - default
        pass

    def draw(self, surf, th) -> None:  # pragma: no cover - implemented by subclasses
        pass


class Label(Widget):
    def __init__(self, rect, text="", color=None, size=20, align="l"):
        super().__init__(rect)
        self.text = text
        self.color = color or T.TEXT
        self.size = size
        self.align = align

    def draw(self, surf, th):
        if not self.visible or not self.text:
            return
        f = th.font.get(th.fs(self.size))
        lines = self.text.split("\n")
        line_h = f.get_linesize() + int(2 * th.scale)
        block_h = line_h * len(lines)
        anchor = pygame.Rect(0, 0, 0, block_h)
        if self.align == "l":
            anchor.midleft = self.rect.midleft
        elif self.align == "r":
            anchor.midright = self.rect.midright
        else:
            anchor.center = self.rect.center
        y = anchor.y
        for line in lines:
            img = f.render(line, True, self.color)
            r = img.get_rect(midleft=(anchor.midleft[0], y + line_h // 2))
            if self.align == "r":
                r.midright = anchor.midright
            elif self.align == "c":
                r.centerx = anchor.centerx
            surf.blit(img, r)
            y += line_h


class Button(Widget):
    """Momentary button. Fires `on_click` on release inside the rect."""

    def __init__(
        self,
        rect,
        label="",
        on_click: Callable[[], None] | None = None,
        kind: str = "ghost",
        accent=None,
        size=26,
        sub="",
    ):
        super().__init__(rect)
        self.label = label
        self.sub = sub
        self.on_click = on_click
        self.kind = kind
        self.accent = accent or T.WARN
        self.size = size
        self.pressed = False
        self.hot = False

    def _colors(self):
        if self.kind == "primary":
            base, fg = self.accent, (18, 20, 26)
        elif self.kind == "danger":
            base, fg = T.BAD, (255, 255, 255)
        elif self.kind == "good":
            base, fg = T.GOOD, (255, 255, 255)
        else:
            base, fg = (T.IDLE_BTN_UP if self.hot else T.IDLE_BTN), T.TEXT
        if self.pressed:
            base = T.mix(base, (0, 0, 0), 0.28)
        return base, fg

    def on_down(self, pos) -> bool:
        if not (self.enabled and self.hit(pos)):
            return False
        self.pressed = True
        return True

    def on_up(self, pos) -> bool:
        was = self.pressed
        self.pressed = False
        if was and self.enabled and self.hit(pos) and self.on_click:
            self.on_click()
            return True
        return was

    def draw(self, surf, th):
        if not self.visible:
            return
        base, fg = self._colors()
        rounded(surf, self.rect, base, th.radius)
        if self.kind == "ghost":
            rounded(surf, self.rect, base, th.radius, border=max(1, int(2 * th.scale)),
                    border_color=T.LINE if not self.pressed else self.accent)
        f = th.font.get(th.fs(self.size))
        img = f.render(self.label, True, fg)
        if self.sub:
            sf = th.font.get(th.fs(self.size - 8))
            simg = sf.render(self.sub, True, T.mix(fg, (0, 0, 0), 0.35))
            # center the label+sub BLOCK as a unit: anchoring the pair on
            # centery pushed it visually off-center
            gap = int(2 * th.scale)
            total = img.get_height() + gap + simg.get_height()
            y0 = self.rect.centery - total // 2
            surf.blit(img, img.get_rect(midtop=(self.rect.centerx, y0)))
            surf.blit(
                simg,
                simg.get_rect(midtop=(self.rect.centerx, y0 + img.get_height() + gap)),
            )
        else:
            surf.blit(img, img.get_rect(center=self.rect.center))


class HoldButton(Button):
    """Stay-while-held. Reports state through `on_state(True/False)`."""

    def __init__(self, rect, label="", on_state=None, arrow=None, **kw):
        super().__init__(rect, label, None, **kw)
        self.on_state = on_state or (lambda v: None)
        self.arrow = arrow  # 'up','down','left','right' or None
        self.holding = False

    def _set(self, value: bool) -> None:
        if value != self.holding:
            self.holding = value
            self.on_state(value)

    def on_down(self, pos) -> bool:
        if not (self.enabled and self.hit(pos)):
            return False
        self.pressed = True
        self._set(True)
        return True

    def on_up(self, pos) -> bool:
        if not self.pressed:
            return False
        self.pressed = False
        self._set(False)  # release anywhere = stop
        return True

    def force_release(self) -> None:
        self.pressed = False
        self._set(False)

    def _colors(self):
        if self.holding:
            return (T.mix(self.accent, (0, 0, 0), 0.1) if self.pressed else self.accent), (18, 20, 26)
        return T.IDLE_BTN, T.TEXT

    def draw(self, surf, th):
        if not self.visible:
            return
        base, fg = self._colors()
        rounded(surf, self.rect, base, th.radius)
        rounded(
            surf,
            self.rect,
            base,
            th.radius,
            border=max(1, int(2 * th.scale)),
            border_color=T.LINE if not self.holding else self.accent,
        )
        if self.arrow:
            _arrow(surf, self.rect, fg, self.arrow, th)
        if self.label:
            f = th.font.get(th.fs(self.size - 6))
            img = f.render(self.label, True, fg)
            surf.blit(
                img,
                img.get_rect(center=(self.rect.centerx, self.rect.bottom - th.fs(self.size) // 2 - 4)),
            )


def _arrow(surf, rect, color, direction, th):
    cx, cy = rect.center
    s = int(min(rect.w, rect.h) * 0.22)
    pts = {
        "up": [(cx, cy - s), (cx - s, cy + int(s * 0.6)), (cx + s, cy + int(s * 0.6))],
        "down": [(cx, cy + s), (cx - s, cy - int(s * 0.6)), (cx + s, cy - int(s * 0.6))],
        "left": [(cx - s, cy), (cx + int(s * 0.6), cy - s), (cx + int(s * 0.6), cy + s)],
        "right": [(cx + s, cy), (cx - int(s * 0.6), cy - s), (cx - int(s * 0.6), cy + s)],
    }[direction]
    pygame.draw.polygon(surf, color, pts)


class Toggle(Widget):
    def __init__(self, rect, label="", on_change=None, accent=None, value=False):
        super().__init__(rect)
        self.label = label
        self.value = value
        self.on_change = on_change or (lambda v: None)
        self.accent = accent or T.WARN

    def on_up(self, pos) -> bool:
        if not (self.enabled and self.hit(pos)):
            return False
        self.value = not self.value
        self.on_change(self.value)
        return True

    def draw(self, surf, th):
        if not self.visible:
            return
        if self.enabled:
            col = self.accent if self.value else T.IDLE_BTN
            label_col = (18, 20, 26) if self.value else T.TEXT
            border_col = self.accent if self.value else T.LINE
        else:
            # disabled: dimmed, no accent - the user must see it is inert
            col = T.IDLE_BTN
            label_col = T.FAINT
            border_col = T.LINE
        rounded(surf, self.rect, col, th.radius)
        rounded(surf, self.rect, col, th.radius, border=max(1, int(2 * th.scale)),
                border_color=border_col)
        dot_r = int(min(self.rect.w, self.rect.h) * 0.16)
        dot_x = self.rect.right - dot_r - int(10 * th.scale) if self.value else (
            self.rect.left + dot_r + int(10 * th.scale))
        pygame.draw.circle(surf, (18, 20, 26) if self.value else T.FAINT, (dot_x, self.rect.centery), dot_r)
        f = th.font.get(th.fs(20))
        img = f.render(self.label, True, label_col)
        surf.blit(img, img.get_rect(midleft=(self.rect.left + int(14 * th.scale), self.rect.centery)))


class Slider(Widget):
    def __init__(self, rect, lo=10, hi=100, value=80, on_change=None, label=""):
        super().__init__(rect)
        self.lo, self.hi = lo, hi
        self.value = value
        self.on_change = on_change or (lambda v: None)
        self.label = label
        self.dragging = False
        self._th = None

    def _track(self, th) -> pygame.Rect:
        m = int(12 * th.scale)
        return pygame.Rect(
            self.rect.x + m,
            self.rect.centery - int(4 * th.scale),
            max(1, self.rect.w - 2 * m),
            int(8 * th.scale),
        )

    def _to_value(self, x: int, th) -> int:
        track = self._track(th)
        t = 0.0 if track.w == 0 else (x - track.x) / track.w
        t = max(0.0, min(1.0, t))
        return int(round(self.lo + t * (self.hi - self.lo)))

    def on_down(self, pos) -> bool:
        if not (self.enabled and self.rect.collidepoint(pos)):
            return False
        self.dragging = True
        self._update(pos[0], self._th)
        return True

    def on_up(self, pos) -> bool:
        self.dragging = False
        return False

    def on_motion(self, pos) -> None:
        if self.dragging and self._th is not None:
            self._update(pos[0], self._th)

    def _update(self, x: int, th) -> None:
        v = self._to_value(x, th)
        if v != self.value:
            self.value = v
            self.on_change(v)

    def draw(self, surf, th):
        self._th = th
        if not self.visible:
            return
        track = self._track(th)
        rounded(surf, track, T.PANEL_2, track.h // 2)
        t = 0.0 if track.w == 0 else (self.value - self.lo) / (self.hi - self.lo)
        fill = track.copy()
        fill.w = max(track.h, int(track.w * t))
        rounded(surf, fill, T.WARN, fill.h // 2)
        kx = track.x + int(track.w * t)
        pygame.draw.circle(surf, T.TEXT, (kx, track.centery), int(14 * th.scale))
        f = th.font.get(th.fs(18))
        img = f.render(f"{self.label} {self.value}%", True, T.DIM)
        surf.blit(img, img.get_rect(midleft=(self.rect.x, self.rect.top - th.fs(12))))


class BatteryGauge(Widget):
    def __init__(self, rect):
        super().__init__(rect)
        self.pct: int | None = None

    def draw(self, surf, th):
        if not self.visible:
            return
        r = self.rect
        rounded(surf, r, T.PANEL_2, int(6 * th.scale))
        pad = max(2, int(3 * th.scale))
        inner = r.inflate(-pad * 2, -pad * 2)
        # battery nub
        nub = pygame.Rect(r.right, r.centery - inner.h // 4, max(3, int(4 * th.scale)), inner.h // 2)
        pygame.draw.rect(surf, T.PANEL_2, nub, border_radius=2)
        if self.pct is None:
            f = th.font.get(th.fs(16))
            img = f.render("--%", True, T.FAINT)
            surf.blit(img, img.get_rect(center=r.center))
            return
        col = T.battery_color(self.pct)
        fill = inner.copy()
        fill.w = max(0, int(inner.w * self.pct / 100.0))
        pygame.draw.rect(surf, col, fill, border_radius=max(1, int(4 * th.scale)))
        f = th.font.get(th.fs(16))
        label = f"{self.pct}%"
        img = f.render(label, True, T.TEXT if self.pct < 55 else (18, 20, 26))
        surf.blit(img, img.get_rect(center=(inner.centerx if self.pct < 55 else inner.x + inner.w // 2,
                                            r.centery)))


def signal_bars(surf, rect, rssi: int, th) -> None:
    """Four bars from an RSSI value."""
    level = 0 if rssi >= -50 else 1
    for lo, n in ((-50, 4), (-62, 3), (-74, 2), (-86, 1)):
        if rssi >= lo:
            level = n
            break
    w = rect.w / 4.0
    for i in range(4):
        h = rect.h * (0.35 + 0.2 * i)
        bar = pygame.Rect(rect.x + i * w, rect.bottom - h, w * 0.66, h)
        pygame.draw.rect(surf, T.GOOD if i < level else T.LINE, bar, border_radius=2)


def wrap_text(font, text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if font.size(trial)[0] <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def angle_lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def polar(cx: float, cy: float, r: float, deg: float) -> tuple[float, float]:
    a = math.radians(deg)
    return cx + r * math.cos(a), cy - r * math.sin(a)
