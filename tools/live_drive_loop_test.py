#!/usr/bin/env python3
"""Live end-to-end test of the resilient DriveLoop against the real car.

Runs a scripted input source (so it needs no TTY) and deliberately hammers the
link at a rate faster than the car's connection interval to try to provoke the
ATT 0x0E "Unlikely Error" that crashed the first version of `drive`.
"""

import asyncio
import sys
import time

sys.path.insert(0, ".")

from qcar.car import QCar, find  # noqa: E402
from qcar.drive import DriveLoop  # noqa: E402
from qcar.protocol import Command  # noqa: E402

SCRIPT = [
    (0.0, Command()),
    (0.6, Command(forward=True)),
    (1.4, Command()),
    (2.0, Command(backward=True, speed=100)),
    (2.8, Command()),
    (3.2, Command(left=True)),
    (4.0, Command()),
]


def main() -> None:
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else 0.02
    cars = asyncio.run(find(timeout=8))
    if not cars:
        sys.exit("no car found")
    address = cars[0].address

    async def run() -> None:
        start = time.monotonic()
        done = {"quit": False}

        def get_command() -> Command:
            t = time.monotonic() - start
            cmd = Command()
            for at, c in SCRIPT:
                if t >= at:
                    cmd = c
            if t > SCRIPT[-1][0] + 1.0:
                done["quit"] = True
            return cmd

        lines: list[str] = []
        async with QCar(address) as car:
            loop = DriveLoop(
                car,
                get_command,
                interval=interval,
                should_quit=lambda: done["quit"],
                render=lambda cmd, extra: lines.append(str(cmd)),
                status=lambda: "",
            )
            await loop.run()
            print(
                f"address={address} interval={interval}s "
                f"tx={car.tx_count} retried_errors={loop.errors} "
                f"reconnects={loop.reconnects} redraws={len(lines)}"
            )
            print(f"last_error={loop.last_error!r}")
            print(f"telemetry={car.telemetry}")

    asyncio.run(run())


if __name__ == "__main__":
    main()
