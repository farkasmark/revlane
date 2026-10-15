"""Shared GUI test scaffolding: a fake radio backend and event helpers."""

from __future__ import annotations

import os
from concurrent.futures import Future

os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")

import pygame  # noqa: E402
import pytest  # noqa: E402

from qcar.gui.app import App  # noqa: E402
from qcar.gui.backend import Discovered  # noqa: E402
from qcar.profiles import Garage  # noqa: E402
from qcar.protocol import Command  # noqa: E402

ADDR = "00:3C:DE:07:01:A4"
FLEET = [
    ("Marci's BMW M3", "00:3C:DE:07:01:A4"),
    ("Dino 246 GT", "00:3C:DE:0A:BB:02"),
    ("Patron 499P", "00:3C:DE:0C:DD:04"),
]
FOUND = [Discovered(a, n, -50 - i * 6) for i, (n, a) in enumerate(FLEET)]


class FakeBackend:
    """Stands in for CarBackend; records every intent it is handed."""

    def __init__(self, found=None):
        self.found = list(FOUND if found is None else found)
        self.state = "offline"
        self.address = None
        self.telemetry = None
        self.tx = 0
        self.errors = 0
        self.error = ""
        self.fps_tx = 22.0
        self.intents: list[Command] = []
        self._intent = Command()
        self.scans = 0
        self.connects: list[str] = []

    def start(self):
        pass

    def stop(self):
        pass

    def tickle(self):
        pass

    @property
    def gui_alive(self):
        return True

    def set_intent(self, cmd):
        self._intent = cmd
        if not self.intents or self.intents[-1] != cmd:
            self.intents.append(cmd)

    def intent(self):
        return self._intent

    def scan(self, timeout=6.0):
        self.scans += 1
        fut: Future = Future()
        fut.set_result(self.found)
        return fut

    def connect(self, address):
        self.connects.append(address)
        self.address = address
        self.state = "connected"
        fut: Future = Future()
        fut.set_result(None)
        return fut

    def disconnect(self):
        self.address = None
        self.state = "offline"
        fut: Future = Future()
        fut.set_result(None)
        return fut


# ------------------------------------------------------------------ helpers


def key_ev(code, ch="", typ=pygame.KEYDOWN):
    return pygame.event.Event(typ, key=code, mod=0, unicode=ch, scancode=0)


def click(rect_or_pos):
    """A full press *and* release - which is what a click actually is."""
    x, y = (rect_or_pos.center if hasattr(rect_or_pos, "center") else rect_or_pos)
    pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=(int(x), int(y)), button=1))
    pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONUP, pos=(int(x), int(y)), button=1))


def finger(x, y, typ=pygame.FINGERDOWN, *, w, h):
    pygame.event.post(
        pygame.event.Event(typ, x=x / w, y=y / h, dx=0.0, dy=0.0, finger_id=0, touch_id=0)
    )


def pump(app, n=2):
    for _ in range(n):
        assert app.step(), "app asked to quit"


# ----------------------------------------------------------------- fixtures


@pytest.fixture
def apps():
    """Quit pygame after each test so instances do not leak between them."""
    made: list[App] = []
    yield made
    for _a in made:
        if pygame.get_init():
            pygame.quit()


@pytest.fixture
def make_app(tmp_path, apps):
    def _make(size=(1024, 640), found=None, fleet=None):
        garage = Garage(tmp_path / f"p{size[0]}x{size[1]}.json")
        for name, addr in (fleet if fleet is not None else []):
            garage.add(name, addr)
        app = App(
            garage=garage,
            size=size,
            video_driver="offscreen",
            backend=FakeBackend(FOUND if found is None else found),
        )
        apps.append(app)
        return app

    return _make
