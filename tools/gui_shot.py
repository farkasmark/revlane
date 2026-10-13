#!/usr/bin/env python3
"""Render GUI scenes to PNG without a car, for design review and CI.

    python tools/gui_shot.py --outdir /tmp/shots
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import Future
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")
sys.path.insert(0, ".")

import pygame  # noqa: E402

from qcar.gui.app import App  # noqa: E402
from qcar.gui.backend import Discovered  # noqa: E402
from qcar.profiles import Garage  # noqa: E402
from qcar.protocol import Telemetry  # noqa: E402

FLEET = [
    ("Marci's BMW M3", "00:3C:DE:07:01:A4", "#ffb300", 95, 41.2, "home car; FL wheel drifts left tired"),
    ("Dino 246 GT", "00:3C:DE:0A:BB:02", "#e53935", 61, 12.5, "guest car, speed capped for the kids"),
    ("Patron 499P", "00:3C:DE:0C:DD:04", "#1e88e5", 22, 3.0, "needs a charge"),
]


class StubBackend:
    def __init__(self, found):
        self.found = found
        self.state = "connected"
        self.address = None
        self.telemetry = Telemetry(seq=11, battery=95)
        self.tx = 4210
        self.errors = 0
        self.fps_tx = 22.0
        self._intent = None

    def start(self): pass
    def stop(self): pass
    def tickle(self): pass
    @property
    def gui_alive(self): return True
    def set_intent(self, cmd): self._intent = cmd
    def intent(self): return self._intent
    def scan(self, timeout=6.0):
        f = Future()
        f.set_result(self.found)
        return f
    def connect(self, address):
        self.address = address
        f = Future()
        f.set_result(None)
        return f
    def disconnect(self):
        self.address = None
        f = Future()
        f.set_result(None)
        return f


def build_garage(path: Path) -> Garage:
    g = Garage(path)
    for i, (name, addr, accent, batt, mins, notes) in enumerate(FLEET):
        p = g.add(name, addr, accent=accent, speed_cap=100 if i == 0 else 80,
                  turbo_default=(i == 0), notes=notes)
        p.last_battery = batt
        p.drive_seconds = mins * 60
        p.last_seen = __import__("time").time() - (i * 3700)
    g.save()
    return g


def check_not_blank(path: Path) -> str:
    s = pygame.image.load(str(path))
    cols = {}
    for x in range(0, s.get_width(), 7):
        for y in range(0, s.get_height(), 7):
            cols[s.get_at((x, y))[:3]] = cols.get(s.get_at((x, y))[:3], 0) + 1
    return f"{len(cols)} distinct colours, {s.get_width()}x{s.get_height()}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="/tmp/shots")
    args = ap.parse_args()
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    pygame.init()

    shots = [
        ("garage_desktop", (1280, 800), "garage"),
        ("garage_editor", (1280, 800), "editor"),
        ("garage_phone_portrait", (390, 720), "garage"),
        ("garage_phone_landscape", (740, 360), "garage"),
        ("drive_phone_landscape", (740, 360), "drive"),
        ("drive_desktop", (1280, 800), "drive"),
    ]
    for name, size, what in shots:
        garage = build_garage(out / f"{name}.json")
        found = [Discovered(a, f"QCAR-{a[-5:]}", -50 - i * 7) for i, (_, a, *_rest) in enumerate(FLEET)]
        app = App(garage=garage, size=size, video_driver="offscreen", backend=StubBackend(found))
        for _ in range(4):
            app.step()
        if what == "editor":
            scene = app.scene
            scene.add_nearby(found[0])
            for _ in range(3):
                app.step()
        elif what == "drive":
            prof = garage.find(FLEET[0][1])
            prof.last_battery = 78
            app.drive(prof)
            app.backend.telemetry = Telemetry(seq=17, battery=78)
            for _ in range(4):
                app.step()
            # hold the pad forward-right so the shot shows an active command
            pad = app.scene.pad
            cx, cy = pad.center
            pygame.event.post(pygame.event.Event(
                pygame.FINGERDOWN, x=cx / app.theme.w,
                y=(cy - pad.radius * 0.75) / app.theme.h, dx=0.0, dy=0.0,
                finger_id=0, touch_id=0))
            for _ in range(3):
                app.step()
        target = out / f"{name}.png"
        pygame.image.save(app.screen, str(target))
        print(f"{target}  {check_not_blank(target)}")
        app.shutdown()


if __name__ == "__main__":
    main()
