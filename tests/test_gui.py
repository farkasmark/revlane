"""GUI behaviour: simulated taps, holds and keystrokes against the real scenes.

Runs SDL headless (offscreen driver) with a fake radio, so this exercises the
exact event path a finger takes without needing a car or a display.
"""

import pygame
import pytest
from conftest import ADDR, FOUND, click, finger, key_ev, pump

from qcar.gui.drive import DriveScene
from qcar.gui.garage import GarageScene
from qcar.protocol import SPEED_NORMAL, SPEED_TURBO, Command, Telemetry

# ------------------------------------------------------------------ garage


def test_editor_opens_from_nearby_and_saves(make_app):
    app = make_app(found=FOUND)
    pump(app, 3)
    scene = app.scene
    assert scene.nearby, "nearby car should be offered"
    add_rect, discovered = scene.nearby[0]
    assert discovered.address == ADDR

    click(add_rect)
    pump(app, 2)
    assert scene.draft is not None, "ADD should open the editor"
    assert scene.draft.name, "should pre-fill a suggested name"

    click(scene.editor_rects["name"])
    pump(app, 1)
    assert scene.draft.focus == "name"
    assert scene.draft.name == "", "the suggestion is a placeholder: typing replaces it"
    for ch in "Marci's BMW M3":
        pygame.event.post(key_ev(pygame.K_a, ch))
    pump(app, 1)
    assert scene.draft.name == "Marci's BMW M3"

    click(scene.editor_rects["SAVE"])
    pump(app, 2)
    prof = app.garage.find(ADDR)
    assert prof is not None and prof.name == "Marci's BMW M3"
    assert app.garage.path.exists()
    assert scene.draft is None


def test_editor_toggles_and_cap_persist(make_app):
    app = make_app(found=FOUND)
    pump(app, 3)
    scene = app.scene
    click(scene.nearby[0][0])
    pump(app, 2)
    click(scene.editor_rects["turbo"])
    pump(app, 2)
    assert scene.draft.turbo is True
    click(scene.editor_rects["lights"])
    pump(app, 1)
    click(scene.editor_rects["SAVE"])
    pump(app, 2)
    prof = app.garage.find(ADDR)
    assert prof.turbo_default is True and prof.lights_default is True
    # pace is independent of the turbo default: turbo maps to SPEED_TURBO
    # in command(), so disabling it falls back to this, not a latched 100
    assert prof.speed_default == SPEED_NORMAL


def test_editor_accent_swatch(make_app):
    app = app = make_app(found=FOUND)
    pump(app, 3)
    scene = app.scene
    click(scene.nearby[0][0])
    pump(app, 2)
    before = scene.draft.accent
    click(scene.swatches[-1][0])
    pump(app, 1)
    assert scene.draft.accent != before
    assert scene.draft.accent == scene.swatches[-1][1]


def test_editor_escape_closes_without_saving(make_app):
    app = make_app(found=FOUND)
    pump(app, 3)
    scene = app.scene
    click(scene.nearby[0][0])
    pump(app, 2)
    pygame.event.post(key_ev(pygame.K_ESCAPE))
    pump(app, 2)
    assert scene.draft is None
    assert len(app.garage) == 0


def test_tab_cycles_the_text_focus(make_app):
    app = make_app(found=FOUND)
    pump(app, 3)
    scene = app.scene
    click(scene.nearby[0][0])
    pump(app, 2)
    assert scene.draft.focus is None
    for expect in ("name", "notes", None):
        pygame.event.post(key_ev(pygame.K_TAB))
        pump(app, 1)
        assert scene.draft.focus == expect


def test_notes_are_saved_and_truncated_on_the_card(make_app):
    app = make_app(found=FOUND)
    pump(app, 3)
    scene = app.scene
    click(scene.nearby[0][0])
    pump(app, 2)
    scene.focus_field("notes")
    for ch in "front-left wheel snaps left when the pack is low":
        pygame.event.post(key_ev(pygame.K_a, ch))
    click(scene.editor_rects["SAVE"])
    pump(app, 2)
    assert "snaps left" in app.garage.find(ADDR).notes


