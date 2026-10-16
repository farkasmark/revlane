"""`qcarctl gui` application shell.

Owns the window, the scene stack and the pointer, and keeps the render loop
completely decoupled from the radio loop. Also renders headless
(``--screenshot`` / ``--frames``) which is how this GUI gets tested at all.
"""

from __future__ import annotations

import os
import queue
import time

# before pygame/SDL initialise: we read touch ourselves, so suppress SDL's
# touch->mouse and mouse->touch emulation or every tap lands twice
os.environ.setdefault("SDL_MOUSE_TOUCH_EVENTS", "0")
os.environ.setdefault("SDL_TOUCH_MOUSE_EVENTS", "0")

import pygame  # noqa: E402

from .. import APP_NAME  # noqa: E402
from ..profiles import Garage, Profile  # noqa: E402
from ..protocol import Command  # noqa: E402
from . import theme as T  # noqa: E402
from .backend import CarBackend  # noqa: E402
from .base import Scene  # noqa: E402
from .drive import DriveScene  # noqa: E402
from .garage import GarageScene  # noqa: E402
from .theme import Theme  # noqa: E402
from .widgets import rounded  # noqa: E402


class Toasts:
    def __init__(self) -> None:
        self.items: list[list] = []

    def push(self, msg: str, color=None, ttl: float = 3.5) -> None:
        self.items.append([msg, time.monotonic() + ttl, color or T.TEXT])
        del self.items[:-4]

    def draw(self, surf, th: Theme, bottom: int | None = None,
             top: int | None = None) -> None:
        """Stack toasts upward from `bottom` (default: screen bottom), or
        downward from `top` (modal scenes). Scenes raise these so toasts
        never cover interactive UI - the drive telemetry bar, the garage
        editor's SAVE/CANCEL row."""
        now = time.monotonic()
        self.items = [i for i in self.items if i[1] > now]
        f = th.font.get(th.fs(18))
        if top is not None:
            y = top
            for msg, _until, color in self.items:
                img = f.render(msg, True, color)
                box = img.get_rect(midtop=(th.w // 2, y))
                box.inflate_ip(int(28 * th.scale), int(14 * th.scale))
                rounded(surf, box, T.PANEL, th.radius, border=1, border_color=T.LINE)
                surf.blit(img, img.get_rect(center=box.center))
                y = box.bottom + int(6 * th.scale)
            return
        y = (th.h if bottom is None else bottom) - int(8 * th.scale)
        for msg, _until, color in reversed(self.items):
            img = f.render(msg, True, color)
            box = img.get_rect(midbottom=(th.w // 2, y))
            box.inflate_ip(int(28 * th.scale), int(14 * th.scale))
            rounded(surf, box, T.PANEL, th.radius, border=1, border_color=T.LINE)
            surf.blit(img, img.get_rect(center=box.center))
            y = box.top - int(6 * th.scale)


class App:
    def __init__(
        self,
        *,
        garage: Garage | None = None,
        adapter: str | None = None,
        size: tuple[int, int] = (1024, 640),
        fullscreen: bool = False,
        fps: int = 60,
        video_driver: str | None = None,
        interval: float = 0.08,
        backend: CarBackend | None = None,
        software: bool = False,
    ) -> None:
        if video_driver:
            os.environ["SDL_VIDEODRIVER"] = video_driver
        if software:
            # must be set before SDL initialises: some X stacks (Xwayland with no
            # GLX) abort the process instead of raising, so this cannot be retried
            os.environ["SDL_RENDER_DRIVER"] = "software"
        pygame.init()
        flags = pygame.FULLSCREEN | pygame.NOFRAME if fullscreen else pygame.RESIZABLE
        self.screen = pygame.display.set_mode(size, flags)
        # Our name only. A window title is the most endorsement-shaped surface
        # an app has, so third-party marks stay out of it entirely.
        pygame.display.set_caption(f"{APP_NAME} - open Bluetooth controller for QCAR cars")
        self.theme = Theme(*self.screen.get_size())
        self.fps = fps
        self.clock = pygame.time.Clock()

        self.garage = garage if garage is not None else Garage.load()
        self.events: queue.Queue = queue.Queue()
        self.backend = backend or CarBackend(
            adapter=adapter, interval=interval, on_event=self._backend_event
        )
        self.backend.start()
        self.toasts = Toasts()

        self.scene: Scene = GarageScene(self)
        self.scene.layout(self.theme)
        self.scene.enter()

        self.running = False
        self._pointer: int | None = None

    # ------------------------------------------------------------------

    def _backend_event(self, kind: str, msg: str) -> None:
        # called from the radio thread; never touch pygame state here
        self.events.put((kind, msg))

    def drain_events(self) -> None:
        while True:
            try:
                kind, msg = self.events.get_nowait()
            except queue.Empty:
                return
            self.toasts.push(msg, T.BAD if kind == "error" else T.TEXT)

    def known_rssi(self, address: str | None) -> int:
        if address is None:
            return 0
        for d in self.backend.found:
            if d.address.upper() == address.upper():
                return d.rssi
        return 0

    # ---- scenes -------------------------------------------------------

    def switch(self, scene: Scene) -> None:
        self.scene.release_inputs()
        self.scene.leave()
        self.scene = scene
        self.scene.layout(self.theme)
        self.scene.enter()

    def to_garage(self) -> None:
        self.backend.set_intent(Command())
        self.switch(GarageScene(self))  # GarageScene.enter() re-scans

    def drive(self, profile: Profile) -> None:
        if self.backend.address != profile.address:
            fut = self.backend.connect(profile.address)

            def done(f):
                if f.exception():
                    self.events.put(("error", f"could not reach {profile.name}: "
                                             f"{f.exception()}"))

            fut.add_done_callback(done)
        self.switch(DriveScene(self, profile))

    # ---- events -------------------------------------------------------

    def handle_event(self, ev) -> bool:
        if ev.type == pygame.QUIT:
            return False
        if ev.type == pygame.WINDOWRESIZED or ev.type == pygame.VIDEORESIZE:
            w, h = self.screen.get_size()
            self.theme.resize(max(320, w), max(240, h))
            self.scene.layout(self.theme)
            return True
        if ev.type == pygame.WINDOWFOCUSLOST:
            # never keep driving a window nobody is looking at: clear the held
            # controls first, or update() would just re-send them next frame
            self.scene.release_inputs()
            self.backend.set_intent(Command())
            self.toasts.push("focus lost - stopped", T.WARN)
            return True

        if ev.type in (pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN):
            self._pointer = getattr(ev, "finger_id", 0)
            self.scene.pointer_down(self._event_pos(ev))
            return True
        if ev.type in (pygame.MOUSEBUTTONUP, pygame.FINGERUP):
            self._pointer = None
            self.scene.pointer_up(self._event_pos(ev))
            return True
        if ev.type in (pygame.MOUSEMOTION, pygame.FINGERMOTION):
            self.scene.pointer_motion(self._event_pos(ev))
            return True
        if ev.type == pygame.KEYDOWN:
            if ev.key == pygame.K_F11:
                pygame.display.toggle_fullscreen()
                return True
            # key_down has side effects (scene state) whether or not it
            # "consumed" the key; returning True keeps the app running
            self.scene.key_down(ev)
            return True
        if ev.type == pygame.KEYUP:
            self.scene.key_up(ev)
            return True
        return True

    def _event_pos(self, ev) -> tuple[int, int]:
        """Finger coords are normalised 0..1; mouse coords are already pixels."""
        if ev.type in (pygame.FINGERDOWN, pygame.FINGERUP, pygame.FINGERMOTION):
            return int(ev.x * self.theme.w), int(ev.y * self.theme.h)
        return tuple(ev.pos)

    # ---- main loop ----------------------------------------------------

    def step(self) -> bool:
        """One frame: events -> update -> draw. Returns False when asked to quit."""
        dt = self.clock.tick(self.fps) / 1000.0
        for ev in pygame.event.get():
            if not self.handle_event(ev):
                return False
        self.drain_events()
        self.backend.tickle()
        self.scene.update(dt)
        self.scene.draw(self.screen, self.theme)
        self.toasts.draw(self.screen, self.theme,
                         bottom=getattr(self.scene, "toast_bottom", None),
                         top=getattr(self.scene, "toast_top", None))
        pygame.display.flip()
        return True

    def run(self, frames: int | None = None, screenshot: str | None = None,
            run_seconds: float = 0.0) -> int:
        self.running = True
        n = 0
        t_end = time.monotonic() + run_seconds if run_seconds else None
        while self.running:
            if not self.step():
                break
            n += 1
            if screenshot and n >= 3:
                pygame.image.save(self.screen, screenshot)
            if frames and n >= frames:
                break
            if t_end and time.monotonic() > t_end:
                break
        self.shutdown()
        return n

    def shutdown(self) -> None:
        try:
            self.scene.leave()
        except Exception:  # noqa: BLE001
            pass
        self.backend.stop()
        if self.garage.dirty:
            self.garage.save()
        pygame.quit()



