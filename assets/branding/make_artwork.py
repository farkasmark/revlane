#!/usr/bin/env python3
"""revlane branding artwork generator.

Original artwork - a side-view coupe silhouette in the GUI's amber accent on
the dark asphalt background (qcar/gui/theme.py).  Everything is drawn here at
4x supersampling and smoothscaled down, so the PNGs are reproducible from this
script alone; edit the shapes/colours and re-run:

    .venv/bin/python assets/branding/make_artwork.py

Outputs (next to this script):
    icon.png        512x512   legacy launcher icon (full square, rounded)
    icon_fg.png    1080x1080  adaptive-icon foreground (transparent margins,
                              emblem inside the launcher safe zone)
    icon_bg.png    1080x1080  adaptive-icon background (solid asphalt)
    presplash.jpg  1920x1080  p4a loading screen, landscape, emblem + wordmark
    previews/                 small-size render checks (48/96/192 px)
"""

import os
import pathlib

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent

# qcar/gui/theme.py palette
BG = (16, 18, 24)        # dark asphalt
WARN = (255, 179, 0)     # amber accent
TEXT = (232, 236, 244)   # near-white

SS = 4  # supersample factor for antialiasing

# car silhouette, design box 1200x600, ground line at y=470, nose pointing right
BODY = [
    (105, 430), (96, 360), (130, 338), (225, 326), (320, 252), (445, 244),
    (540, 292), (560, 288), (735, 308), (845, 330), (900, 356), (908, 396),
    (890, 430), (105, 430),
]
WINDOWS = [(335, 258), (440, 252), (515, 290), (355, 296)]
WHEELS = [(285, 436), (760, 436)]
WHEEL_R = 66
ARCH_R = 96


def _punch_hole(surf: pygame.Surface, center: tuple[int, int], r: int) -> None:
    """Cut a transparent circle (wheel arch) out of the body.

    pygame.draw writes the raw RGBA (alpha 0 included) on a SRCALPHA
    surface - a transparent-circle draw is the punch.  (Blitting an empty
    SRCALPHA surface with BLEND_RGBA_SUB subtracts (0,0,0,0): a no-op.)
    """
    pygame.draw.circle(surf, (*BG, 0), center, r)


def _self_check() -> None:
    """The arch gap must be see-through and the body solid amber - a silent
    no-op in _punch_hole (or a draw regression) fails loudly here."""
    emblem = draw_emblem(600)
    scale = 600 / 1200
    gap = emblem.get_at((int(285 * scale), int(360 * scale)))   # arch ring
    body = emblem.get_at((int(480 * scale), int(320 * scale)))  # body panel
    assert gap.a == 0, f"wheel arch is not cut out (alpha {gap.a})"
    assert body == (*WARN, 255), f"body is not solid amber: {body}"


def draw_emblem(width: int) -> pygame.Surface:
    """The car emblem on transparency, `width` px wide (aspect 2:1)."""
    design_w, design_h = 1200, 600
    scale = width / design_w
    w, h = int(design_w * scale), int(design_h * scale)
    surf = pygame.Surface((w * SS, h * SS), pygame.SRCALPHA)

    def sc(pts):
        return [((x * scale) * SS, (y * scale) * SS) for x, y in pts]

    pygame.draw.polygon(surf, (*WARN, 255), sc(BODY))
    for wx, wy in WHEELS:
        _punch_hole(surf, (int(wx * scale) * SS, int(wy * scale) * SS),
                    int(ARCH_R * scale) * SS)
    for wx, wy in WHEELS:
        cx, cy = int(wx * scale) * SS, int(wy * scale) * SS
        r = int(WHEEL_R * scale) * SS
        pygame.draw.circle(surf, (*BG, 255), (cx, cy), r)               # tyre
        pygame.draw.circle(surf, (*WARN, 255), (cx, cy), int(r * 0.60),  # rim
                           max(2, int(r * 0.18)))
        pygame.draw.circle(surf, (*WARN, 255), (cx, cy), max(2, int(r * 0.14)))
    pygame.draw.polygon(surf, (*BG, 255), sc(WINDOWS))                  # glass

    return pygame.transform.smoothscale(surf, (w, h))


def _font(size: int):
    for name in ("dejavusansbold", "dejavusans"):
        f = pygame.font.match_font(name)
        if f:
            return pygame.font.Font(f, size)
    f = pygame.font.Font(pygame.font.get_default_font(), size)
    f.set_bold(True)
    return f


def _wordmark(text: str, size: int, color) -> pygame.Surface:
    """Lowercase wordmark with a little letter tracking."""
    font = _font(size)
    tracking = int(size * 0.08)
    imgs = [font.render(ch, True, color) for ch in text]
    total = sum(i.get_width() for i in imgs) + tracking * (len(imgs) - 1)
    h = max(i.get_height() for i in imgs)
    surf = pygame.Surface((total, h), pygame.SRCALPHA)
    x = 0
    for img in imgs:
        surf.blit(img, (x, 0))
        x += img.get_width() + tracking
    return surf


def _canvas(w: int, h: int) -> pygame.Surface:
    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    surf.fill((*BG, 255))
    return surf


def make_legacy_icon(path: pathlib.Path, size: int = 512) -> None:
    surf = _canvas(size, size)
    emblem = draw_emblem(int(size * 0.82))
    surf.blit(emblem, emblem.get_rect(center=(size // 2, int(size * 0.46))))
    mark = _wordmark("revlane", int(size * 0.11), TEXT)
    surf.blit(mark, mark.get_rect(midbottom=(size // 2, int(size * 0.94))))
    pygame.image.save(surf, path)


def make_adaptive_layers(fg_path: pathlib.Path, bg_path: pathlib.Path,
                         size: int = 1080) -> None:
    # background: solid asphalt (launchers may crop/mask anywhere)
    pygame.image.save(_canvas(size, size), bg_path)
    # foreground: emblem inside the safe zone - the outer 25% of an adaptive
    # layer can be cropped, and the mask is circular, so keep the car within
    # the central ~58% of the layer
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    emblem = draw_emblem(int(size * 0.58))
    surf.blit(emblem, emblem.get_rect(center=(size // 2, int(size * 0.52))))
    pygame.image.save(surf, fg_path)


def make_presplash(path: pathlib.Path, w: int = 1920, h: int = 1080) -> None:
    surf = _canvas(w, h)
    emblem = draw_emblem(int(w * 0.30))
    surf.blit(emblem, emblem.get_rect(center=(w // 2, int(h * 0.42))))
    mark = _wordmark("revlane", int(h * 0.115), TEXT)
    surf.blit(mark, mark.get_rect(midtop=(w // 2, int(h * 0.68))))
    pygame.image.save(surf, path)  # p4a serves it as drawable/presplash.jpg


def make_previews(icon_path: pathlib.Path, out_dir: pathlib.Path) -> None:
    out_dir.mkdir(exist_ok=True)
    src = pygame.image.load(str(icon_path))
    for px in (48, 96, 192):
        pygame.image.save(pygame.transform.smoothscale(src, (px, px)),
                          out_dir / f"icon_{px}.png")


def main() -> None:
    pygame.init()
    _self_check()
    make_legacy_icon(HERE / "icon.png")
    make_adaptive_layers(HERE / "icon_fg.png", HERE / "icon_bg.png")
    make_presplash(HERE / "presplash.jpg")
    make_previews(HERE / "icon.png", HERE / "previews")
    print("artwork written to", HERE)


if __name__ == "__main__":
    main()