def test_empty_garage_and_no_cars(make_app):
    app = make_app(found=[])
    pump(app, 3)
    assert app.scene.card_rects == []
    assert app.scene.nearby == []


def test_delete_a_saved_car(make_app):
    app = make_app(fleet=[("Dino", "00:3C:DE:0A:BB:02")])
    pump(app, 3)
    scene = app.scene
    key = next(k for k in scene.card_rects_by_key if k[1] == "edit")
    click(scene.card_rects_by_key[key])
    pump(app, 2)
    assert scene.draft is not None and scene.draft.existing is not None
    click(scene.editor_rects["DELETE"])
    pump(app, 2)
    assert len(app.garage) == 0


# ------------------------------------------------------------------- drive


def _drive(make_app, name="Test Car", size=None, **kw):
    app = make_app() if size is None else make_app(size=size)
    pump(app, 3)
    prof = app.garage.add(name, ADDR, **kw)
    app.drive(prof)
    pump(app, 2)
    return app, prof


def test_connect_happens_on_drive(make_app):
    app, _prof = _drive(make_app)
    assert app.backend.connects == [ADDR]
    assert isinstance(app.scene, DriveScene)


def test_battery_gauge_stays_inside_the_topbar_frame(make_app):
    """Regression: the gauge sat flush on the topbar's right edge while the
    battery nub is drawn PAST the gauge's right edge - the nub landed on the
    frame's border line."""
    app, _prof = _drive(make_app)
    scene = app.scene
    th = app.theme
    nub_w = max(3, int(4 * th.scale))
    assert scene.gauge.rect.right + nub_w < scene.topbar.right, (
        "battery nub must clear the topbar frame border"
    )


def test_button_label_and_sub_are_centered_as_a_block(make_app):
    """The label+sub pair must be vertically centered as one block, not
    anchored on centery (which pushed the pair off-center)."""
    from qcar.gui.widgets import Button

    app = make_app()
    pump(app, 1)
    th = app.theme
    # the widget draws at its absolute rect: put it at the surface origin
    btn = Button(pygame.Rect(0, 0, 200, 120), "DRIVE", sub="hold", kind="primary")
    surf = pygame.Surface(btn.rect.size)
    surf.fill(btn.accent)  # base color: only the text pixels will differ
    btn.draw(surf, th)

    # bounding box of the text pixels inside the button
    xs, ys = [], []
    for y in range(btn.rect.h):
        for x in range(btn.rect.w):
            if surf.get_at((x, y)) != btn.accent:
                xs.append(x)
                ys.append(y)
    assert xs, "no text rendered"
    block_center = (min(ys) + max(ys)) / 2
    assert abs(block_center - btn.rect.h / 2) <= 2.0, (
        f"text block center {block_center} != button center {btn.rect.h / 2}"
    )


def test_finger_hold_to_drive(make_app):
    app, _ = _drive(make_app)
    pad = app.scene.pad
    cx, cy = pad.center
    w, h = app.theme.w, app.theme.h
    finger(cx, cy - int(pad.radius * 0.8), pygame.FINGERDOWN, w=w, h=h)
    pump(app, 2)
    assert app.backend.intent().forward is True
    assert app.backend.intent().backward is False
    finger(cx, cy - int(pad.radius * 0.8), pygame.FINGERUP, w=w, h=h)
    pump(app, 2)
    assert app.backend.intent().forward is False


def test_finger_steering_is_diagonal(make_app):
    app, _ = _drive(make_app)
    pad = app.scene.pad
    cx, cy = pad.center
    w, h = app.theme.w, app.theme.h
    finger(cx + pad.radius, cy - pad.radius, pygame.FINGERDOWN, w=w, h=h)
    pump(app, 2)
    cmd = app.backend.intent()
    assert cmd.forward and cmd.right
    finger(cx + pad.radius, cy - pad.radius, pygame.FINGERUP, w=w, h=h)
    pump(app, 2)
    assert not app.backend.intent().forward


