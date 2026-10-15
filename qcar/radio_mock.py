"""A virtual QCAR in the same process - the emulator / CI / demo backend.

Implements the Radio interface against a tiny simulation of the car's
firmware: it decrypts command frames, tracks drive state, and emits the same
AES-encrypted ``VBT`` telemetry notifications the real car does, at the same
~46 ms ACKed-write pace. The garage GUI, the drive pad, the watchdog and the
whole transport seam get exercised without any radio at all - which is the
only way to run it inside the Android emulator (no Bluetooth there).

Not a road-legal driving simulator. Battery drain is optimistic.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from .crypto import BLOCK_SIZE, decrypt_block, encrypt_block
from .protocol import CMD_MAGIC, TEL_MAGIC, Command
from .radio import Discovered, Radio

log = logging.getLogger("qcar")

#: plausible-looking, locally administered, clearly fake
MOCK_ADDRESS = "00:1A:7D:DA:71:13"
MOCK_NAME = "QCAR-MOCK"

#: ACKed write round trip on the real car (~22 Hz link)
ACK_LATENCY = 0.045
#: the real car notifies about twice a second
TELEMETRY_PERIOD = 0.5


class MockRadio(Radio):
    def __init__(self, *, adapter: str | None = None) -> None:
        self.adapter = adapter  # accepted for API parity; meaningless here
        self._link_up = False
        self._notify_task: asyncio.Task | None = None
        self._notify_cb: Callable[[bytes], None] | None = None
        self._seq = 0
        self._battery = 100
        self._cmd = Command()
        #: last decrypted command, for tests and demos
        self.last_command = Command()

    # -- scanning --------------------------------------------------------

    async def scan(
        self,
        timeout: float = 8.0,
        on_device: Callable[[Discovered], None] | None = None,
    ) -> list[Discovered]:
        hits = [
            Discovered(address=MOCK_ADDRESS, name=MOCK_NAME, rssi=-42),
            # a decoy: must be filtered out by the QCAR name/mfg rules
            Discovered(address="11:22:33:44:55:66", name="SOME_HEADPHONES", rssi=-30),
        ]
        # pretend the airwaves take a moment, but stay snappy in tests
        await asyncio.sleep(min(0.4, max(0.0, timeout / 8)))
        if on_device is not None:
            for hit in hits:
                if hit.name and hit.name.upper().startswith("QCAR"):
                    on_device(hit)
        return [h for h in hits if h.name and h.name.upper().startswith("QCAR")]

    # -- connection ------------------------------------------------------

    async def connect(self, address: str, timeout: float = 20.0) -> None:
        if address.upper() != MOCK_ADDRESS:
            raise ConnectionError(
                f"{address} not advertising. Is the car switched on? "
                f"(the mock car is {MOCK_ADDRESS} - set QCAR_RADIO=mock "
                "and scan again)"
            )
        await asyncio.sleep(0.05)  # pretend the handshake takes a moment
        self._link_up = True
        self._battery = 100
        log.info("mock: connected to %s", address)

    @property
    def connected(self) -> bool:
        return self._link_up

    async def disconnect(self) -> None:
        self._link_up = False
        task, self._notify_task = self._notify_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # -- traffic ---------------------------------------------------------

    async def start_notify(
        self, char_uuid: str, cb: Callable[[bytes], None]
    ) -> None:
        self._notify_cb = cb
        self._notify_task = asyncio.create_task(self._telemetry_loop())

    async def _telemetry_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(TELEMETRY_PERIOD)
                self._seq = (self._seq + 1) % 256
                if self._cmd.moving:
                    self._battery = max(0, self._battery - 1)
                elif self._seq % 8 == 0:  # slow idle drain
                    self._battery = max(0, self._battery - 1)
                self._notify(self._frame())
        except asyncio.CancelledError:
            raise

    def _frame(self) -> bytes:
        p = bytearray(BLOCK_SIZE)
        p[0] = self._seq
        p[1:4] = TEL_MAGIC
        p[4] = self._battery
        return encrypt_block(bytes(p))

    def _notify(self, frame: bytes) -> None:
        if self._notify_cb is not None:
            with contextlib.suppress(Exception):
                self._notify_cb(frame)

    async def write(
        self, char_uuid: str, data: bytes, *, response: bool = True
    ) -> None:
        # honour the real car's timing so GUI-side pacing/watchdog code is
        # exercised the same way it will run against hardware
        await asyncio.sleep(ACK_LATENCY if response else 0.005)
        try:
            p = decrypt_block(bytes(data))
        except ValueError:
            return
        if p[1:4] != CMD_MAGIC:
            return
        self._cmd = Command(
            forward=bool(p[4]),
            backward=bool(p[5]) and not p[4],
            left=bool(p[6]),
            right=bool(p[7]) and not p[6],
            lights=(p[8] == 0),  # inverted, like the firmware
            speed=p[9],
        )
        self.last_command = self._cmd
