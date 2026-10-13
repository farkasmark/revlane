"""GUI behaviour: simulated taps, holds and keystrokes against the real scenes.

Runs SDL headless (offscreen driver) with a fake radio, so this exercises the
exact event path a finger takes without needing a car or a display.
"""

import pygame
from conftest import ADDR, FOUND, click, finger, key_ev, pump

from qcar.gui.drive import DriveScene
from qcar.gui.garage import GarageScene
from qcar.protocol import Command, Telemetry

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
    assert prof.speed_default == 100


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


def _drive(make_app, name="Test Car", **kw):
    app = make_app()
    pump(app, 3)
    prof = app.garage.add(name, ADDR, **kw)
    app.drive(prof)
    pump(app, 2)
    return app, prof


def test_connect_happens_on_drive(make_app):
    app, _prof = _drive(make_app)
    assert app.backend.connects == [ADDR]
    assert isinstance(app.scene, DriveScene)


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
