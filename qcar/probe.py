"""Scripted field-by-field probe of an unknown QCAR-ish car.

Drives through each protocol field with a countdown so you can watch what the
car actually does, then dumps anything the car notifies in between.
"""

from __future__ import annotations

import asyncio
import time

from .car import AUX_CHAR_UUIDS, STOP, QCar
from .protocol import Command

# (label, seconds, command)
MOTION = [
    ("idle", 1.5, STOP),
    ("FORWARD", 0.7, Command(forward=True)),
    ("idle", 1.0, STOP),
    ("BACKWARD", 0.7, Command(backward=True)),
    ("idle", 1.0, STOP),
    ("STEER LEFT (still)", 1.2, Command(left=True)),
    ("STEER RIGHT (still)", 1.2, Command(right=True)),
    ("idle", 1.0, STOP),
    ("FWD+LEFT", 0.7, Command(forward=True, left=True)),
    ("idle", 1.0, STOP),
]

FLAGS = [
    ("byte8=0 (docs: lights ON)", 2.0, Command(lights=True)),
    ("idle", 1.0, STOP),
    ("byte8=1 (docs: lights off)", 2.0, Command(lights=False)),
    ("idle", 1.0, STOP),
]

SPEEDS = [(f"speed {v} fwd", 0.7, Command(forward=True, speed=v)) for v in (0x20, 0x40, 0x50, 0x64)]


def _fmt(cmd: Command) -> str:
    return cmd.to_plaintext().hex(" ")


async def sequence(car: QCar, steps, *, countdown: bool = True, verbose: bool = True) -> None:
    for label, secs, cmd in steps:
        if countdown:
            for i in range(3, 0, -1):
                print(f"   {label}: in {i} ...", end="\r", flush=True)
                await asyncio.sleep(0.4)
        if verbose:
            print(f"\n>> {label:<32} plain={_fmt(cmd)}")
        await car.burst(cmd, secs)
        await car.write(STOP)
        await asyncio.sleep(0.3)


async def probe_aux(car: QCar, *, countdown: bool = True) -> None:
    """Write the idle frame to the undocumented characteristics (always ACKed)."""
    idle = STOP.encrypt()
    for uuid in AUX_CHAR_UUIDS:
        if countdown:
            input(f"press enter to write idle frame to {uuid[-6:]} ... ")
        try:
            await car.write_raw(idle, char_uuid=uuid, response=True)
            print(f"   {uuid[-6:]}: idle frame ACKed")
        except Exception as exc:  # noqa: BLE001
            print(f"   {uuid[-6:]}: {exc}")
        await asyncio.sleep(0.5)

    fd02 = "0000fd02-0000-1000-8000-00805f9b34fb"
    for payload in (b"\x00", b"\x01", idle):
        try:
            await car.write_raw(payload, char_uuid=fd02, response=True)
            print(f"   fd02 <- {payload.hex() or '(empty)'}: ACKed")
        except Exception as exc:  # noqa: BLE001
            print(f"   fd02 <- {payload.hex() or '(empty)'}: {exc}")
        await asyncio.sleep(0.5)


async def run_probe(car: QCar, *, which: str = "all", countdown: bool = True) -> None:
    start = time.monotonic()
    print(f"probing {car.address} - watch the car")
    if which in ("all", "motion"):
        await sequence(car, MOTION, countdown=countdown)
    if which in ("all", "lights"):
        await sequence(car, FLAGS, countdown=countdown)
    if which in ("all", "speed"):
        await sequence(car, SPEEDS, countdown=countdown)
    if which in ("all", "aux"):
        await probe_aux(car, countdown=False)
    await car.write(STOP)
    print(f"\ndone in {time.monotonic() - start:.1f}s, tx={car.tx_count}, "
          f"telemetry={car.telemetry}")


async def probe_cmd(args) -> None:
    from .cli import resolve  # local import to avoid a cycle at module load

    address = await resolve(args)
    async with QCar(address, adapter=getattr(args, "adapter", None)) as car:
        await run_probe(car, which=args.which, countdown=not args.fast)
