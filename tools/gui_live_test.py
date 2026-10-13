#!/usr/bin/env python3
"""Live smoke test: real CarBackend + real GUI scenes, offscreen.

Drives the actual app object (not a fake) and blinks the headlights through the
GUI's own key handling, so it proves the whole chain:
    key event -> DriveScene -> CarBackend thread -> AES frame -> ACKed write
Watch the car: the headlights should come on for about half a second, then go off.
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")
sys.path.insert(0, ".")

import pygame  # noqa: E402

from qcar.gui.app import App  # noqa: E402
from qcar.profiles import Garage  # noqa: E402


def wait(app, predicate, timeout=12.0, why=""):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.step()
        if predicate():
            return True
        time.sleep(0.005)
    raise SystemExit(f"timed out waiting for {why}: state={app.backend.state}")


def main() -> None:
    garage = Garage.load()
    if not len(garage):
        sys.exit("no saved cars - use `qcarctl profile add` or the GUI first")
    prof = garage.sorted()[0]
    print(f"target: {prof.name}  {prof.address}")

    app = App(garage=garage, size=(1024, 640), video_driver="offscreen")
    app.fps = 240  # no vsync offscreen; spin fast so the scan finishes quickly
    try:
        wait(app, lambda: app.backend.state != "scanning", 12, "scan to finish")
        print(f"scan found {len(app.backend.found)} car(s); state={app.backend.state}")

        key = next((k for k in app.scene.card_rects_by_key if k[1] == "drive"), None)
        if key is None:
            sys.exit("DRIVE button not on screen for that profile (try another page)")
        rect = app.scene.card_rects_by_key[key]
        for typ in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
            pygame.event.post(pygame.event.Event(typ, pos=rect.center, button=1))
        wait(app, lambda: app.backend.state == "connected", 25, "connection")
        print(f"connected; scene={app.scene.__class__.__name__}")

        # toggle the headlights using the GUI's own key handling
        for i in (1, 2):
            pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_l, mod=0,
                                                 unicode="l", scancode=0))
            for _ in range(40):
                app.step()
            print(f"  blink {i}: lights={app.backend.intent().lights} tx={app.backend.tx} "
                  f"errors={app.backend.errors}")
            pygame.event.post(pygame.event.Event(pygame.KEYUP, key=pygame.K_l, mod=0,
                                                 unicode="", scancode=0))
            for _ in range(40):
                app.step()

        tel = app.backend.telemetry
        print(f"telemetry: {tel}")
        print(f"total frames sent={app.backend.tx} errors={app.backend.errors}")
        print("OK - if the headlights came on then went off, the GUI radio path works")
    finally:
        app.shutdown()


if __name__ == "__main__":
    main()
