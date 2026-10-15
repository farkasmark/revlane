"""QCar.stream(): the watchdog must latch, not oscillate.

Regression test for a review finding: the fallback STOP used to refresh the
change-timer, so an unchanged driving command came back to life on the very
next iteration and the car alternated drive/stop forever after expiry.
Input changes and transmitted fallbacks are now tracked separately.
"""

import asyncio

from qcar.car import STOP, QCar
from qcar.protocol import Command

FORWARD = Command(forward=True)
FRESH = Command(forward=True, lights=True)
LEFT = Command(left=True)


class RecordingCar:
    """Records everything stream() transmits; no radio needed."""

    def __init__(self) -> None:
        self.frames: list[Command] = []

    async def write(self, cmd, *, response=None) -> None:
        self.frames.append(cmd)


async def test_watchdog_latches_stopped_until_fresh_input():
    car = RecordingCar()
    state = {"cmd": FORWARD}
    stop = asyncio.Event()

    async def run():
        await QCar.stream(
            car, lambda: state["cmd"],
            interval=0.005, watchdog=0.05, stop_event=stop,
        )

    task = asyncio.create_task(run())
    await asyncio.sleep(0.2)  # several watchdog windows worth of polls

    frames = car.frames
    assert frames[0] == FORWARD, "drives until the watchdog fires"
    assert STOP in frames, "watchdog degrades to idle"
    first_stop = frames.index(STOP)
    tail = frames[first_stop:]
    assert len(tail) >= 10, "several iterations must pass after expiry"
    assert all(f == STOP for f in tail), (
        "stale driving command must not resume after watchdog expiry"
    )

    # a genuinely fresh input (different command) resumes control
    state["cmd"] = FRESH
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 2)

    assert FRESH in frames[first_stop:], "fresh input resumes after expiry"
    assert car.frames[-1] == FRESH


async def test_returning_to_the_stale_command_still_does_not_resume():
    # expiry while forward -> feeder briefly switches -> returns to the
    # original forward: that IS a fresh change and may resume, but switching
    # back and forth must never resurrect a command the watchdog stopped
    # without an actual input change. The contract under test: only real
    # input changes clear the latch.
    car = RecordingCar()
    state = {"cmd": FORWARD}
    stop = asyncio.Event()

    async def run():
        await QCar.stream(
            car, lambda: state["cmd"],
            interval=0.005, watchdog=0.05, stop_event=stop,
        )

    task = asyncio.create_task(run())
    await asyncio.sleep(0.15)  # expired by now, sending STOP
    first_stop = car.frames.index(STOP)
    assert all(f == STOP for f in car.frames[first_stop:])

    state["cmd"] = LEFT  # fresh input: latch clears
    await asyncio.sleep(0.03)
    assert car.frames[-1] == LEFT

    state["cmd"] = FORWARD  # another fresh input: drives again
    await asyncio.sleep(0.03)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert car.frames[-1] == FORWARD


async def test_no_watchdog_keeps_streaming_unchanged_command():
    car = RecordingCar()
    stop = asyncio.Event()

    async def run():
        await QCar.stream(
            car, lambda: FORWARD, interval=0.005, stop_event=stop,
        )

    task = asyncio.create_task(run())
    await asyncio.sleep(0.06)
    stop.set()
    await asyncio.wait_for(task, 2)

    assert car.frames
    assert all(f == FORWARD for f in car.frames), (
        "without a watchdog an unchanged command just keeps streaming"
    )


async def test_none_input_degrades_to_idle_and_recovers():
    car = RecordingCar()
    state = {"cmd": None}
    stop = asyncio.Event()

    async def run():
        await QCar.stream(
            car, lambda: state["cmd"], interval=0.005, stop_event=stop,
        )

    task = asyncio.create_task(run())
    await asyncio.sleep(0.04)
    assert car.frames and all(f == STOP for f in car.frames), (
        "None degrades to idle"
    )

    state["cmd"] = FORWARD
    await asyncio.sleep(0.03)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert car.frames[-1] == FORWARD, "feeder waking up resumes driving"


async def test_changing_input_never_trips_the_watchdog():
    car = RecordingCar()
    stop = asyncio.Event()
    seq = [FORWARD, FRESH, LEFT]
    polls = {"n": 0}

    def feeder():
        cmd = seq[(polls["n"] // 3) % len(seq)]  # each command lives 3 polls
        polls["n"] += 1
        return cmd

    async def run():
        await QCar.stream(
            car, feeder, interval=0.005, watchdog=0.05, stop_event=stop,
        )

    task = asyncio.create_task(run())
    await asyncio.sleep(0.15)  # 30 polls; every command changes within 15 ms
    stop.set()
    await asyncio.wait_for(task, 2)

    assert car.frames
    assert STOP not in car.frames, (
        "input that keeps changing must never trip the watchdog"
    )