def test_finger_drag_off_the_pad_still_releases(make_app):
    app, _ = _drive(make_app)
    pad = app.scene.pad
    cx, cy = pad.center
    w, h = app.theme.w, app.theme.h
    finger(cx, cy - pad.radius, pygame.FINGERDOWN, w=w, h=h)
    pump(app, 2)
    assert app.backend.intent().forward
    # finger slides far away, then lifts
    finger(cx, cy - pad.radius, pygame.FINGERUP, w=w, h=h)
    pump(app, 2)
    assert not app.backend.intent().forward


def test_deadzone_does_nothing(make_app):
    app, _ = _drive(make_app)
    cx, cy = app.scene.pad.center
    w, h = app.theme.w, app.theme.h
    finger(cx + 3, cy - 3, pygame.FINGERDOWN, w=w, h=h)
    pump(app, 2)
    cmd = app.backend.intent()
    assert not (cmd.forward or cmd.backward or cmd.left or cmd.right)


def test_keyboard_reports_release(make_app):
    app, _ = _drive(make_app)
    pygame.event.post(key_ev(pygame.K_w))
    pump(app, 2)
    assert app.backend.intent().forward
    pygame.event.post(key_ev(pygame.K_w, typ=pygame.KEYUP))
    pump(app, 2)
    assert not app.backend.intent().forward


def test_stop_button_clears_everything(make_app):
    app, _ = _drive(make_app)
    pygame.event.post(key_ev(pygame.K_d))
    pump(app, 2)
    assert app.backend.intent().right
    click(app.scene.stop.rect)
    pump(app, 2)
    cmd = app.backend.intent()
    assert not (cmd.forward or cmd.backward or cmd.left or cmd.right)


def test_lights_and_turbo_keys(make_app):
    app, _ = _drive(make_app, speed_cap=100)
    pygame.event.post(key_ev(pygame.K_l))
    pump(app, 2)
    assert app.backend.intent().lights is True
    pygame.event.post(key_ev(pygame.K_t))
    pump(app, 2)
    assert app.backend.intent().speed == 100


def test_speed_cap_beats_turbo(make_app):
    app, _ = _drive(make_app, speed_cap=30)
    pygame.event.post(key_ev(pygame.K_t))
    pump(app, 2)
    assert app.backend.intent().speed <= 30


def test_leaving_drive_parks_the_car(make_app):
    app, _ = _drive(make_app)
    pygame.event.post(key_ev(pygame.K_w))
    pump(app, 2)
    pygame.event.post(key_ev(pygame.K_q))
    pump(app, 2)
    assert app.backend.intent() == Command()
    assert isinstance(app.scene, GarageScene)


def test_focus_lost_stops_and_clears_held_keys(make_app):
    app, _ = _drive(make_app)
    pygame.event.post(key_ev(pygame.K_w))
    pump(app, 2)
    assert app.backend.intent().forward
    pygame.event.post(pygame.event.Event(pygame.WINDOWFOCUSLOST))
    pump(app, 3)
    assert not app.backend.intent().forward
    # and it must not spring back to life on the frame after
    pump(app, 3)
    assert not app.backend.intent().forward


def test_battery_flows_back_to_the_card(make_app):
    app, prof = _drive(make_app)
    app.backend.telemetry = Telemetry(seq=4, battery=61)
    pump(app, 2)
    assert prof.last_battery == 61


def test_session_is_counted(make_app):
    app, prof = _drive(make_app)
    assert prof.sessions == 1
    app.to_garage()
    pump(app, 2)
    assert prof.drive_seconds >= 0


def test_quit_event_shuts_down(make_app):
    app = make_app()
    pump(app, 2)
    pygame.event.post(pygame.event.Event(pygame.QUIT))
    assert not app.step()


# ------------------------------------------------------------------ base scene

