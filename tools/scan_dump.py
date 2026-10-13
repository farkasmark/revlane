#!/usr/bin/env python3
"""Dump raw advertisement data for every BLE device BlueZ knows about."""
import sys
import time

import dbus

bus = dbus.SystemBus()
mgr = dbus.Interface(
    bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager"
)
SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0


def hx(b):
    return " ".join(f"{x:02x}" for x in b)


def dump():
    out = {}
    for path, ifaces in mgr.GetManagedObjects().items():
        if "org.bluez.Device1" not in ifaces:
            continue
        d = ifaces["org.bluez.Device1"]
        rec = {
            "addr": str(d.get("Address")),
            "name": str(d.get("Alias") or d.get("Name") or ""),
            "rssi": int(d.get("RSSI", 0)),
            "uuids": [str(u) for u in d.get("UUIDs", [])],
            "mfg": {},
            "svc": {},
            "flags": int(d.get("Flags", 0)),
            "cx": int(d.get("Connected", 0)),
        }
        for k, v in d.get("ManufacturerData", {}).items():
            rec["mfg"][f"0x{int(k):04x}"] = hx(bytes(bytearray(v)))
        for k, v in d.get("ServiceData", {}).items():
            rec["svc"][str(k)] = hx(bytes(bytearray(v)))
        out[rec["addr"]] = rec
    return out


# start discovery
for path, ifaces in mgr.GetManagedObjects().items():
    if "org.bluez.Adapter1" in ifaces:
        dbus.Interface(bus.get_object("org.bluez", path), "org.bluez.Adapter1").StartDiscovery()
        ctrl = path
time.sleep(SECONDS)
for path, ifaces in mgr.GetManagedObjects().items():
    if path == ctrl:
        dbus.Interface(bus.get_object("org.bluez", path), "org.bluez.Adapter1").StopDiscovery()
time.sleep(1)

for a, r in sorted(dump().items(), key=lambda kv: -kv[1]["rssi"]):
    print(f'{a}  rssi={r["rssi"]:4d}  name={r["name"]!r} conn={r["cx"]}')
    if r["uuids"]:
        print(f'     uuids: {", ".join(r["uuids"])}')
    for k, v in r["mfg"].items():
        print(f'     mfg  {k}: {v}')
    for k, v in r["svc"].items():
        print(f'     svc  {k}: {v}')
