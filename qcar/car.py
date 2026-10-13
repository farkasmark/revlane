"""Async BLE client for Brandbase "QCAR" cars."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice

from .protocol import Command, Telemetry, parse_telemetry

log = logging.getLogger("qcar")

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
CMD_CHAR_UUID = "d44bc439-abfd-45a2-b575-925416129600"
TEL_CHAR_UUID = "d44bc439-abfd-45a2-b575-925416129601"

# Undocumented extras seen on the same service.
AUX_CHAR_UUIDS = (
    "d44bc439-abfd-45a2-b575-92541612960a",  # user description "11A"
    "d44bc439-abfd-45a2-b575-92541612960b",
)

NAME_PREFIX = "QCAR"
# 0x5452 == "TR" (Brandbase) - the value advertises the first two MAC bytes.
MANUFACTURER_IDS = (0x5452, 0x5254)

STOP = Command()


def is_qcar(device: BLEDevice, adv=None) -> bool:
    name = (device.name or (adv and getattr(adv, "local_name", None)) or "")
    if name.upper().startswith(NAME_PREFIX):
        return True
    mfg = {}
    if adv is not None:
        mfg = getattr(adv, "manufacturer_data", None) or {}
    return any(mid in mfg for mid in MANUFACTURER_IDS)


async def find(timeout: float = 8.0, *, adapter: str | None = None) -> list[BLEDevice]:
    """Scan for QCAR cars. Returns every plausible candidate."""
    devices = await BleakScanner.discover(
        timeout=timeout,
        adapter=adapter,
        return_adv=True,
    )
    out = []
    for dev, adv in devices.values():
        if is_qcar(dev, adv):
            out.append(dev)
    return out


async def find_by_address(
    address: str, timeout: float = 20.0, *, adapter: str | None = None
) -> BLEDevice | None:
    return await BleakScanner.find_device_by_address(address, timeout=timeout, adapter=adapter)


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
        device: BLEDevice | None = None,
        adapter: str | None = None,
        scan_timeout: float = 20.0,
    ) -> None:
        if device is None and not address:
            raise ValueError("need an address or a BLEDevice")
        self.address = (address or device.address).upper()
        self._device = device
        self._adapter = adapter
        self._scan_timeout = scan_timeout
        self._client: BleakClient | None = None
        self._cmd_char: BleakGATTCharacteristic | None = None
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
        return bool(self._client and self._client.is_connected)

    async def connect(self) -> None:
        if self._device is None:
            self._device = await find_by_address(
                self.address, self._scan_timeout, adapter=self._adapter
            )
            if self._device is None:
                raise ConnectionError(
                    f"{self.address} not advertising. Is the car switched on? "
                    "If another program still holds the link, close it and "
                    "power-cycle the car."
                )
        self._client = BleakClient(self._device, adapter=self._adapter, timeout=30)
        await self._client.connect()
        self._cmd_char = self._client.services.get_characteristic(CMD_CHAR_UUID)
        if self._cmd_char is None:
            raise ConnectionError("car is connected but service fff0/char 9600 is missing")
        await self._enable_notifications()
        log.info("connected to %s", self.address)

    async def disconnect(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self.write(STOP)  # make sure we leave it stopped
            with contextlib.suppress(Exception):
                await self._client.disconnect()
            self._client = None

    async def _enable_notifications(self) -> None:
        tel = self._client.services.get_characteristic(TEL_CHAR_UUID)
        if tel is None:
            return

        def _cb(_char, data: bytearray) -> None:
            t = parse_telemetry(bytes(data))
            if t is not None:
                self.telemetry = t
                for fn in self._listeners:
                    with contextlib.suppress(Exception):
                        fn(t)

        with contextlib.suppress(Exception):
            await self._client.start_notify(tel, _cb)

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
        assert self._client is not None and self._cmd_char is not None
        frame = cmd.encrypt()
        if response is None:
            response = True
        await self._client.write_gatt_char(self._cmd_char, frame, response=response)
        self.last_tx = time.monotonic()
        self.tx_count += 1

    async def write_raw(
        self,
        data: bytes,
        *,
        char_uuid: str = CMD_CHAR_UUID,
        response: bool = True,
    ) -> None:
        assert self._client is not None
        ch = self._client.services.get_characteristic(char_uuid)
        if ch is None:
            raise ValueError(f"characteristic {char_uuid} not found")
        await self._client.write_gatt_char(ch, data, response=response)

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
        """
        stop_event = stop_event or asyncio.Event()
        last_seen: Command | None = None
        last_change = time.monotonic()
        while not stop_event.is_set():
            try:
                cmd = get_command()
                if cmd is None or (watchdog and time.monotonic() - last_change > watchdog):
                    cmd = idle
                if cmd != last_seen:
                    last_seen = cmd
                    last_change = time.monotonic()
                await self.write(cmd)
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


def first(devices: Iterable[BLEDevice]) -> BLEDevice | None:
    devices = list(devices)
    return devices[0] if devices else None