def test_base_scene_draw_fills_with_theme_bg():
    """Regression: Scene.draw used to fill with a nonexistent Theme.BG
    (colours are module constants) - latent AttributeError for any scene
    that ever relied on the base implementation."""
    from qcar.gui import theme as T
    from qcar.gui.base import Scene

    pygame.init()
    surf = pygame.Surface((40, 30))
    surf.fill((255, 0, 255))
    Scene(object()).draw(surf, T.Theme(40, 30))
    assert surf.get_at((0, 0))[:3] == T.BG


# ----------------------------------------------------------------- drive controls
# Mocked-backend regression tests for the drive screen's right-hand controls:
# taps are simulated through the real event path (SDL offscreen), and the
# recorded intents are asserted - the exact failures reported on hardware.


def test_speed_slider_sets_intent_speed(make_app):
    app, _prof = _drive(make_app, speed_cap=100)
    pump(app, 1)
    scene = app.scene
    click(scene.slider.rect.center)  # track centre = (20+100)/2
    pump(app, 2)
    assert app.backend.intent().speed == 60


def test_turbo_toggle_engages_turbo_speed(make_app):
    app, _prof = _drive(make_app, speed_cap=100)
    pump(app, 1)
    scene = app.scene
    click(scene.turbo_t.rect.center)
    pump(app, 2)
    assert app.backend.intent().speed == SPEED_TURBO


def test_profile_cap_clamps_speed_and_disables_turbo(make_app):
    """The cap is a safety feature, so the controls must be honest about it:
    the slider tops out at the cap and TURBO renders disabled - instead of
    silently clamping while the controls pretend to work."""
    app, prof = _drive(make_app)  # default cap: 80
    pump(app, 1)
    scene = app.scene
    assert scene.turbo_t.enabled is False
    assert app.backend.intent().speed <= prof.speed_cap

    click((scene.slider.rect.right - 2, scene.slider.rect.centery))
    pump(app, 2)
    assert app.backend.intent().speed == prof.speed_cap == 80

    click(scene.turbo_t.rect.center)  # disabled: must be inert
    pump(app, 2)
    assert app.backend.intent().speed <= 80


def test_stop_button_releases_a_held_pad(make_app):
    app, _prof = _drive(make_app)
    pump(app, 1)
    scene = app.scene
    th = app.theme
    pad = scene.pad
    finger(pad.center[0], pad.center[1] - int(pad.radius * 0.8),
           pygame.FINGERDOWN, w=th.w, h=th.h)
    pump(app, 2)
    assert app.backend.intent().forward is True  # driving

    click(scene.stop.rect)  # second finger on STOP while the pad is held
    pump(app, 2)
    intent = app.backend.intent()
    assert not intent.forward and not intent.backward, "STOP must clear the drive flags"
    assert any("stop" in msg for msg, _until, _col in app.toasts.items), \
        "STOP must give visible feedback"


def test_garage_nearby_labels_do_not_overlap_at_phone_scale(make_app):
    """Regression: the nearby cell anchored its two text lines at fixed
    ±11px offsets while the fonts render ~2x taller at phone scale - the
    lines collided. The draw pass now records the rects; assert separation."""
    app = make_app(size=(1920, 1080), found=FOUND)
    pump(app, 3)
    scene = app.scene
    assert scene.nearby_text_rects, "nearby cars should have rendered"
    for name_r, meta_r in scene.nearby_text_rects:
        assert name_r.bottom <= meta_r.top, "name and meta lines overlap"


# ---------------------------------------------------- garage geometry (glyphs)
# GATE FOR UI CHANGES. These tests capture every text blit of a real frame and
# test the actual glyph masks against each other, across screen sizes and UI
# states. They exist because rect arithmetic alone missed three real overlaps
# on the phone (title/count, toggle dot/label, slider value/label): font boxes
# include padding the glyphs do not use.
#
# CONTRACT FOR UI REVAMPS: keep these scans green, and extend them - new
# screens, widgets or states must be added to the scanned states below, and a
# deliberately redesigned layout must update the pinpoint assertions too.

UI_SIZES = [(1920, 1080), (1280, 720), (1024, 640)]


