"""On-device BLE bridge self-test - runs INSIDE the APK as the app entry point.

Pushed to files/app/main.py by tools/run_bridge_selftest.sh (the bootstrap
prefers main.pyc, so the harness renames it aside first). Exercises the
pyjnius layer of qcar/radio_android.py without Bluetooth hardware:

  1. class loading    - autoclass() of every JAVA_CLASSES entry against the
                        real runtime dex (catches bad FQNs, e.g. a
                        ScanSettings outside android.bluetooth.le)
  2. proxy creation   - the PythonJavaClass implementing the PyBleListener
                        interface, plus both Java bridge objects
  3. Java->Python     - every interface method fired through the JVM via the
                        BridgeSelfTest shim; recorded arguments are ASSERTED,
                        validating each @java_method JNI descriptor. byte[]
                        payloads are created on the Java side (like the
                        framework does) and cross exactly one JNI boundary,
                        fired repeatedly to catch ref/marshalling flakiness.

Still unverified here: actual BLE dispatch and GATT behaviour - that needs a
phone and a car. Results go to logcat ("I python", grep BRIDGE-SELFTEST).
"""

import os
import sys
import traceback

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
CCCD = "00002902-0000-1000-8000-00805f9b34fb"

rec = {}
failures = []


def check(name, fn):
    try:
        got = fn()
    except Exception as exc:
        failures.append(name)
        print(f"BRIDGE-SELFTEST: FAIL {name}: {exc!r}", flush=True)
        traceback.print_exc()
        return
    print(f"BRIDGE-SELFTEST: ok {name} -> {got!r}", flush=True)


def record(key):
    def _rec(*args):
        print(f"BRIDGE-SELFTEST: raw {key}: nargs={len(args)} "
              f"types={[type(a).__name__ for a in args]}", flush=True)
        rec[key] = args if len(args) != 1 else args[0]
    return _rec


def _norm(v):
    # pyjnius delivers byte[] args as jnius.ByteArray, not bytes - the
    # production handlers wrap with bytes() for the same reason. Only
    # array-like values are converted: bytes(int) would silently build a
    # zero-filled buffer of that length!
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    try:
        return bytes(v)
    except (TypeError, ValueError):
        return v


def main() -> None:
    from jnius import autoclass  # noqa: PLC0415 - on-device only

    from qcar.radio_android import JAVA_CLASSES, AndroidRadio, _classes

    # 1. class loading: every FQN must resolve against the runtime dex
    j = {}
    for name, fqn in JAVA_CLASSES.items():
        check(f"autoclass {name} = {fqn}", lambda n=name, f=fqn: j.setdefault(n, autoclass(f)))

    def scansettings_mode():
        mode = j["ScanSettings"].SCAN_MODE_LOW_LATENCY
        assert mode == 2, mode
        return mode

    check("ScanSettings.SCAN_MODE_LOW_LATENCY (android.bluetooth.le)", scansettings_mode)

    # 2. proxy creation - via the radio's own resolver, which also defines
    # the PythonJavaClass against the live JVM ("PyBleListenerImpl" is not a
    # JAVA_CLASSES entry; it is registered by _classes())
    radio = AndroidRadio()
    full = _classes()
    check("radio._classes() registers PyBleListenerImpl", lambda: full["PyBleListenerImpl"])
    listener = full["PyBleListenerImpl"](radio)
    scan_cb = full["PyScanCallback"](listener)
    gatt_cb = full["PyGattCallback"](listener)
    check("PyBleListener proxy created", lambda: type(listener).__name__)

    def supername(cb):
        return cb.getClass().getSuperclass().getName()

    check("PyScanCallback extends", lambda: supername(scan_cb))
    check("PyGattCallback extends", lambda: supername(gatt_cb))

    # 3. Java->Python: recorders assert the round-tripped arguments
    radio._cb_scan_hit = record("scan")
    radio._cb_scan_failed = record("failed")
    radio._cb_conn_state = record("conn")
    radio._cb_services = record("services")
    radio._cb_write = record("write")
    radio._cb_descriptor = record("desc")
    radio._cb_notify = record("notify")

    shim = autoclass("org.artalis.revlane.ble.BridgeSelfTest")

    def implements_interface():
        # proven on the Java side: the shim's fire*/checkInstance take a
        # PyBleListener parameter, which the proxy must actually implement
        ok = shim.checkInstance(listener)
        assert ok, "Java does not see the proxy as a PyBleListener"
        return ok

    check("proxy accepted as PyBleListener (Java instanceof)", implements_interface)

    def fired(key, expected, fire):
        def go():
            fire()
            got = rec[key]
            got = tuple(_norm(x) for x in got) if isinstance(got, tuple) else _norm(got)
            assert got == expected, f"got {got!r}, want {expected!r}"
            return got

        return go

    # every argument is constructed on the JAVA side - the same direction
    # production traffic flows (framework-created byte[], one JNI boundary);
    # each callback is fired with asserted, distinct values
    check("onNotify (String,String,byte[])",
          fired("notify", ("AA:11", SERVICE_UUID, b"\x01\x5a"),
                lambda: shim.fireNotify(listener, 1)))
    check("onNotify again (repeated dispatch)",
          fired("notify", ("AA:11", SERVICE_UUID, b"\x02\x5a"),
                lambda: shim.fireNotify(listener, 2)))

    check("onScanHit (String,String,int,int)",
          fired("scan", ("AA:11", "QCAR-1234", -40, 0x5452),
                lambda: shim.fireScanHit(listener)))
    check("onScanHit no-name (Java coerces null to \"\")",
          fired("scan", ("BB:22", "", -50, -1),
                lambda: shim.fireScanHitNoName(listener)))
    check("onScanFailed (int)",
          fired("failed", 3, lambda: shim.fireScanFailed(listener)))
    check("onGattState (String,int,int)",
          fired("conn", ("AA:11", 8, 0), lambda: shim.fireGattState(listener)))
    check("onServicesDiscovered (String,int)",
          fired("services", ("AA:11", 0), lambda: shim.fireServicesDiscovered(listener)))
    check("onCharacteristicWrite (String,String,int)",
          fired("write", ("AA:11", SERVICE_UUID, 0),
                lambda: shim.fireCharacteristicWrite(listener)))
    check("onDescriptorWrite (String,String,int)",
          fired("desc", ("AA:11", CCCD, 0),
                lambda: shim.fireDescriptorWrite(listener)))

    def via_bridge_object():
        scan_cb.onScanFailed(4)  # real PyScanCallback -> listener -> Python
        assert rec["failed"] == 4, rec["failed"]
        return rec["failed"]

    check("PyScanCallback.onScanFailed forwards to Python", via_bridge_object)

    if failures:
        print(f"BRIDGE-SELFTEST: VERDICT FAIL ({len(failures)} failed: {failures})", flush=True)
        sys.stdout.flush()
        os._exit(1)
    print("BRIDGE-SELFTEST: VERDICT PASS", flush=True)
    sys.stdout.flush()
    os._exit(0)


print("BRIDGE-SELFTEST: starting", flush=True)
try:
    main()
except Exception:
    traceback.print_exc()
    print("BRIDGE-SELFTEST: VERDICT FAIL (unhandled exception)", flush=True)
    sys.stdout.flush()
    os._exit(1)
