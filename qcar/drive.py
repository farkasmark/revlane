"""Robust command loop shared by `drive` and `gamepad`.

The car's GATT server is a cheap embedded stack: under load it sometimes
answers a Write Request with ATT error 0x0E ``Unlikely Error`` instead of
acknowledging, and the Bluetooth link can blip when the LiPo sags during a
turbo burst.  A controller must ride that out rather than die with a traceback.

Safety comes for free from the car itself: it goes limp whenever the frame
stream pauses, so dropping a few frames (or even the whole connection) always
degrades to *stopped*, never to *runaway*.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from bleak.exc import BleakError

from .car import STOP, QCar
from .protocol import Command

log = logging.getLogger("qcar.drive")

#: errors worth retrying, as opposed to "the car walked away"
TRANSIENT = (
    BleakError,
    asyncio.TimeoutError,
    TimeoutError,
    OSError,
)


class DriveLoop:
    """Feed the car frames until ``should_quit()`` is true.

    ``get_command`` returns the frame to send.  While the link is unhealthy we
    send nothing (the car is stopped anyway) and try to re-establish it.
    """

    def __init__(
        self,
        car: QCar,
        get_command: Callable[[], Command],
        *,
        interval: float = 0.08,
        max_retries: int = 5,
        reconnect_attempts: int = 3,
        status: Callable[[], str] = lambda: "",
        render: Callable[[Command, str], None] | None = None,
        should_quit: Callable[[], bool] = lambda: False,
    ) -> None:
        self.car = car
        self.get_command = get_command
        self.interval = interval
        self.max_retries = max_retries
        self.reconnect_attempts = reconnect_attempts
        self.status = status
        self.render = render
        self.should_quit = should_quit
        self.last_error = ""
        self.errors = 0
        self.reconnects = 0
        self._last_render = 0.0

    async def run(self) -> None:
        while not self.should_quit():
            cmd = self.get_command()
            ok = await self._send(cmd)
            self._draw(cmd if ok else STOP)
            await asyncio.sleep(0 if not ok else self.interval)

    async def _send(self, cmd: Command) -> bool:
        """One write, with retry and reconnect.  Returns True if it went out."""
        try:
            await self.car.write(cmd)
        except TRANSIENT as exc:
            self.errors += 1
            self.last_error = str(exc) or exc.__class__.__name__
            log.debug("write failed (%s)", self.last_error)
            if self.errors > self.max_retries:
                return await self._reconnect()
            # back off a little; the stack is usually busy, not dead
            await asyncio.sleep(0.05 * self.errors)
            return False
        self.errors = 0
        return True

    async def _reconnect(self) -> bool:
        for attempt in range(1, self.reconnect_attempts + 1):
            self._draw(STOP, f"link lost, reconnecting {attempt}/{self.reconnect_attempts}")
            log.warning("reconnecting (%s): %s", attempt, self.last_error)
            try:
                await self.car.disconnect()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5 * attempt)
            try:
                # QCar.connect() builds a fresh transport, which is all the
                # reset a post-seam car needs
                await self.car.connect()
                # Best-effort stop frame. It is fine if this fails: the car has
                # already gone limp because the frame stream paused, and
                # refusing to declare the link up here would be worse.
                with contextlib.suppress(Exception):
                    await self.car.write(STOP)
                self.reconnects += 1
                self.errors = 0
                self.last_error = ""
                return True
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.warning("reconnect %s failed: %s", attempt, exc)
        raise ConnectionError(f"lost the car, giving up: {self.last_error}")

    def _draw(self, cmd: Command, extra: str | None = None) -> None:
        if self.render is None:
            return
        now = time.monotonic()
        if now - self._last_render < 0.1 and not extra:
            return
        self._last_render = now
        self.render(cmd, extra if extra is not None else self.status())