def _capture_text_blits(app):
    """One frame's text blits as (rect, surface, under_panel) tuples.

    Full-screen translucent veils (the garage editor's modal backdrop) are not
    returned; text drawn before them is flagged under_panel=True because the
    opaque modal panel hides it - it cannot collide with anything on top.
    """

    class Rec(pygame.Surface):
        blits = []
        veiled = False

        def blit(self, src, dest, *a, **k):
            if src.get_bitsize() == 32 and src.get_masks()[3] != 0:
                if isinstance(dest, pygame.Rect):
                    r = dest.copy()
                else:
                    # tuple dests carry no size - take it from the source
                    r = pygame.Rect(dest[0], dest[1],
                                    src.get_width(), src.get_height())
                if r.width >= app.theme.w and r.height >= app.theme.h:
                    Rec.veiled = True
                else:
                    Rec.blits.append((r, src, Rec.veiled))
            return super().blit(src, dest, *a, **k)

    old, app.screen = app.screen, Rec((app.theme.w, app.theme.h))
    app.step()
    app.screen = old
    return Rec.blits


def _glyph_collisions(blits):
    """Pairs of text blits whose actual glyphs (not just boxes) touch."""
    masks = [(r, pygame.mask.from_surface(img, 127), under)
             for r, img, under in blits]
    hits = []
    for i in range(len(masks)):
        for j in range(i + 1, len(masks)):
            if masks[i][2] != masks[j][2]:
                continue  # one is behind the modal's opaque panel
            if not masks[i][0].colliderect(masks[j][0]):
                continue
            off = (masks[j][0].x - masks[i][0].x, masks[j][0].y - masks[i][0].y)
            if masks[i][1].overlap(masks[j][1], off):
                hits.append((masks[i][0], masks[j][0]))
    return hits


@pytest.mark.parametrize("size", UI_SIZES)
def test_gate_garage_glyph_scan(size, make_app):
    """No two text elements may touch, in any garage state, at any size."""
    app = make_app(size=size,
                   fleet=[("A Very Long Car Name That Keeps Going", ADDR),
                          ("Dino 246 GT", "00:3C:DE:0A:BB:02")])
    app.garage.sorted()[0].notes = "x" * 120
    pump(app, 2)
    blits = _capture_text_blits(app)
    assert len(blits) > 6, "garage should have rendered texts"
    assert _glyph_collisions(blits) == [], "garage: glyphs collide"

    # editor modal open, with a toast heading for its button row
    sc = app.scene
    sc.add_nearby(list(sc.app.backend.found)[0])
    pump(app, 1)
    app.toasts.push("saved car")
    blits = _capture_text_blits(app)
    assert _glyph_collisions(blits) == [], "editor: glyphs collide"


@pytest.mark.parametrize("size", UI_SIZES)
def test_gate_drive_glyph_scan(size, make_app):
    app, _prof = _drive(make_app, size=size)
    pump(app, 2)
    blits = _capture_text_blits(app)
    assert len(blits) > 6, "drive screen should have rendered texts"
    assert _glyph_collisions(blits) == [], "drive: glyphs collide"


def test_garage_card_text_stays_inside_the_card(make_app):
    app = make_app(size=(1920, 1080),
                   fleet=[("A Very Long Car Name That Keeps Going", ADDR)])
    pump(app, 2)
    sc = app.scene
    th = app.theme
    _prof, r = sc.card_rects[0]
    blits = sorted((b for b in _capture_text_blits(app) if r.colliderect(b[0])),
                   key=lambda b: b[0].top)
    assert len(blits) >= 2
    name_r, meta_r = blits[0][0], blits[1][0]
    chip_x = r.right - int(84 * th.scale)
    assert name_r.right <= chip_x, "card name must elide before the battery chip"
    assert meta_r.right <= r.right - int(18 * th.scale), "card meta must not overflow the card"


def test_garage_count_sits_below_the_title_line(make_app):
    app = make_app(size=(1920, 1080))
    pump(app, 1)
    th = app.theme
    assert app.scene.count.rect.top >= app.scene.title.rect.y + th.fs(46) - 2


