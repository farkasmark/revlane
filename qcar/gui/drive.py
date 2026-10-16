"""DRIVE scene: analog pad, big STOP, live telemetry.

The pad is a thumb-controlled joystick: press anywhere and drag. Up/down is
throttle, left/right is steering, quantised to the four flags the car actually
understands. Because touch and mouse buttons give a real release event (a
terminal never does), hold-to-drive works exactly as you would expect here.

Keyboard works too, and also reports releases: WASD or arrows, SPACE to stop,
L lights, T turbo, Q/Esc back to the garage.
"""

from __future__ import annotations

import math
import time

import pygame

from ..profiles import Profile
from ..protocol import SPEED_TURBO, Command
from . import theme as T
from .base import Scene
from .widgets import BatteryGauge, Button, Label, Slider, Toggle, rounded, signal_bars


class ControlPad:
    """Analog joystick pad -> the four digital flags the car speaks."""

    DEADZONE = 0.22

    def __init__(self, rect: pygame.Rect) -> None:
        self.rect = rect
        self.active = False
        self.knob = (0.0, 0.0)  # -1..1, y up

    @property
    def center(self) -> tuple[int, int]:
        return self.rect.center

    @property
    def radius(self) -> int:
        return int(min(self.rect.w, self.rect.h) * 0.42)

    def _normalised(self, pos) -> tuple[float, float]:
        cx, cy = self.center
        r = self.radius or 1
        dx = (pos[0] - cx) / r
        dy = -(pos[1] - cy) / r
        mag = math.hypot(dx, dy)
        if mag > 1.0:
            dx, dy = dx / mag, dy / mag
        return dx, dy

    def on_down(self, pos) -> bool:
        if not self.rect.collidepoint(pos):
            return False
        self.active = True
        self.knob = self._normalised(pos)
        return True

    def on_motion(self, pos) -> None:
        if self.active:
            self.knob = self._normalised(pos)

    def on_up(self, pos) -> bool:
        if not self.active:
            return False
        self.active = False
        self.knob = (0.0, 0.0)
        return True

    def release(self) -> None:
        self.active = False
        self.knob = (0.0, 0.0)

    def axes(self) -> tuple[float, float]:
        if not self.active:
            return 0.0, 0.0
        return self.knob

    def flags(self) -> dict:
        dx, dy = self.axes()
        dz = self.DEADZONE
        return {
            "forward": dy > dz,
            "backward": dy < -dz,
            "left": dx < -dz,
            "right": dx > dz,
        }

    def draw(self, surf, th: T.Theme) -> None:
        cx, cy = self.center
        r = self.radius
        base = T.PANEL if not self.active else T.PANEL_2
        pygame.draw.circle(surf, T.SHADOW, (cx, cy + int(4 * th.scale)), r)
        pygame.draw.circle(surf, base, (cx, cy), r)
        pygame.draw.circle(surf, T.LINE if not self.active else T.WARN, (cx, cy), r,
                           max(2, int(2 * th.scale)))
        pygame.draw.circle(surf, T.LINE, (cx, cy), int(r * 0.45), 1)
        # direction hints
        f = th.font.get(th.fs(15))
        for label, ang in (("F", 90), ("B", 270), ("L", 180), ("R", 0)):
            x = cx + math.cos(math.radians(ang)) * r * 0.78
            y = cy - math.sin(math.radians(ang)) * r * 0.78
            img = f.render(label, True, T.FAINT)
            surf.blit(img, img.get_rect(center=(x, y)))
        kx = cx + self.knob[0] * r
        ky = cy - self.knob[1] * r
        if self.active:
            pygame.draw.line(surf, T.WARN, (cx, cy), (kx, ky), max(2, int(3 * th.scale)))
        knob_r = int(r * 0.3)
        pygame.draw.circle(surf, T.WARN if self.active else T.IDLE_BTN_UP, (int(kx), int(ky)), knob_r)
        pygame.draw.circle(surf, T.TEXT, (int(kx), int(ky)), knob_r, max(1, int(2 * th.scale)))


