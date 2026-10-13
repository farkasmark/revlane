"""DriveLoop must survive a cranky GATT server, not crash."""

import asyncio

import pytest
from bleak.exc import BleakError, BleakGATTProtocolError, BleakGATTProtocolErrorCode

from qcar.drive import DriveLoop
from qcar.protocol import Command


def _err():
    """Exactly what the car answers when its ATT server is busy."""
    return BleakGATTProtocolError(BleakGATTProtocolErrorCode.UNLIKELY_ERROR)


class FakeCar:
    def __init__(self, fail=0, always_fail=False, reconnect_ok=True):
        self.frames: list[Command] = []
        self.fail = fail
        self.always_fail = always_fail
        self.reconnect_ok = reconnect_ok
        self.reconnects = 0
        self.disconnects = 0
        self.telemetry = None
        self.tx_count = 0

    async def write(self, cmd, *, response=None):
        if self.always_fail or self.fail > 0:
            self.fail -= 1
            raise _err()
        self.frames.append(cmd)
        self.tx_count += 1

    async def disconnect(self):
        self.disconnects += 1

    async def connect(self):
        self.reconnects += 1
        if not self.reconnect_ok:
            raise BleakError("not advertising")


async def test_recovers_from_transient_errors():
    car = FakeCar(fail=3)
    done = {"quit": False}
    loop = DriveLoop(
        car,
        lambda: Command(forward=True),
        interval=0.0,
        max_retries=9,
        should_quit=lambda: done["quit"],
    )

    async def stop_soon():
        # long enough to cover the retry backoff (0.05 + 0.10 + 0.15 s)
        await asyncio.sleep(0.5)
        done["quit"] = True

    task = asyncio.create_task(loop.run())
    await stop_soon()
    await asyncio.wait_for(task, timeout=3)

    assert car.frames, "kept driving after the errors"
    assert loop.errors == 0, "error counter reset by the next good write"
    assert "Unlikely" in loop.last_error  # remembered for diagnostics, not fatal


async def test_reconnects_when_errors_pile_up():
    car = FakeCar(fail=100, reconnect_ok=True)
    done = {"quit": False}
    loop = DriveLoop(
        car,
        lambda: Command(),
        interval=0.0,
        max_retries=2,
        reconnect_attempts=2,
        should_quit=lambda: done["quit"],
    )

    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.3)
    done["quit"] = True
    await asyncio.wait_for(task, timeout=3)

    assert car.reconnects >= 1
    assert car.disconnects >= 1
    assert loop.reconnects >= 1


async def test_gives_up_with_a_clean_error():
    car = FakeCar(always_fail=True, reconnect_ok=False)
    loop = DriveLoop(
        car, lambda: Command(), interval=0.0, max_retries=1, reconnect_attempts=2
    )
    with pytest.raises(ConnectionError, match="lost the car"):
        await asyncio.wait_for(loop.run(), timeout=10)
    assert loop.reconnects == 0, "no reconnect actually succeeded"
    assert car.reconnects == 2, "both attempts were made"


async def test_stops_when_asked():
    car = FakeCar()
    done = {"quit": False}
    loop = DriveLoop(car, lambda: Command(), interval=0.001, should_quit=lambda: done["quit"])
    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.05)
    done["quit"] = True
    await asyncio.wait_for(task, timeout=2)
    assert car.frames


async def test_render_is_throttled():
    car = FakeCar()
    calls = []
    done = {"quit": False}
    loop = DriveLoop(
        car,
        lambda: Command(),
        interval=0.0,
        render=lambda cmd, extra: calls.append(extra),
        status=lambda: "status-text",
        should_quit=lambda: done["quit"],
    )
    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.12)
    done["quit"] = True
    await asyncio.wait_for(task, timeout=2)
    # thousands of writes, but only a handful of redraws
    assert 0 < len(calls) < 50
