"""Layout sanity: nothing overlaps, nothing runs off screen.

Screenshots are not assertable, geometry is. Every interactive rect must be
inside the window, big enough for a finger, and must not sit on top of another
one (an ambiguous tap is a bug you only find in the hallway).
"""

import pytest
from conftest import FLEET, FOUND

SIZES = [(360, 640), (390, 720), (640, 360), (740, 360), (1024, 640), (1280, 800), (1920, 1080)]


def inside(rect, w, h):
    return rect.left >= 0 and rect.top >= 0 and rect.right <= w and rect.bottom <= h


def overlap(a, b):
    return a.inflate(-2, -2).colliderect(b.inflate(-2, -2))


@pytest.mark.parametrize("size", SIZES)
def test_garage_geometry(make_app, size):
    app = make_app(size=size, fleet=FLEET, found=FOUND)
    for _ in range(3):
        assert app.step()
    scene = app.scene
    w, h = app.theme.w, app.theme.h

    named = {f"{k[0]}/{k[1]}": r for k, r in scene.card_rects_by_key.items()}
    named["scan"] = scene.scan_btn.rect
    named.update({f"nearby:{i}": r for i, (r, _f) in enumerate(scene.nearby)})
    assert scene.card_rects_by_key, "at least one car's buttons must be laid out"
    assert len(named) > 2

    for label, rect in named.items():
        assert rect.w > 0 and rect.h > 0, f"{label} collapsed at {size}: {rect}"
        assert inside(rect, w, h), f"{label} outside window at {size}: {rect} vs {w}x{h}"
        assert rect.h >= int(26 * app.theme.scale), f"{label} too short to tap: {rect}"

    keys = list(named)
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            assert not overlap(named[a], named[b]), (
                f"{a} {named[a]} overlaps {b} {named[b]} at {size}"
            )


@pytest.mark.parametrize("size", SIZES)
def test_every_saved_car_is_reachable(make_app, size):
    """Small screens page the garage - but no car may become unreachable."""
    from conftest import click

    app = make_app(size=size, fleet=FLEET, found=FOUND)
    for _ in range(3):
        assert app.step()
    scene = app.scene
    seen: set[str] = set()
    for _ in range(len(FLEET) * 3):  # bounded walk through the pages
        seen.update(pid for pid, _kind in scene.card_rects_by_key)
        if seen == {p.id for p in app.garage}:
            break
        assert scene.pager is not None, (
            f"{size}: cars hidden but no pager offered"
        )
        click(scene.pager[1])
        for _ in range(2):
            assert app.step()
    assert seen == {p.id for p in app.garage}, f"{size}: never reachable: {seen}"


@pytest.mark.parametrize("size", SIZES)
def test_drive_geometry(make_app, size):
    app = make_app(size=size, fleet=FLEET, found=FOUND)
    for _ in range(3):
        assert app.step()
    app.drive(app.garage.sorted()[0])
    for _ in range(3):
        assert app.step()
    scene = app.scene
    w, h = app.theme.w, app.theme.h
    named = {
        "pad": scene.pad.rect,
        "stop": scene.stop.rect,
        "turbo": scene.turbo_t.rect,
        "lights": scene.lights_t.rect,
        "slider": scene.slider.rect,
        "back": scene.back.rect,
        "gauge": scene.gauge.rect,
    }
    for label, rect in named.items():
        assert rect.w > 0 and rect.h > 0, f"{label} collapsed at {size}: {rect}"
        assert inside(rect, w, h), f"{label} outside window at {size}: {rect} vs {w}x{h}"

    for label in ("stop", "turbo", "lights", "slider"):
        assert not overlap(named["pad"], named[label]), f"pad overlaps {label} at {size}"
    assert not overlap(named["turbo"], named["lights"])
    assert not overlap(named["back"], named["gauge"])

    # STOP is the control you reach for fastest; keep it unmissable
    assert named["stop"].h >= int(56 * app.theme.scale), named["stop"]
    assert named["stop"].w >= int(150 * app.theme.scale), named["stop"]
    # the pad should dominate the left of the screen
    assert named["pad"].w >= w * 0.4, f"pad too small at {size}: {named['pad']}"


def test_editor_stays_on_screen_on_a_phone(make_app):
    app = make_app(size=(740, 360), fleet=[], found=FOUND)
    for _ in range(3):
        assert app.step()
    scene = app.scene
    scene.add_nearby(FOUND[0])
    for _ in range(3):
        assert app.step()
    w, h = app.theme.w, app.theme.h
    for label, rect in scene.editor_rects.items():
        assert inside(rect, w, h), f"editor {label} outside window: {rect} vs {w}x{h}"
    for a_i, (la, ra) in enumerate(scene.editor_rects.items()):
        for lb, rb in list(scene.editor_rects.items())[a_i + 1 :]:
            if la in ("name", "notes") or lb in ("name", "notes"):
                continue
            assert not overlap(ra, rb), f"editor {la} overlaps {lb}: {ra} {rb}"


def test_wireframe_dump(make_app, capsys):
    """Lets a human review the layout as text - no image viewer needed."""
    app = make_app(size=(1024, 640), fleet=FLEET, found=FOUND)
    for _ in range(3):
        assert app.step()
    th, scene = app.theme, app.scene
    print(f"\nGARAGE {th.w}x{th.h} scale={th.scale:.2f}")
    print(f"  scan   {scene.scan_btn.rect}")
    print(f"  strip  {scene.strip}")
    by_id = {p.id: p.name for p in app.garage}
    for (pid, kind), rect in sorted(scene.card_rects_by_key.items()):
        print(f"  {kind:<6} {rect}  {by_id[pid]}")
    print(f"  nearby {[r for r, _ in scene.nearby]}")
    out = capsys.readouterr().out
    assert "GARAGE" in out and "drive" in out
