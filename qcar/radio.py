"""Transport abstraction between QCar and the platform Bluetooth stack.

Three backends implement the same small async interface:

===================  =========================================================
``BleakRadio``       bleak over BlueZ/D-Bus - desktop Linux (and macOS/Windows)
``AndroidRadio``     pyjnius -> ``android.bluetooth`` - the Android APK
``MockRadio``        an in-process virtual car - emulator, CI, demos
===================  ==========================================================

``make_radio()`` picks one: the ``QCAR_RADIO`` environment variable wins
(``bleak`` | ``android`` | ``mock``); otherwise ``android`` when running
inside the APK (python-for-android sets ``ANDROID_ARGUMENT``), ``bleak``
everywhere else. Keeping this seam is what lets the *same* GUI drive a real
car over BlueZ, a real car from a phone, and a simulated car inside an
emulator that has no Bluetooth at all.

The interface deliberately mirrors what the car's firmware needs, nothing
more: scanned advertisements filtered down to plausible QCARs, an ACKed
write, and notifications from one characteristic. Everything above this line
(frames, retries, the watchdog) is platform-independent.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

#: How the cars advertise themselves (see PROTOCOL.md / car.py).
NAME_PREFIX = "QCAR"
# 0x5452 == "TR" (Brandbase) - the value advertises the first two MAC bytes.
MANUFACTURER_IDS = (0x5452, 0x5254)

#: GATT service the cars expose (re-exported by car.py).
SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"

#: Client Characteristic Configuration descriptor - needed to subscribe.
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"


@dataclass(frozen=True)
class Discovered:
    """One over-the-air sighting of a plausible car."""

    address: str
    name: str | None = None
    rssi: int = 0


def looks_like_qcar(name: str | None, manufacturer_data=None) -> bool:
    """Name/manufacturer filter shared by every backend.

    ``manufacturer_data`` is whatever mapping the platform gives us
    (bleak: ``{int: bytes}``; Android: None - we match by name there).
    """
    if name and name.upper().startswith(NAME_PREFIX):
        return True
    mfg = manufacturer_data or {}
    try:
        return any(mid in mfg for mid in MANUFACTURER_IDS)
    except TypeError:  # not a mapping we understand -> name only
        return False


class Radio(ABC):
    """One radio handle. Cheap to create, one per scan or connection."""

    adapter: str | None = None

    @abstractmethod
    async def scan(
        self,
        timeout: float = 8.0,
        on_device: Callable[[Discovered], None] | None = None,
    ) -> list[Discovered]:
        """Scan for ~timeout seconds, filter to plausible cars.

        ``on_device`` is called as sightings arrive (from the event loop),
        so a UI can populate while the scan is still running. The returned
        list is the final, de-duplicated result sorted by signal strength.
        """

    @abstractmethod
    async def connect(self, address: str, timeout: float = 20.0) -> None:
        """Connect and discover services. Raises ConnectionError on failure."""

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def start_notify(
        self, char_uuid: str, cb: Callable[[bytes], None]
    ) -> None:
        """Subscribe to notifications; ``cb`` receives raw ciphertext frames."""

    @abstractmethod
    async def write(
        self, char_uuid: str, data: bytes, *, response: bool = True
    ) -> None:
        """Write a frame. ``response=True`` is an ACKed Write *Request* -
        the car silently drops unacknowledged writes, so callers must keep
        the default."""


def android_running() -> bool:
    """True inside a python-for-android APK (not just any Linux)."""
    return bool(os.environ.get("ANDROID_ARGUMENT"))


def make_radio(*, adapter: str | None = None) -> Radio:
    """Build the transport for this platform / QCAR_RADIO setting."""
    choice = os.environ.get("QCAR_RADIO", "").strip().lower()
    if not choice:
        choice = "android" if android_running() else "bleak"
    if choice == "mock":
        from .radio_mock import MockRadio

        return MockRadio(adapter=adapter)
    if choice == "android":
        from .radio_android import AndroidRadio

        return AndroidRadio(adapter=adapter)
    if choice == "bleak":
        from .radio_bleak import BleakRadio

        return BleakRadio(adapter=adapter)
    raise ValueError(
        f"QCAR_RADIO={choice!r} is not a backend (bleak|android|mock)"
    )
