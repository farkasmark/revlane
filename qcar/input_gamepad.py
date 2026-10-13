"""Gamepad / joystick input via evdev (optional dependency).

``pip install evdev`` and make sure you may read ``/dev/input/event*``
(usually: add yourself to the ``input`` group).

Default mapping (Xbox / Switch Pro style pads):

  left stick Y   forward / backward (proportional throttle)
  left stick X   steer
  dpad up/down   forward / backward (digital)
  dpad left/right steer (digital)
  A / cross      turbo while held
  X / square     toggle lights
  B / circle     hard stop
  Start          quit
"""

from __future__ import annotations

import contextlib
import dataclasses
import threading

from .protocol import SPEED_NORMAL, SPEED_TURBO, Command

try:  # optional
    from evdev import InputDevice, ecodes

    HAVE_EVDEV = True
except Exception:  # pragma: no cover - optional dep
    InputDevice = None
    HAVE_EVDEV = False


def list_gamepads() -> list[tuple[str, str]]:
    if not HAVE_EVDEV:
        return []
    import glob

    out = []
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if ecodes.EV_ABS in dev.capabilities():
            out.append((path, dev.name))
    return out


@dataclasses.dataclass
class GamepadState:
    deadzone: float = 0.15
    speed: int = SPEED_NORMAL
    lights: bool = False
    turbo: bool = False
    quit: bool = False

    fwd: float = 0.0
    steer: float = 0.0
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)
    status: str = ""

    def command(self) -> Command:
        with self._lock:
            dz = self.deadzone
            fwd = 0.0 if abs(self.fwd) < dz else self.fwd
            steer = 0.0 if abs(self.steer) < dz else self.steer
            speed = SPEED_TURBO if self.turbo else self.speed
            return Command(
                forward=fwd > 0,
                backward=fwd < 0,
                left=steer < 0,
                right=steer > 0,
                lights=self.lights,
                speed=int(speed * (0.4 + 0.6 * abs(fwd))) if fwd else SPEED_NORMAL,
            )


AXES = {"ABS_X": "steer", "ABS_Y": "fwd", "ABS_RX": "steer", "ABS_RY": "fwd", "ABS_Z": "fwd"}


class GamepadSession:
    def __init__(self, state: GamepadState, device: str | None = None) -> None:
        if not HAVE_EVDEV:
            raise SystemExit("gamepad support needs `pip install evdev`")
        self.state = state
        self.path = device or self._pick()
        self.dev = InputDevice(self.path)
        self.abs_range: dict[int, tuple[int, int]] = {}
        for code, vals in self.dev.capabilities().get(ecodes.EV_ABS, []):
            self.abs_range[code] = (vals.minimum, vals.maximum)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _pick() -> str:
        pads = list_gamepads()
        if not pads:
            raise SystemExit("no joystick-like /dev/input/event* device found")
        return pads[0][0]

    def __enter__(self) -> GamepadState:
        self.dev.grab()  # exclusive-ish; keeps Steam/other mappers from double-driving
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self.state

    def __exit__(self, *exc) -> None:
        self._stop.set()
        with _quiet():
            self.dev.ungrab()

    def _norm(self, code: int, value: int) -> float:
        lo, hi = self.abs_range.get(code, (-1, 1))
        mid = (lo + hi) / 2
        half = (hi - lo) / 2 or 1
        return max(-1.0, min(1.0, (value - mid) / half))

    def _loop(self) -> None:
        import select

        while not self._stop.is_set():
            try:
                events = select.select([self.dev.fd], [], [], 0.05)[0]
            except OSError:
                return
            if not events:
                continue
            for ev in self.dev.read():
                self._on_event(ev)

    def _on_event(self, ev) -> None:
        st = self.state
        with st._lock:
            if ev.type == ecodes.EV_ABS:
                kind = AXES.get(_abs_name(ev.code), "")
                if kind == "fwd":
                    st.fwd = -self._norm(ev.code, ev.value)  # stick up = forward
                elif kind == "steer":
                    st.steer = self._norm(ev.code, ev.value)
            elif ev.type == ecodes.EV_KEY:
                down = bool(ev.value)
                name = _btn_name(ev.code)
                if name == "BTN_DPAD_UP":
                    st.fwd = 1.0 if down else 0.0
                elif name == "BTN_DPAD_DOWN":
                    st.fwd = -1.0 if down else 0.0
                elif name == "BTN_DPAD_LEFT":
                    st.steer = -1.0 if down else 0.0
                elif name == "BTN_DPAD_RIGHT":
                    st.steer = 1.0 if down else 0.0
                elif name in ("BTN_A", "BTN_TR", "BTN_TL2", "BTN_TR2"):
                    st.turbo = down
                    if down:
                        st.speed = SPEED_TURBO
                elif name == "BTN_X" and down:
                    st.lights = not st.lights
                elif name == "BTN_B" and down:
                    st.fwd = 0.0
                    st.steer = 0.0
                elif name in ("BTN_START", "BTN_MODE") and down:
                    st.quit = True


@contextlib.contextmanager
def _quiet():
    try:
        yield
    except Exception:
        pass


def _abs_name(code: int) -> str:
    try:
        return ecodes.ABS_by(code)
    except KeyError:
        return ""


def _btn_name(code: int) -> str:
    try:
        return ecodes.BTN_by(code)
    except KeyError:
        return ""
