"""bleak/BlueZ transport - the desktop backend.

Behaviour is deliberately identical to the original bleak code that lived in
car.py: streaming scan with a detection callback (so the garage populates as
cars are heard, not after the scan finishes), ACKed writes by default, and
notifications via BleakClient.start_notify.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from .radio import Discovered, Radio, looks_like_qcar

log = logging.getLogger("qcar")


class BleakRadio(Radio):
    def __init__(self, *, adapter: str | None = None) -> None:
        self.adapter = adapter
        self._client = None

    # -- scanning --------------------------------------------------------

    async def scan(
        self,
        timeout: float = 8.0,
        on_device: Callable[[Discovered], None] | None = None,
    ) -> list[Discovered]:
        from bleak import BleakScanner

        hits: dict[str, Discovered] = {}

        def on_adv(dev, adv) -> None:
            name = dev.name or getattr(adv, "local_name", None)
            mfg = getattr(adv, "manufacturer_data", None)
            if not looks_like_qcar(name, mfg):
                return
            hit = Discovered(
                address=dev.address,
                name=name,
                rssi=getattr(adv, "rssi", 0) or 0,
            )
            hits[dev.address] = hit
            # Publish as we hear them: a weak, far-away car should look
            # present while the scan is still running, not absent.
            if on_device is not None:
                on_device(hit)

        scanner = BleakScanner(detection_callback=on_adv, adapter=self.adapter)
        await scanner.start()
        try:
            await asyncio.sleep(timeout)
        finally:
            await scanner.stop()
        return sorted(hits.values(), key=lambda d: -d.rssi)

    # -- connection ------------------------------------------------------

    async def connect(self, address: str, timeout: float = 20.0) -> None:
        from bleak import BleakClient, BleakScanner

        dev = await BleakScanner.find_device_by_address(
            address, timeout=timeout, adapter=self.adapter
        )
        if dev is None:
            raise ConnectionError(
                f"{address} not advertising. Is the car switched on? "
                "If another program still holds the link, close it and "
                "power-cycle the car."
            )
        self._client = BleakClient(dev, adapter=self.adapter, timeout=30)
        await self._client.connect()
        log.info("bleak: connected to %s", address)

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    async def disconnect(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
            self._client = None

    # -- traffic ---------------------------------------------------------

    def _characteristic(self, char_uuid: str):
        ch = self._client.services.get_characteristic(char_uuid)
        if ch is None:
            raise ValueError(f"characteristic {char_uuid} not found")
        return ch

    async def start_notify(
        self, char_uuid: str, cb: Callable[[bytes], None]
    ) -> None:
        tel = self._client.services.get_characteristic(char_uuid)
        if tel is None:
            return

        def _cb(_char, data: bytearray) -> None:
            cb(bytes(data))

        with contextlib.suppress(Exception):
            await self._client.start_notify(tel, _cb)

    async def write(
        self, char_uuid: str, data: bytes, *, response: bool = True
    ) -> None:
        ch = self._characteristic(char_uuid)
        await self._client.write_gatt_char(ch, data, response=response)