class DriveScene(Scene):
    KEYS = {
        pygame.K_w: "forward",
        pygame.K_UP: "forward",
        pygame.K_s: "backward",
        pygame.K_DOWN: "backward",
        pygame.K_a: "left",
        pygame.K_LEFT: "left",
        pygame.K_d: "right",
        pygame.K_RIGHT: "right",
    }

    def __init__(self, app, profile: Profile) -> None:
        super().__init__(app)
        self.profile = profile
        self.pad = ControlPad(pygame.Rect(0, 0, 10, 10))
        self.slider: Slider | None = None
        self.kb: dict[str, bool] = dict.fromkeys(("forward", "backward", "left", "right"), False)
        self.speed = profile.speed_default
        self.lights = profile.lights_default
        self.turbo = profile.turbo_default
        self.started = time.monotonic()
        self._last_intent: Command | None = None

    # ------------------------------------------------------------------

    def enter(self) -> None:
        prof = self.profile
        prof.start_session()
        self.app.garage.save()
        self.app.backend.set_intent(Command(speed=self.speed, lights=self.lights))
        self.app.toasts.push(f"linked to {prof.name}", T.GOOD)

    def leave(self) -> None:
        self.release_inputs()
        self.app.backend.set_intent(Command())
        self.profile.end_session(time.monotonic() - self.started)
        self.app.garage.save()

    def layout(self, th: T.Theme) -> None:
        self.th = th
        pad = th.pad
        bar_h = int(54 * th.scale)
        self.topbar = pygame.Rect(pad, pad, th.w - 2 * pad, bar_h)
        self.back = Button(pygame.Rect(self.topbar.x, self.topbar.y, int(84 * th.scale), bar_h),
                           "< BACK", self.app.to_garage, size=19)
        self.name = Label(pygame.Rect(self.back.rect.right + int(14 * th.scale), self.topbar.y,
                                      int(260 * th.scale), bar_h), self.profile.name, T.TEXT, 24)
        self.gauge = BatteryGauge(pygame.Rect(0, 0, int(96 * th.scale), int(30 * th.scale)))
        # inset from the frame: the gauge draws a battery nub PAST its right
        # edge, and flush placement put the nub on the topbar's border line
        self.gauge.rect.midright = (self.topbar.right - int(12 * th.scale),
                                    self.topbar.centery)
        self.sig = pygame.Rect(self.gauge.rect.x - int(44 * th.scale), 0, int(30 * th.scale),
                               int(26 * th.scale))
        self.sig.centery = self.topbar.centery

        body = pygame.Rect(
            pad,
            self.topbar.bottom + pad,
            th.w - 2 * pad,
            max(int(150 * th.scale), th.h - self.topbar.bottom - 2 * pad - int(58 * th.scale)),
        )
        # Phone in portrait (or a tall window): pad on top, controls stacked
        # underneath. Landscape: pad takes the left, controls a column on the
        # right. Never let the control column squeeze the pad into a slot.
        if body.h >= body.w * 1.02:
            pad_rect = pygame.Rect(body.x, body.y, body.w, int(body.h * 0.58))
            right = pygame.Rect(body.x, pad_rect.bottom + int(10 * th.scale), body.w,
                                body.h - pad_rect.h - int(10 * th.scale))
        else:
            right_w = int(min(int(420 * th.scale), max(int(190 * th.scale), body.w * 0.36)))
            right_w = min(right_w, int(body.w * 0.55))
            pad_rect = pygame.Rect(body.x, body.y, body.w - right_w - pad, body.h)
            right = pygame.Rect(body.right - right_w, body.y, right_w, body.h)
        self.pad = ControlPad(pad_rect)

        stop_h = int(min(96 * th.scale, max(44 * th.scale, right.h * 0.30)))
        self.stop = Button(pygame.Rect(right.x, right.y, right.w, stop_h),
                           "STOP", self.hard_stop, kind="danger", size=32)
        y = self.stop.rect.bottom + int(12 * th.scale)
        tcol = right.w // 2 - int(5 * th.scale)
        togg_h = int(min(56 * th.scale, max(38 * th.scale, right.h * 0.18)))
        self.turbo_t = Toggle(pygame.Rect(right.x, y, tcol, togg_h), "TURBO",
                              self.set_turbo, value=self.turbo)
        self.lights_t = Toggle(pygame.Rect(right.x + tcol + int(10 * th.scale), y, tcol, togg_h),
                               "LIGHTS", self.set_lights, value=self.lights)
        y = self.turbo_t.rect.bottom + int(28 * th.scale)
        slider_h = int(min(64 * th.scale, max(40 * th.scale, right.h * 0.2)))
        # the profile cap always wins (safety), so make the controls HONEST
        # about it: the slider tops at the cap and TURBO is disabled when the
        # cap is below turbo speed - instead of silently clamping while the
        # controls pretend to work
        cap = self.profile.speed_cap
        self.slider = Slider(pygame.Rect(right.x, y, right.w, slider_h),
                             20, min(100, cap), min(self.speed, min(100, cap)),
                             self.set_speed, label="SPEED")
        self.turbo_t.enabled = cap >= SPEED_TURBO
        self.hint = Label(
            pygame.Rect(right.x, self.slider.rect.bottom + int(14 * th.scale), right.w,
                        max(0, right.bottom - self.slider.rect.bottom - int(14 * th.scale))),
            "drag the pad or\nWASD / arrows\nSPACE stop  L lights\nT turbo  Q back",
            T.FAINT,
            16,
        )
        self.hint.visible = self.hint.rect.h >= th.fs(60)
        # self.name is drawn explicitly in draw_topbar - it is not a widget
        # here, and listing it drew the label twice
        self.widgets = [self.back, self.turbo_t, self.lights_t, self.slider, self.stop]

    # ------------------------------------------------------------------
    # control

    def command(self) -> Command:
        flags = dict(self.kb)
        for k, v in self.pad.flags().items():
            flags[k] = flags[k] or v
        # the profile cap always wins - turbo must not smuggle extra throttle
        speed = min(SPEED_TURBO if self.turbo else self.speed, self.profile.speed_cap)
        return Command(
            forward=flags["forward"],
            backward=flags["backward"],
            left=flags["left"],
            right=flags["right"],
            lights=self.lights,
            speed=speed,
        )

    def release_inputs(self) -> None:
        """Drop every held control (window lost focus, scene is leaving)."""
        self.kb = dict.fromkeys(self.kb, False)
        self.pad.release()
        if self.slider is not None:
            self.slider.dragging = False

    def hard_stop(self) -> None:
        self.release_inputs()
        self.app.backend.set_intent(Command(speed=self.speed, lights=self.lights))
        # feedback: a stop tap while coasting otherwise looks like a dead button
        self.app.toasts.push("stopped", T.WARN)

    def set_turbo(self, value: bool) -> None:
        self.turbo = value
        if value:
            self.speed = SPEED_TURBO

    def set_lights(self, value: bool) -> None:
        self.lights = value

    def set_speed(self, value: int) -> None:
        self.turbo = value >= SPEED_TURBO
        self.turbo_t.value = self.turbo
        self.speed = value

    def update(self, dt: float) -> None:
        cmd = self.command()
        self._last_intent = cmd
        self.app.backend.set_intent(cmd)
        # keep the garage card honest about the battery
        tel = self.app.backend.telemetry
        if tel is not None and tel.battery != self.profile.last_battery:
            self.profile.last_battery = tel.battery
            self.profile.last_seen = time.time()
            self.app.garage.dirty = True

    # ------------------------------------------------------------------
    # input

    def pointer_down(self, pos) -> bool:
        if self.pad.on_down(pos):
            return True
        for w in self.widgets:
            if w.on_down(pos):
                return True
        return False

    def pointer_up(self, pos) -> bool:
        released = self.pad.on_up(pos)
        for w in self.widgets:
            if w.on_up(pos):
                return True
        return released

    def pointer_motion(self, pos) -> None:
        self.pad.on_motion(pos)
        for w in self.widgets:
            w.on_motion(pos)

    def key_down(self, ev) -> bool:
        if ev.key in self.KEYS:
            self.kb[self.KEYS[ev.key]] = True
            return True
        if ev.key == pygame.K_SPACE:
            self.hard_stop()
            return True
        if ev.key == pygame.K_l:
            self.lights = not self.lights
            self.lights_t.value = self.lights
            return True
        if ev.key == pygame.K_t:
            self.set_turbo(not self.turbo)
            return True
        if ev.key in (pygame.K_q, pygame.K_ESCAPE):
            self.app.to_garage()
            return True
        return False

    def key_up(self, ev) -> bool:
        if ev.key in self.KEYS:
            self.kb[self.KEYS[ev.key]] = False
            return True
        return False

    # ------------------------------------------------------------------
    # output

    def draw(self, surf, th: T.Theme) -> None:
        surf.fill(T.BG)
        self.draw_topbar(surf, th)
        self.pad.draw(surf, th)
        for w in self.widgets:
            w.draw(surf, th)
        self.draw_telemetry(surf, th)

    def draw_topbar(self, surf, th: T.Theme) -> None:
        rounded(surf, self.topbar, T.PANEL, th.radius, border=1, border_color=T.LINE)
        self.name.draw(surf, th)
        b = self.app.backend
        self.gauge.pct = b.telemetry.battery if b.telemetry else self.profile.last_battery
        self.gauge.draw(surf, th)
        signal_bars(surf, self.sig, self.app.known_rssi(b.address), th)
        st = b.state
        color = {"connected": T.GOOD, "scanning": T.WARN, "connecting": T.WARN,
                 "error": T.BAD}.get(st, T.FAINT)
        f = th.font.get(th.fs(18))
        img = f.render(st.upper(), True, color)
        surf.blit(img, img.get_rect(midright=(self.sig.x - int(12 * th.scale), self.topbar.centery)))

    def draw_telemetry(self, surf, th: T.Theme) -> None:
        b = self.app.backend
        bar = pygame.Rect(th.pad, th.h - th.pad - int(44 * th.scale), th.w - 2 * th.pad,
                          int(44 * th.scale))
        rounded(surf, bar, T.PANEL, int(10 * th.scale), border=1, border_color=T.LINE)
        # toasts must stop above the telemetry bar, not on its text
        self.toast_bottom = bar.top - int(6 * th.scale)
        cmd = self.command()
        dx, dy = self.pad.axes()
        parts = [
            f"pad {dx:+.2f}/{dy:+.2f}",
            f"cmd {cmd.to_plaintext()[4:10].hex()}",
            f"speed {cmd.speed}%",
            f"tx {b.fps_tx:.0f}/s",
            f"frames {b.tx}",
            f"errors {b.errors}",
            f"seq {b.telemetry.seq if b.telemetry else '-'}",
        ]
        if not b.gui_alive:
            parts.insert(0, "GUI STALLED -> STOPPED")
        if b.state == "error" and b.error:
            parts.insert(0, f"connect failed: {b.error}")
        x = bar.x + int(14 * th.scale)
        f = th.font.get(th.fs(17))
        for p in parts:
            col = T.BAD if (p.startswith("errors") and b.errors) or "STALLED" in p else T.DIM
            img = f.render(p, True, col)
            surf.blit(img, img.get_rect(midleft=(x, bar.centery)))
            x += img.get_width() + int(20 * th.scale)
