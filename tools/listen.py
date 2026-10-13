#!/usr/bin/env python3
"""Passive listener: connect, subscribe to all notify/indicate chars, log frames."""
import asyncio
import sys
import time

from bleak import BleakClient, BleakScanner

ADDR = "00:3C:DE:07:01:A4"
SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0


def show(b):
    p = "".join(chr(x) if 32 <= x < 127 else "." for x in b)
    return f"{bytes(b).hex(' ')}  |{p}|"


def t0():
    return f"{time.time() - START:7.3f}"


START = time.time()


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=25)
    if not dev:
        print("not found")
        return
    async with BleakClient(dev, timeout=30) as c:
        subs = []
        for svc in c.services:
            for ch in svc.characteristics:
                if "notify" in ch.properties or "indicate" in ch.properties:

                    def mk(uuid, handle):
                        def cb(_, data):
                            print(f"[{t0()}] NOTIF {uuid[:8]} h=0x{handle:04x} {show(data)}", flush=True)

                        return cb

                    cb = mk(ch.uuid, ch.handle)
                    try:
                        await c.start_notify(ch, cb)
                        subs.append(ch)
                        print(f"subscribed {ch.uuid[:8]} h=0x{ch.handle:04x}", flush=True)
                    except Exception as e:  # noqa: BLE001
                        print(f"sub fail {ch.uuid[:8]}: {e}", flush=True)
        print(f"listening {SECONDS}s ...", flush=True)
        await asyncio.sleep(SECONDS)
        for ch in subs:
            try:
                await c.stop_notify(ch)
            except Exception:  # noqa: BLE001
                pass


asyncio.run(main())
