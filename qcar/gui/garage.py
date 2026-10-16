"""GARAGE scene: scan, save, remember, drive.

A "draft" is the modal editor state. It binds an address (found or known) to
the human facts you want to keep: a name, an accent colour, a speed cap and
whether turbo/lights should be on when you get in.
"""

from __future__ import annotations

import dataclasses
import time

import pygame

from ..profiles import ACCENTS, Profile
from . import theme as T
from .backend import Discovered
from .base import Scene
from .widgets import Button, Label, Slider, Toggle, fit_text, rounded, wrap_text


@dataclasses.dataclass
class Draft:
    address: str
    name: str = ""
    notes: str = ""
    accent: str = ACCENTS[0]
    cap: int = 80
    turbo: bool = False
    lights: bool = False
    existing: Profile | None = None
    focus: str | None = None  # None | "name" | "notes"
    suggested: bool = False  # name is our guess, so typing should replace it
    hint: str = ""            # shown while a field is empty

    @property
    def is_new(self) -> bool:
        return self.existing is None


class GarageScene(Scene):
    def __init__(self, app) -> None:
        super().__init__(app)
        self.draft: Draft | None = None
        self.page = 0
        self.pager = None
        self.nearby: list[tuple[pygame.Rect, Discovered]] = []
        self.swatches: list[tuple[pygame.Rect, str]] = []
        self.editor_hits: list[tuple[pygame.Rect, object]] = []
        self.card_hits: list[tuple[pygame.Rect, object]] = []
        self.card_rects_by_key: dict[tuple[str, str], pygame.Rect] = {}
        self.editor_rects: dict[str, pygame.Rect] = {}
        self.cap_slider: Slider | None = None

    # ------------------------------------------------------------------

    def enter(self) -> None:
        self.app.backend.scan(4.0)

    def layout(self, th: T.Theme) -> None:
        self.th = th
        pad = th.pad
        head = pygame.Rect(pad, pad, th.w - 2 * pad, th.fs(46))
        self.title = Label(head, "GARAGE", T.TEXT, 40)
        n = len(self.app.garage)
        # the count sits BELOW the title's line, not inside the same fixed
        # head rect: both rendered in one fs(46) block and the glyphs
        # collided at phone font scales
        self.count = Label(
            pygame.Rect(head.x, head.y + th.fs(46) + int(6 * th.scale),
                        head.w, th.fs(22)),
            f"{n} car{'s' if n != 1 else ''} in the garage",
            T.DIM,
            20,
        )
        self.scan_btn = Button(
            pygame.Rect(th.w - pad - int(140 * th.scale), pad, int(140 * th.scale), th.touch_min()),
            "SCAN",
            self.on_scan,
            size=22,
        )
        self.widgets = [self.title, self.count, self.scan_btn]

        strip_h = int(84 * th.scale)
        self.strip = pygame.Rect(pad, self.count.rect.bottom + int(30 * th.scale),
                                 th.w - 2 * pad, strip_h)

        top = self.strip.bottom + int(36 * th.scale)
        cols = 1 if th.w < 700 else (2 if th.w < 1180 else 3)
        gap = pad
        bottom_reserve = int(64 * th.scale)
        avail = max(strip_h, th.h - top - pad - bottom_reserve)
        # Page instead of overflow: a phone in landscape only has room for one
        # row, and a card shorter than ~130 px stops being readable anyway.
        min_card_h = int(132 * th.scale)
        rows = max(1, int((avail + gap) // (min_card_h + gap)))
        card_h = int(max(min_card_h, min(208 * th.scale, (avail - (rows - 1) * gap) / rows)))
        card_w = (th.w - 2 * pad - (cols - 1) * gap) // cols
        page_size = max(1, cols * rows)
        all_cars = self.app.garage.sorted()
        pages = max(1, -(-len(all_cars) // page_size))
        self.page = max(0, min(self.page, pages - 1))
        visible = all_cars[self.page * page_size : (self.page + 1) * page_size]
        self.pager = None
        if pages > 1:
            pw = int(116 * th.scale)
            ph = int(44 * th.scale)
            self.pager = (
                pygame.Rect(pad, th.h - pad - ph, pw, ph),
                pygame.Rect(th.w - pad - pw, th.h - pad - ph, pw, ph),
                f"page {self.page + 1} of {pages}",
            )
            self.widgets += [
                Button(self.pager[0], "< PREV", self.prev_page, size=18),
                Button(self.pager[1], "NEXT >", self.next_page, size=18),
            ]
        self.card_rects: list[tuple[Profile, pygame.Rect]] = []
        self.card_hits = []
        self.card_rects_by_key = {}
        for i, prof in enumerate(visible):
            r = pygame.Rect(
                pad + (i % cols) * (card_w + gap),
                top + (i // cols) * (card_h + gap),
                card_w,
                card_h,
            )
            self.card_rects.append((prof, r))
            drive = pygame.Rect(r.right - int(94 * th.scale), r.bottom - int(54 * th.scale),
                                int(86 * th.scale), int(42 * th.scale))
            edit = pygame.Rect(r.x + int(12 * th.scale), r.bottom - int(54 * th.scale),
                               int(72 * th.scale), int(42 * th.scale))
            self.card_hits.append((drive, lambda p=prof: self.app.drive(p)))
            self.card_hits.append((edit, lambda p=prof: self.edit_profile(p)))
            self.card_rects_by_key[(prof.id, "drive")] = drive
            self.card_rects_by_key[(prof.id, "edit")] = edit

    # ------------------------------------------------------------------
    # actions

    def on_scan(self) -> None:
        self.app.backend.scan(4.0)
        self.app.toasts.push("scanning...")

    def prev_page(self) -> None:
        self.page = max(0, self.page - 1)
        self.layout(self.app.theme)

    def next_page(self) -> None:
        self.page += 1
        self.layout(self.app.theme)

    def edit_profile(self, prof: Profile) -> None:
        self.draft = Draft(
            address=prof.address,
            name=prof.name,
            notes=prof.notes,
            accent=prof.accent,
            cap=prof.speed_cap,
            turbo=prof.turbo_default,
            lights=prof.lights_default,
            existing=prof,
        )

    def add_nearby(self, found: Discovered) -> None:
        prof = self.app.garage.find(found.address)
        if prof:
            self.edit_profile(prof)
            return
        self.draft = Draft(
            address=found.address,
            name=self.app.garage.suggest_name(found.address),
            accent=ACCENTS[len(self.app.garage) % len(ACCENTS)],
            suggested=True,
            hint=self.app.garage.suggest_name(found.address),
        )

    def focus_field(self, key: str) -> None:
        d = self.draft
        if d is None:
            return
        if key == "name" and d.suggested:
            # our guess is a placeholder: first tap replaces it instead of
            # appending to it, which is what every "prefilled" field does
            d.name = ""
            d.suggested = False
        d.focus = key

    def save_draft(self) -> None:
        d = self.draft
        if d is None:
            return
        name = d.name.strip() or self.app.garage.suggest_name(d.address)
        prof = self.app.garage.add(name, d.address)
        prof.accent = d.accent
        prof.speed_cap = d.cap
        prof.turbo_default = d.turbo
        prof.lights_default = d.lights
        prof.notes = d.notes.strip()
        self.app.garage.save()
        self.app.toasts.push(f"saved {prof.name}", T.GOOD)
        self.draft = None
        self.layout(self.app.theme)

    def delete_draft(self) -> None:
        d = self.draft
        if d is None:
            return
        if d.existing is not None:
            self.app.garage.remove(d.existing.id)
            self.app.garage.save()
            self.app.toasts.push(f"removed {d.existing.name}", T.WARN)
        self.draft = None
        self.layout(self.app.theme)

    # ------------------------------------------------------------------
    # input

    def pointer_down(self, pos) -> bool:
        if self.draft is not None:
            for rect, action in self.editor_hits:
                if rect.collidepoint(pos):
                    action()
                    return True
            for rect, hexc in self.swatches:
                if rect.collidepoint(pos):
                    self.draft.accent = hexc
                    return True
            if self.cap_slider is not None and self.cap_slider.rect.collidepoint(pos):
                self.cap_slider.on_down(pos)
                return True
            for key in ("name", "notes"):
                if self._fr(key).collidepoint(pos):
                    self.focus_field(key)
                    return True
            return True  # modal: swallow everything
        for w in self.widgets:  # SCAN and the pager buttons
            if w.on_down(pos):
                return True
        for rect, action in self.card_hits:
            if rect.collidepoint(pos):
                self._pressed = (rect, action)
                return True
        for rect, found in self.nearby:
            if rect.collidepoint(pos):
                self._pressed = (rect, lambda f=found: self.add_nearby(f))
                return True
        return False

    def pointer_up(self, pos) -> bool:
        if self.draft is not None:
            if self.cap_slider is not None:
                self.cap_slider.on_up(pos)
            return True
        for w in self.widgets:
            if w.on_up(pos):
                return True
        hit = getattr(self, "_pressed", None)
        self._pressed = None
        if hit is not None:
            rect, action = hit
            if rect.collidepoint(pos):
                action()
            return True
        return False

    def key_down(self, ev) -> bool:
        d = self.draft
        if d is None:
            if ev.key == pygame.K_r:
                self.on_scan()
                return True
            return False
        if ev.key == pygame.K_ESCAPE:
            self.draft = None
            return True
        if ev.key == pygame.K_TAB:
            d.focus = {"name": "notes", "notes": None, None: "name"}[d.focus]
            return True
        if ev.key in (pygame.K_BACKSPACE, pygame.K_DELETE):
            if d.focus == "notes":
                d.notes = d.notes[:-1]
            elif d.focus == "name":
                d.name = d.name[:-1]
            return True
        if ev.key == pygame.K_RETURN:
            if d.focus is None:
                self.save_draft()
            else:
                d.focus = None
            return True
        # pygame puts the typed character in KEYDOWN.unicode (TEXTINPUT also
        # exists, but unicode is what is present on both paths)
        ch = getattr(ev, "unicode", "") or getattr(ev, "text", "") or ""
        if ch.isprintable():
            if d.focus == "name":
                d.name += ch
            elif d.focus == "notes":
                d.notes += ch
            return True
        return False

    # ------------------------------------------------------------------
    # drawing

    def draw(self, surf, th: T.Theme) -> None:
        surf.fill(T.BG)
        _background_grid(surf, th)
        self.toast_top = None
        for w in self.widgets:
            w.draw(surf, th)
        self._draw_nearby(surf, th)
        self._draw_cards(surf, th)
        if self.draft is not None:
            self._draw_editor(surf, th)

    def _draw_nearby(self, surf, th: T.Theme) -> None:
        r = self.strip
        lf = th.font.get(th.fs(18))
        state = self.app.backend.state
        found = list(self.app.backend.found)
        surf.blit(lf.render("NEARBY" + ("  (scanning...)" if state == "scanning" else ""),
                            True, T.DIM), (r.x, r.y - int(24 * th.scale)))
        rounded(surf, r, T.PANEL, th.radius, border=1, border_color=T.LINE)
        self.nearby = []
        self.nearby_text_rects = []
        if not found:
            # do not accuse the car of being off while we are still listening,
            # and never swallow a failed scan - state == ERROR used to be
            # indistinguishable from an empty scan, which hid real failures
            err = self.app.backend.error
            if state == "scanning":
                msg, col = "listening for cars...", T.FAINT
            elif state == "error" and err:
                msg, col = f"scan failed: {err}", T.WARN
            else:
                msg, col = "no cars in range - is one switched on?", T.FAINT
            mf = th.font.get(th.fs(21))
            img = mf.render(msg, True, col)
            surf.blit(img, img.get_rect(center=r.center))
            return
        x = r.x + int(12 * th.scale)
        for d in found[: max(1, (r.w - int(24 * th.scale)) // int(236 * th.scale))]:
            w = int(226 * th.scale)
            cell = pygame.Rect(x, r.y + int(10 * th.scale), w, r.h - int(20 * th.scale))
            prof = self.app.garage.find(d.address)
            col = T.hex_rgb(prof.accent) if prof else T.FAINT
            rounded(surf, cell, T.PANEL_2, int(10 * th.scale))
            pygame.draw.rect(surf, col, (cell.x, cell.y + int(8 * th.scale), int(4 * th.scale),
                                         cell.h - int(16 * th.scale)), border_radius=2)
            btn = pygame.Rect(cell.right - int(64 * th.scale), cell.centery - int(15 * th.scale),
                              int(56 * th.scale), int(30 * th.scale))
            nf = th.font.get(th.fs(19))
            label = (prof.name if prof else d.label)[:20]
            # left-anchored lines stacked around the cell centre, elided
            # before the EDIT/ADD button: midbottom/midtop centred the text
            # on the anchor (half of it off the cell) and fixed offsets
            # ignored the rendered font sizes
            tx = cell.x + int(14 * th.scale)
            max_w = btn.x - int(12 * th.scale) - tx
            name_img = nf.render(fit_text(nf, label, max_w), True, T.TEXT)
            sf = th.font.get(th.fs(15))
            meta = f"{d.address}   {d.rssi} dBm"
            meta_img = sf.render(fit_text(sf, meta, max_w), True, T.DIM)
            # stack the two lines by their measured heights around the cell
            # centre: a fixed gap collides once the fonts render tall
            gap = int(6 * th.scale)
            block = name_img.get_height() + gap + meta_img.get_height()
            top = cell.centery - block // 2
            name_r = name_img.get_rect(
                midleft=(tx, top + name_img.get_height() // 2))
            surf.blit(name_img, name_r)
            meta_r = meta_img.get_rect(
                midleft=(tx, top + name_img.get_height() + gap
                         + meta_img.get_height() // 2))
            surf.blit(meta_img, meta_r)
            self.nearby_text_rects.append((name_r, meta_r))
            Button(btn, "EDIT" if prof else "ADD", None, kind="ghost", size=17).draw(surf, th)
            self.nearby.append((btn, d))
            x += w + int(10 * th.scale)

    def _draw_cards(self, surf, th: T.Theme) -> None:
        if not self.card_rects:
            mf = th.font.get(th.fs(23))
            msg = "No saved cars yet. Press SCAN, then ADD on one you find nearby."
            y = self.strip.bottom + int(64 * th.scale)
            for ln in wrap_text(mf, msg, self.th.w - 4 * th.pad):
                surf.blit(mf.render(ln, True, T.FAINT), (th.pad, y))
                y += th.fs(30)
            return
        for prof, r in self.card_rects:
            self._draw_card(surf, th, prof, r)
        if self.pager is not None:
            pf = th.font.get(th.fs(19))
            img = pf.render(self.pager[2], True, T.DIM)
            surf.blit(img, img.get_rect(center=(th.w // 2, self.pager[0].centery)))

    def _draw_card(self, surf, th: T.Theme, prof: Profile, r: pygame.Rect) -> None:
        accent = T.hex_rgb(prof.accent)
        pygame.draw.rect(surf, T.SHADOW, r.move(0, int(4 * th.scale)), border_radius=th.radius)
        rounded(surf, r, T.PANEL, th.radius, border=1, border_color=T.LINE)
        pygame.draw.rect(surf, accent, (r.x, r.y + th.radius, int(5 * th.scale),
                                        r.h - 2 * th.radius))
        nf = th.font.get(th.fs(25))
        sf = th.font.get(th.fs(15))
        # stack by measured heights: the fixed offsets (26/48/70) ignored the
        # rendered text sizes and the lines collided at phone font scales.
        # name elides before the battery chip, meta before the card edge
        x = r.x + int(18 * th.scale)
        chip_x = r.right - int(84 * th.scale)
        name_img = nf.render(
            fit_text(nf, prof.name[:26], chip_x - x - int(12 * th.scale)), True, T.TEXT)
        meta = f"{prof.address}   seen {prof.last_seen_text}   {prof.odometer}"
        meta_img = sf.render(
            fit_text(sf, meta, r.right - int(18 * th.scale) - x), True, T.DIM)
        y = r.y + int(8 * th.scale)
        surf.blit(name_img, name_img.get_rect(
            midleft=(x, y + name_img.get_height() // 2)))
        y += name_img.get_height() + int(2 * th.scale)
        surf.blit(meta_img, meta_img.get_rect(
            midleft=(x, y + meta_img.get_height() // 2)))
        y += meta_img.get_height() + int(6 * th.scale)
        if prof.notes:
            note = " ".join(wrap_text(sf, prof.notes, r.w - int(40 * th.scale)))[:84]
            note_img = sf.render(note, True, T.FAINT)
            # never run into the bottom button row
            if y + note_img.get_height() <= r.bottom - int(58 * th.scale):
                surf.blit(note_img, (x, y))

        chip = pygame.Rect(r.right - int(84 * th.scale), r.y + int(12 * th.scale),
                           int(72 * th.scale), int(26 * th.scale))
        rounded(surf, chip, T.PANEL_2, int(13 * th.scale))
        bf = th.font.get(th.fs(15))
        txt = f"{prof.last_battery}%" if prof.last_battery is not None else "no data"
        col = T.battery_color(prof.last_battery or 0) if prof.last_battery is not None else T.FAINT
        img = bf.render(txt, True, col)
        surf.blit(img, img.get_rect(center=chip.center))

        badge = pygame.Rect(chip.x, chip.bottom + int(8 * th.scale), int(72 * th.scale),
                            int(26 * th.scale))
        rounded(surf, badge, T.PANEL_2, int(8 * th.scale))
        img = bf.render(f"{prof.speed_cap}% cap", True, T.WARN)
        surf.blit(img, img.get_rect(center=badge.center))

        Button(pygame.Rect(r.x + int(12 * th.scale), r.bottom - int(54 * th.scale),
                           int(72 * th.scale), int(42 * th.scale)), "EDIT", None,
               kind="ghost", size=18).draw(surf, th)
        Button(pygame.Rect(r.right - int(94 * th.scale), r.bottom - int(54 * th.scale),
                           int(86 * th.scale), int(42 * th.scale)), "DRIVE", None,
               kind="primary", accent=accent, size=20).draw(surf, th)

    # ---- editor geometry ------------------------------------------------

    def _editor_box(self, th: T.Theme) -> tuple[pygame.Rect, dict]:
        box = pygame.Rect(0, 0, min(int(560 * th.scale), th.w - 2 * th.pad),
                          min(int(560 * th.scale), th.h - 2 * th.pad))
        box.center = (th.w // 2, th.h // 2)
        r = box.inflate(-int(40 * th.scale), -int(30 * th.scale))
        h = int(44 * th.scale)
        rects: dict[str, pygame.Rect] = {"title": pygame.Rect(r.x, r.y, r.w, th.fs(30))}
        y = r.y + th.fs(46)
        for key in ("name", "notes"):
            rects[key + "_label"] = pygame.Rect(r.x, y, r.w, th.fs(15))
            rects[key] = pygame.Rect(r.x, y + th.fs(19), r.w, h)
            y += h + th.fs(30)
        rects["cap_label"] = pygame.Rect(r.x, y, r.w, th.fs(15))
        # the slider renders its value above its own rect: leave that line
        # free, or "80%" lands on the SPEED CAP label
        rects["cap"] = pygame.Rect(r.x, y + th.fs(34), r.w, int(44 * th.scale))
        y += int(44 * th.scale) + th.fs(46)
        rects["accents"] = pygame.Rect(r.x, y, r.w, int(38 * th.scale))
        y += int(38 * th.scale) + int(14 * th.scale)
        rects["toggles"] = pygame.Rect(r.x, y, r.w, h)
        y += h + int(18 * th.scale)
        rects["row"] = pygame.Rect(r.x, min(y, box.bottom - h), r.w, h)
        return box, rects

    def _fr(self, key: str) -> pygame.Rect:
        return self._editor_box(self.th)[1][key]

    def _draw_editor(self, surf, th: T.Theme) -> None:
        d = self.draft
        assert d is not None
        veil = pygame.Surface((th.w, th.h), pygame.SRCALPHA)
        veil.fill((0, 0, 0, 175))
        surf.blit(veil, (0, 0))
        box, R = self._editor_box(th)
        # while the modal is open, toasts stack from the top: at the screen
        # bottom they land on the SAVE/CANCEL row
        self.toast_top = int(10 * th.scale)
        rounded(surf, box, T.PANEL, th.radius * 2, border=2, border_color=T.LINE)
        f = th.font.get(th.fs(27))
        title = "SAVE CAR" if d.is_new else "EDIT CAR"
        surf.blit(f.render(title, True, T.TEXT), R["title"].topleft)
        af = th.font.get(th.fs(16))
        img = af.render(fit_text(af, d.address, box.w // 2 - int(24 * th.scale)),
                        True, T.DIM)
        surf.blit(img, img.get_rect(midright=(box.right - int(24 * th.scale),
                                              R["title"].centery)))

        blink = int(time.monotonic() * 2) % 2 == 0
        for key in ("name", "notes"):
            lab = R[key + "_label"]
            surf.blit(af.render(key.upper(), True, T.DIM), lab.topleft)
            rect = R[key]
            active = d.focus == key
            rounded(surf, rect, T.PANEL_2, int(8 * th.scale), border=2,
                    border_color=T.WARN if active else T.LINE)
            tf = th.font.get(th.fs(21))
            value = d.name if key == "name" else d.notes
            placeholder = not value and key == "name" and bool(d.hint)
            shown = d.hint if placeholder else value + ("|" if active and blink else "")
            img = tf.render(shown, True, T.FAINT if placeholder else T.TEXT)
            surf.blit(img, img.get_rect(midleft=(rect.x + int(12 * th.scale), rect.centery)))

        surf.blit(af.render("SPEED CAP", True, T.DIM), R["cap_label"].topleft)
        if self.cap_slider is None:
            self.cap_slider = Slider(R["cap"], 20, 100, d.cap, lambda v: setattr(d, "cap", v))
        self.cap_slider.rect = R["cap"]
        self.cap_slider.value = d.cap
        self.cap_slider.on_change = lambda v: setattr(d, "cap", v)
        self.cap_slider.draw(surf, th)

        ar = R["accents"]
        sw = min(int(38 * th.scale), ar.w // len(ACCENTS) - int(6 * th.scale))
        self.swatches = []
        for i, hexc in enumerate(ACCENTS):
            r = pygame.Rect(ar.x + i * (sw + int(7 * th.scale)), ar.y, sw, sw)
            pygame.draw.rect(surf, T.hex_rgb(hexc), r, border_radius=int(8 * th.scale))
            if hexc == d.accent:
                pygame.draw.rect(surf, T.TEXT, r.inflate(6, 6), max(2, int(3 * th.scale)),
                                 border_radius=int(11 * th.scale))
            self.swatches.append((r, hexc))

        tw = (R["toggles"].w - int(10 * th.scale)) // 2
        t1 = pygame.Rect(R["toggles"].x, R["toggles"].y, tw, R["toggles"].h)
        t2 = pygame.Rect(t1.right + int(10 * th.scale), R["toggles"].y, tw, R["toggles"].h)
        Toggle(t1, "TURBO", None, value=d.turbo).draw(surf, th)
        Toggle(t2, "LIGHTS", None, value=d.lights).draw(surf, th)

        self.editor_hits = [
            (t1, lambda: setattr(d, "turbo", not d.turbo)),
            (t2, lambda: setattr(d, "lights", not d.lights)),
        ]
        self.editor_rects = {"turbo": t1, "lights": t2, "accents": R["accents"],
                             "cap": R["cap"], "name": R["name"], "notes": R["notes"]}
        row = R["row"]
        bw = int((row.w - int(18 * th.scale)) / 3)
        specs = [
            ("SAVE", "primary", self.save_draft),
            ("CANCEL", "ghost", lambda: setattr(self, "draft", None)),
        ]
        if d.existing is not None:
            specs.append(("DELETE", "danger", self.delete_draft))
        else:
            bw = int((row.w - int(9 * th.scale)) / 2)
        for i, (lab, kind, cb) in enumerate(specs):
            r = pygame.Rect(int(row.x + i * (bw + int(9 * th.scale))), row.y, bw, row.h)
            Button(r, lab, None, kind=kind, size=21).draw(surf, th)
            self.editor_hits.append((r, cb))
            self.editor_rects[lab] = r


def _background_grid(surf, th: T.Theme) -> None:
    step = int(48 * th.scale)
    for x in range(0, th.w, step):
        pygame.draw.line(surf, T.mix(T.BG, T.PANEL, 0.6), (x, 0), (x, th.h))
    for y in range(0, th.h, step):
        pygame.draw.line(surf, T.mix(T.BG, T.PANEL, 0.6), (0, y), (th.w, y))
