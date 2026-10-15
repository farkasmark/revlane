"""Keyboard input for a terminal.

A raw TTY cannot tell us when a key is *released*, so we offer two mappings:

``toggle``
    press once to start, press again to stop.  Immediate, no lag, but you
    tap twice to reverse.
``hold``
    press-and-hold to drive.  Works by exploiting the keyboard auto-repeat:
    while the key is held the repeat keeps the direction alive, and it drops
    out after ``hold_ttl`` seconds.  Feels natural; costs ~0.5 s of stop lag.
"""

from __future__ import annotations

import dataclasses
import select
import sys
import termios
import threading
import time
import tty

from .protocol import SPEED_NORMAL, SPEED_TURBO, Command

# key -> logical action
PLAIN = {
    "w": "fwd",
    "s": "back",
    "a": "left",
    "d": "right",
}
# arrow escape-sequence final byte -> the equivalent WASD letter, so the rest
# of the pipeline only ever has to deal with single characters
CSI = {"A": "w", "B": "s", "C": "d", "D": "a"}


@dataclasses.dataclass
class KeyState:
    """Thread-safe holder of the current drive state."""

    mode: str = "toggle"
    hold_ttl: float = 0.55
    repeat_block: float = 0.12
    speed: int = SPEED_NORMAL
    lights: bool = False
    turbo: bool = False
    quit: bool = False

    _flags: dict = dataclasses.field(default_factory=dict)
    _last_key: dict = dataclasses.field(default_factory=dict)
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)
    status: str = ""

    def handle(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            prev = self._last_key.get(key, 0.0)
            self._last_key[key] = now
            if key in PLAIN:
                action = PLAIN[key]
                if self.mode == "toggle":
                    # auto-repeat must not re-trigger a toggle; keeping
                    # _last_key fresh (above, unconditionally) is what
                    # suppresses a whole held-key burst
                    if now - prev < self.repeat_block:
                        return
                    self._flags[action] = not self._flags.get(action, False)
                else:
                    # hold mode lives off auto-repeat: every repeat of the
                    # held key is a keep-alive and refreshes the hold, so a
                    # held key stays alive well past hold_ttl
                    self._flags[action] = True
                    self._flags["_t_" + action] = now
                self._exclusive(action)
            elif key == " ":
                self._flags.clear()
                self.status = "stopped"
            elif key == "l":
                self.lights = not self.lights
                self.status = f"lights {'on' if self.lights else 'off'}"
            elif key == "t":
                self.turbo = not self.turbo
                self.speed = SPEED_TURBO if self.turbo else SPEED_NORMAL
                self.status = f"turbo {'on' if self.turbo else 'off'}"
            elif key in ("+", "="):
                self.speed = min(SPEED_TURBO, self.speed + 5)
                self.status = f"speed {self.speed}"
            elif key in ("-", "_"):
                self.speed = max(10, self.speed - 5)
                self.status = f"speed {self.speed}"
            elif key in ("q", "\x03", "\x1b"):
                self.quit = True

    def _exclusive(self, action: str) -> None:
        pairs = {"fwd": "back", "back": "fwd", "left": "right", "right": "left"}
        other = pairs[action]
        if self._flags.get(action):
            self._flags[other] = False

    def command(self) -> Command:
        now = time.monotonic()
        with self._lock:
            flags = dict(self._flags)
            for a in ("fwd", "back", "left", "right"):
                if self.mode != "toggle" and now - flags.get("_t_" + a, 0) > self.hold_ttl:
                    flags[a] = False
            return Command(
                forward=bool(flags.get("fwd")),
                backward=bool(flags.get("back")),
                left=bool(flags.get("left")),
                right=bool(flags.get("right")),
                lights=self.lights,
                speed=self.speed,
            )


def _parse(buf: bytes) -> list[str]:
    """Turn a raw stdin chunk into logical key names."""
    keys: list[str] = []
    i = 0
    while i < len(buf):
        b = buf[i : i + 1]
        if b == b"\x1b":
            if buf[i + 1 : i + 2] == b"[":
                final = buf[i + 2 : i + 3 : 1]
                if final:
                    keys.append(CSI.get(final.decode(), ""))
                    i += 3
                    continue
                keys.append("\x1b")
                i += 1
                continue
            keys.append(b.decode(errors="ignore"))
            i += 1
            continue
        keys.append(b.decode(errors="ignore"))
        i += 1
    return [k for k in keys if k]


class KeyboardSession:
    """Context manager that puts the terminal in raw mode and pumps keys."""

    def __init__(self, state: KeyState) -> None:
        self.state = state
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._saved = None

    def _loop(self) -> None:
        fd = sys.stdin.fileno()
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([fd], [], [], 0.05)
            except (OSError, ValueError):
                return
            if not r:
                continue
            try:
                buf = os_read(fd, 64)
            except OSError:
                return
            if not buf:
                return
            for key in _parse(buf):
                self.state.handle(key)

    def __enter__(self) -> KeyState:
        if not sys.stdin.isatty():
            raise SystemExit("keyboard control needs a real terminal (run it from a shell)")
        self._saved = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self.state

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.3)
        if self._saved is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._saved)


def os_read(fd: int, n: int) -> bytes:
    import os

    return os.read(fd, n)
