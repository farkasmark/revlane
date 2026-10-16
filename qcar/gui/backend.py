"""Threaded BLE backend so the GUI never blocks on the radio.

The render loop must keep painting at 60 fps, while BLE writes are ACKed round
trips that take ~46 ms each.  So all Bluetooth lives on a dedicated thread with
its own asyncio loop, and the two sides talk through:

  GUI -> radio : `set_intent(Command)` (a lock-guarded assignment) and an
                 action queue for connect / scan / disconnect
  radio -> GUI : plain attributes polled once per frame (state, telemetry, tx,
                 errors, rssi)

The watchdog matters: if the GUI thread wedges (GC pause, modal dialog, the
user getting a phone call), we stop feeding it the last command and push STOP
instead, so a frozen window can never leave the car driving unattended.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future

from ..car import STOP, QCar
from ..protocol import Command, Telemetry
from ..radio import make_radio

log = logging.getLogger("qcar.backend")

IDLE = "offline"
SCANNING = "scanning"
CONNECTING = "connecting"
CONNECTED = "connected"
ERROR = "error"


@dataclasses.dataclass
class Discovered:
    address: str
    name: str
    rssi: int
    saved: bool = False

    @property
    def label(self) -> str:
        return self.name or self.address


class CarBackend:
    def __init__(
        self,
        *,
        adapter: str | None = None,
        interval: float = 0.08,
        watchdog: float = 0.5,
        on_event: Callable[[str, str], None] | None = None,
    ) -> None:
        self.adapter = adapter
        self.interval = interval
        self.watchdog = watchdog
        self.on_event = on_event or (lambda kind, msg: None)

        self.state = IDLE
        self.address: str | None = None
        self.error = ""
        self.telemetry: Telemetry | None = None
        self.tx = 0
        self.errors = 0
        self.rssi = 0
        self.fps_tx = 0.0
        self.found: list[Discovered] = []

        self._intent = Command()
        self._lock = threading.Lock()
        self._actions: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._car: QCar | None = None
        self._next_write = 0.0
        self._last_tickle = time.monotonic()
        self._rate_window: list[float] = []

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._bootstrap, daemon=True, name="qcar-ble")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _bootstrap(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.run_until_complete(self._cleanup())
            self._loop.close()

    # ---- API used by the GUI -------------------------------------------

    def set_intent(self, cmd: Command) -> None:
        with self._lock:
            self._intent = cmd
        self.tickle()

    def intent(self) -> Command:
        with self._lock:
            return self._intent

    def tickle(self) -> None:
        """Called by the GUI every frame; stale intent gets replaced by STOP."""
        self._last_tickle = time.monotonic()

    @property
    def gui_alive(self) -> bool:
        return time.monotonic() - self._last_tickle < self.watchdog

    def _submit(self, coro_factory) -> Future:
        fut: Future = Future()
        self._actions.put((coro_factory, fut))
        return fut

    def scan(self, timeout: float = 6.0) -> Future:
        return self._submit(lambda: self._do_scan(timeout))

    def connect(self, address: str) -> Future:
        return self._submit(lambda: self._do_connect(address))

    def disconnect(self) -> Future:
        return self._submit(self._do_disconnect)

    # ---- work ----------------------------------------------------------

    async def _do_scan(self, timeout: float) -> list[Discovered]:
        log.debug("scan starting")
        self.state = SCANNING
        self.error = ""
        self.found = []
        hits: dict[str, Discovered] = {}

        def on_adv(dev) -> None:
            # dev is a radio.Discovered sighting; the radio has already
            # filtered it down to plausible QCARs.
            hits[dev.address] = Discovered(
                address=dev.address,
                name=dev.name or dev.address,
                rssi=dev.rssi,
            )
            # Publish as we hear them. Waiting for the scan to finish would
            # leave the UI blank for seconds and makes a weak, far-away car
            # look absent when it is merely quiet. Rebinding the list is
            # atomic, so readers never see a torn one.
            self.found = sorted(hits.values(), key=lambda d: -d.rssi)

        try:
            await make_radio(adapter=self.adapter).scan(
                timeout=timeout, on_device=on_adv
            )
        finally:
            self.state = CONNECTED if self._car and self._car.connected else IDLE
        out = self.found
        self.on_event("scan", f"{len(out)} car(s) in range")
        return out

    async def _do_connect(self, address: str) -> None:
        # disconnect FIRST: it resets state to IDLE, and CONNECTING must be
        # set after that or the UI shows "offline" for the whole connect
        # (including the radio's retry window) - the "discoverable but
        # displays offline" bug
        await self._do_disconnect(quiet=True)
        self.state = CONNECTING
        self.error = ""
        car = QCar(address, adapter=self.adapter)
        await car.connect()
        self._car = car
        self.address = address
        self.telemetry = None
        self.state = CONNECTED
        self.set_intent(Command())
        self.on_event("connect", f"connected to {address}")

    async def _do_disconnect(self, quiet: bool = False) -> None:
        car, self._car = self._car, None
        if car is not None:
            for _ in range(4):  # belt and braces: leave it stopped
                try:
                    await car.write(STOP)
                except Exception:  # noqa: BLE001
                    break
                await asyncio.sleep(0.02)
            try:
                await car.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.address = None
        self.state = IDLE
        if not quiet:
            self.on_event("disconnect", "disconnected")

    async def _cleanup(self) -> None:
        try:
            await self._do_disconnect(quiet=True)
        except Exception:  # noqa: BLE001
            pass

    async def _main(self) -> None:
        while not self._stop.is_set():
            try:
                factory, fut = self._actions.get_nowait()
            except queue.Empty:
                factory = None
            if factory is not None:
                try:
                    fut.set_result(await factory())
                except BaseException as exc:  # noqa: BLE001
                    # warning level: this is the only trace a swallowed
                    # action leaves, and it is how on-device failures were
                    # first seen (logcat carries python warnings)
                    log.warning("action failed: %r", exc)
                    self.error = str(exc) or exc.__class__.__name__
                    self.state = ERROR if self._car is None else CONNECTED
                    if not fut.done():
                        fut.set_exception(exc)

            car = self._car
            if car is not None and time.monotonic() >= self._next_write:
                self._next_write = time.monotonic() + self.interval
                cmd = self.intent() if self.gui_alive else STOP
                try:
                    await car.write(cmd)
                    self.tx += 1
                    self.errors = 0
                    self._rate_window.append(time.monotonic())
                    if self._car is not None:
                        self.telemetry = self._car.telemetry
                except Exception as exc:  # noqa: BLE001
                    self.errors += 1
                    self.error = str(exc) or exc.__class__.__name__
                    if self.errors > 20:
                        self.on_event("error", f"link lost after {self.errors} writes: {self.error}")
                        await self._do_disconnect(quiet=True)
            self._prune_rate()
            await asyncio.sleep(0.004)

    def _prune_rate(self) -> None:
        cutoff = time.monotonic() - 1.0
        self._rate_window = [t for t in self._rate_window if t > cutoff]
        self.fps_tx = float(len(self._rate_window))
