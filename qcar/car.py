"""Async BLE client for Brandbase "QCAR" cars.

The radio itself is behind ``qcar.radio``: bleak over BlueZ on the desktop,
pyjnius over Android's BluetoothGatt inside the APK, and a virtual mock car
for the emulator and CI. Everything here - frames, retries, the watchdog - is
transport-agnostic.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable

from .protocol import Command, Telemetry, parse_telemetry
from .radio import (  # noqa: F401  (re-exported, used across the codebase)
    SERVICE_UUID,
    Discovered,
    looks_like_qcar,
    make_radio,
)

log = logging.getLogger("qcar")

CMD_CHAR_UUID = "d44bc439-abfd-45a2-b575-925416129600"
TEL_CHAR_UUID = "d44bc439-abfd-45a2-b575-925416129601"

# Undocumented extras seen on the same service.
AUX_CHAR_UUIDS = (
    "d44bc439-abfd-45a2-b575-92541612960a",  # user description "11A"
    "d44bc439-abfd-45a2-b575-92541612960b",
)

# advertising identity lives in radio.py (NAME_PREFIX, MANUFACTURER_IDS)

STOP = Command()


def is_qcar(device, adv=None) -> bool:
    """Does a scanned device look like a QCAR? (bleak-shaped objects)"""
    name = (device.name or (adv and getattr(adv, "local_name", None)) or "")
    mfg = {}
    if adv is not None:
        mfg = getattr(adv, "manufacturer_data", None) or {}
    return looks_like_qcar(name, mfg)


async def find(
    timeout: float = 8.0, *, adapter: str | None = None
) -> list[Discovered]:
    """Scan for QCAR cars. Returns every plausible candidate."""
    return await make_radio(adapter=adapter).scan(timeout=timeout)


class QCar:
    """One connected car.

    Usage::

        async with QCar("00:3C:DE:07:01:A4") as car:
            await car.stream(lambda: my_command)   # keeps the car alive
    """

    def __init__(
        self,
        address: str | None = None,
        *,
        adapter: str | None = None,
        scan_timeout: float = 20.0,
    ) -> None:
        if not address:
            raise ValueError("need an address")
        self.address = address.upper()
        self._adapter = adapter
        self._scan_timeout = scan_timeout
        self._radio = None
        self._listeners: list[Callable[[Telemetry], None]] = []
        self.telemetry: Telemetry | None = None
        self.last_tx = 0.0
        self.tx_count = 0

    # -- connection ------------------------------------------------------

    async def __aenter__(self) -> QCar:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    @property
    def connected(self) -> bool:
        return bool(self._radio and self._radio.connected)

    async def connect(self) -> None:
        self._radio = make_radio(adapter=self._adapter)
        await self._radio.connect(self.address, timeout=self._scan_timeout)
        await self._enable_notifications()
        log.info("connected to %s", self.address)

    async def disconnect(self) -> None:
        if self._radio is not None:
            with contextlib.suppress(Exception):
                await self.write(STOP)  # make sure we leave it stopped
            with contextlib.suppress(Exception):
                await self._radio.disconnect()
            self._radio = None

    async def _enable_notifications(self) -> None:
        def _cb(data: bytes) -> None:
            t = parse_telemetry(data)
            if t is not None:
                self.telemetry = t
                for fn in self._listeners:
                    with contextlib.suppress(Exception):
                        fn(t)

        with contextlib.suppress(Exception):
            await self._radio.start_notify(TEL_CHAR_UUID, _cb)

    def add_telemetry_listener(self, fn: Callable[[Telemetry], None]) -> None:
        self._listeners.append(fn)

    # -- traffic ---------------------------------------------------------

    async def write(self, cmd: Command, *, response: bool | None = None) -> None:
        """Send one command frame.

        Defaults to a real, ACKed **Write Request**.  This matters: the car's
        characteristic advertises both ``write`` and ``write-without-response``,
        but its firmware silently discards unacknowledged Write Commands -
        the ATT layer accepts them, the application layer never sees them.
        Confirmed on QCAR-0701A4: with Write Commands neither the motor nor the
        headlights react; with Write Requests both do.  Pass ``response=False``
        only if you know what you are doing.

        The ACK also makes rejections visible (vendor error ``0x80`` etc.
        instead of a quiet drop), at the cost of one round trip per frame -
        ~45 ms on the car's preferred connection interval, so ~22 Hz.
        """
        assert self._radio is not None
        frame = cmd.encrypt()
        if response is None:
            response = True
        await self._radio.write(CMD_CHAR_UUID, frame, response=response)
        self.last_tx = time.monotonic()
        self.tx_count += 1

    async def write_raw(
        self,
        data: bytes,
        *,
        char_uuid: str = CMD_CHAR_UUID,
        response: bool = True,
    ) -> None:
        assert self._radio is not None
        await self._radio.write(char_uuid, data, response=response)

    async def stream(
        self,
        get_command: Callable[[], Command | None],
        *,
        interval: float = 0.05,
        watchdog: float | None = None,
        idle: Command = STOP,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Keep the car fed with frames.

        ``get_command`` is polled every ``interval`` seconds.  Returning
        ``None``, or a watchdog expiry of more than ``watchdog`` seconds
        without a *changed* frame, degrades to ``idle`` (stopped).

        The watchdog latches: once it fires, ``idle`` keeps going out until
        the feeder produces a *different* command.  Re-issuing the stale one
        must not bring the car back to life, or a wedged feeder would
        alternate drive/stop forever instead of stopping - so input changes
        and transmitted fallbacks are tracked separately.
        """
        stop_event = stop_event or asyncio.Event()
        last_input: Command | None = None   # what the feeder last produced
        last_change = time.monotonic()      # when the input last changed
        expired = False                     # watchdog fired; stay idle
        while not stop_event.is_set():
            try:
                cmd = get_command()
                if cmd != last_input:
                    last_input = cmd
                    last_change = time.monotonic()
                    expired = False
                if watchdog is not None and not expired and \
                        time.monotonic() - last_change > watchdog:
                    expired = True
                out = idle if (cmd is None or expired) else cmd
                await self.write(out)
            except Exception as exc:  # link died -> let the caller decide
                log.warning("write failed: %s", exc)
                return
            await asyncio.sleep(interval)

    async def burst(
        self,
        cmd: Command,
        seconds: float = 1.0,
        interval: float = 0.05,
        *,
        response: bool | None = None,
    ) -> None:
        """Drive for ``seconds`` then stop (used by scripted moves)."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            await self.write(cmd, response=response)
            await asyncio.sleep(interval)
        await self.write(STOP, response=response)


@contextlib.asynccontextmanager
async def connected(*args, **kwargs):
    car = QCar(*args, **kwargs)
    await car.connect()
    try:
        yield car
    finally:
        await car.disconnect()


def first(devices: Iterable[Discovered]) -> Discovered | None:
    devices = list(devices)
    return devices[0] if devices else None
