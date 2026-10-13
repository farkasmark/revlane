#!/usr/bin/env python3
"""Dump the full GATT table of a BLE device (services, chars, descriptors, flags)."""
import asyncio
import sys

from bleak import BleakClient, BleakScanner

ADDR = sys.argv[1] if len(sys.argv) > 1 else "00:3C:DE:07:01:A4"


def show(val):
    if isinstance(val, (bytes, bytearray)):
        p = "".join(chr(b) if 32 <= b < 127 else "." for b in val)
        return f"{bytes(val).hex(' ')}  |{p}|"
    return str(val)


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=25)
    if not dev:
        print("device not found (advertising off? disconnected?)")
        return
    async with BleakClient(dev, timeout=30) as c:
        print(f"connected={c.is_connected}  mtu={getattr(c, 'mtu_size', '?')}")
        for svc in c.services:
            print(f"\nService {svc.uuid}  handle=0x{svc.handle:04x}")
            for ch in svc.characteristics:
                props = ",".join(ch.properties)
                print(f"  Char {ch.uuid}  h=0x{ch.handle:04x}  [{props}]")
                for d in ch.descriptors:
                    try:
                        val = await c.read_gatt_descriptor(d.handle)
                    except Exception as e:  # noqa: BLE001
                        val = f"<err {e}>"
                    print(f"      Desc {d.uuid} h=0x{d.handle:04x} = {show(val)}")
                if "read" in ch.properties:
                    try:
                        val = await c.read_gatt_char(ch)
                        print(f"      READ -> {show(val)}")
                    except Exception as e:  # noqa: BLE001
                        print(f"      READ -> <err {e}>")


asyncio.run(main())