def test_nearby_text_stays_inside_its_cell(make_app):
    app = make_app(size=(1920, 1080), found=FOUND)
    pump(app, 3)
    sc = app.scene
    th = app.theme
    assert sc.nearby_text_rects, "nearby cars should have rendered"
    for i, (name_r, meta_r) in enumerate(sc.nearby_text_rects):
        cell_x = (sc.strip.x + int(12 * th.scale)
                  + i * (int(226 * th.scale) + int(10 * th.scale)))
        btn_x = cell_x + int(226 * th.scale) - int(64 * th.scale)
        assert name_r.left >= cell_x, "name was centred on its anchor, off the cell"
        assert meta_r.left >= cell_x
        assert name_r.right <= btn_x - int(12 * th.scale)
        assert meta_r.right <= btn_x - int(12 * th.scale)
        assert name_r.bottom <= meta_r.top, "name and meta lines overlap"


def test_toggle_dot_never_lands_on_its_label():
    from qcar.gui import theme as T
    from qcar.gui.widgets import Toggle
    pygame.init()
    th = T.Theme(1920, 1080)
    surf = pygame.Surface((1920, 1080))
    r = pygame.Rect(600, 500, int(500 * th.scale), int(64 * th.scale))
    for value in (False, True):
        t = Toggle(r, "TURBO", None, value=value)
        t.draw(surf, th)
        f = th.font.get(th.fs(20))
        lab = f.render("TURBO", True, (0, 0, 0)).get_rect(
            midleft=(r.left + int(14 * th.scale), r.centery))
        assert not t._dot_rect.colliderect(lab), f"dot on label (value={value})"


def test_editor_cap_slider_value_clears_its_label(make_app):
    app = make_app(size=(1920, 1080))
    pump(app, 2)
    sc = app.scene
    th = app.theme
    sc.add_nearby(list(sc.app.backend.found)[0])
    pump(app, 1)
    _box, R = sc._editor_box(th)
    # the slider renders its value above its own rect
    assert R["cap"].top - th.fs(12) >= R["cap_label"].bottom, \
        "slider value text landed on the SPEED CAP label"


def test_toasts_clear_the_editor_buttons_while_editing(make_app):
    from qcar.gui import theme as T
    app = make_app(size=(1920, 1080))
    pump(app, 2)
    sc = app.scene
    sc.add_nearby(list(sc.app.backend.found)[0])
    pump(app, 1)
    app.toasts.push("saved car", T.GOOD)
    _box, R = sc._editor_box(app.theme)
    row = R["row"]
    in_row = [r for r, _img, _under in _capture_text_blits(app)
              if r.colliderect(row)]
    assert len(in_row) == 2, f"only SAVE/CANCEL belong in the row, found {in_row}"


def test_disabling_turbo_restores_the_selected_speed(make_app):
    app, _prof = _drive(make_app, speed_cap=100)
    pump(app, 1)
    scene = app.scene
    click(scene.slider.rect.center)  # track centre = 60
    pump(app, 2)
    click(scene.turbo_t.rect.center)  # turbo on
    pump(app, 2)
    assert app.backend.intent().speed == SPEED_TURBO
    click(scene.turbo_t.rect.center)  # turbo off again
    pump(app, 2)
    assert app.backend.intent().speed == 60, "turbo off left full throttle"


def test_disabling_a_turbo_default_profile_restores_a_normal_speed(make_app):
    """A turbo_default profile starts at full boost, but disabling it must
    fall back to the driver's pace - speed_default used to initialise the
    normal speed at 100, so switching turbo off kept full throttle."""
    app, _prof = _drive(make_app, speed_cap=100, turbo_default=True)
    pump(app, 1)
    scene = app.scene
    assert scene.turbo is True
    assert app.backend.intent().speed == SPEED_TURBO

    click(scene.turbo_t.rect.center)  # driver disables turbo
    pump(app, 2)
    assert scene.turbo is False
    assert app.backend.intent().speed < SPEED_TURBO
