"""Off-device validation of the Android radio, as far as this environment allows.

No Android runtime runs here (and the mock radio proves nothing about the
real Bluetooth stack), so these tests pin down everything that *can* be
checked locally:

* every framework class the radio resolves at runtime really exists in the
  SDK's android.jar, and ScanSettings is resolved from ``android.bluetooth.le``
  (regression: it used to say ``android.bluetooth.ScanSettings``, which does
  not exist - the scanner would have died on its first scan);
* the Java bridge sources (android-src/) extend/implement the right classes
  and compile with javac against that same android.jar;
* a built APK (bin/*.apk, when present) actually contains the compiled
  bridge classes;
* the Python callback handlers filter cars, marshal onto the event loop and
  resolve the per-operation futures.

Real radio behaviour can only be validated on a phone with an actual car.
"""

import asyncio
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from qcar.radio import Discovered
from qcar.radio_android import JAVA_CLASSES, AndroidRadio, _CallbackRouter

REPO = Path(__file__).resolve().parent.parent
BRIDGE_DIR = REPO / "android-src" / "org" / "artalis" / "revlane" / "ble"

# framework classes only - the bridge lives in the APK, PythonActivity in the
# p4a bootstrap, neither is part of android.jar
SDK_CLASSES = {
    name: fqn for name, fqn in JAVA_CLASSES.items() if fqn.startswith(("android.", "java."))
}


def _android_jar() -> Path | None:
    jars = sorted(Path.home().glob(".buildozer/android/platform/android-sdk/platforms/*/android.jar"))
    return jars[-1] if jars else None


def _javac() -> str | None:
    found = shutil.which("javac")
    if found:
        return found
    for jdk in sorted(Path.home().glob(".jdks/jdk-*/bin/javac")):
        return str(jdk)
    return None


def test_scansettings_is_resolved_from_the_le_package():
    # android.bluetooth.ScanSettings does not exist; the scanner would throw
    # ClassNotFoundException on its first call
    assert JAVA_CLASSES["ScanSettings"] == "android.bluetooth.le.ScanSettings"
    assert JAVA_CLASSES["ScanSettingsBuilder"] == "android.bluetooth.le.ScanSettings$Builder"
    for fqn in JAVA_CLASSES.values():
        assert fqn != "android.bluetooth.ScanSettings"


def test_framework_classes_exist_in_sdk_android_jar():
    jar = _android_jar()
    if jar is None:
        pytest.skip("no android SDK android.jar found")
    with zipfile.ZipFile(jar) as z:
        listing = set(z.namelist())
    for name, fqn in SDK_CLASSES.items():
        entry = f"{fqn.replace('.', '/')}.class"
        assert entry in listing, f"{name}: {fqn} not in {jar}"


def test_bridge_sources_declare_the_right_hierarchy():
    listener = (BRIDGE_DIR / "PyBleListener.java").read_text()
    scan = (BRIDGE_DIR / "PyScanCallback.java").read_text()
    gatt = (BRIDGE_DIR / "PyGattCallback.java").read_text()
    assert "public interface PyBleListener" in listener
    assert "extends ScanCallback" in scan
    assert "extends BluetoothGattCallback" in gatt
    assert JAVA_CLASSES["PyBleListener"] == "org.artalis.revlane.ble.PyBleListener"
    assert JAVA_CLASSES["PyScanCallback"] == "org.artalis.revlane.ble.PyScanCallback"
    assert JAVA_CLASSES["PyGattCallback"] == "org.artalis.revlane.ble.PyGattCallback"


def test_java_bridge_compiles_against_android_jar(tmp_path):
    jar, javac = _android_jar(), _javac()
    if jar is None or javac is None:
        pytest.skip("need javac and an android.jar to compile the bridge")
    subprocess.run(
        [javac, "-source", "8", "-target", "8", "-classpath", str(jar),
         "-d", str(tmp_path), *map(str, BRIDGE_DIR.glob("*.java"))],
        capture_output=True, text=True, check=True,
    )
    produced = {p.name for p in tmp_path.rglob("*.class")}
    assert produced == {
        "PyBleListener.class",
        "PyScanCallback.class",
        "PyGattCallback.class",
        "BridgeSelfTest.class",  # test-only shim for tools/run_bridge_selftest.sh
    }


def test_apk_contains_compiled_bridge_classes():
    apks = sorted((REPO / "bin").glob("*.apk"))
    if not apks:
        pytest.skip("no APK built (bin/ is empty) - run buildozer android debug")
    apk = apks[-1]
    with zipfile.ZipFile(apk) as z:
        dex = b"".join(z.read(n) for n in z.namelist() if n.endswith(".dex"))
    for cls in (b"PyBleListener", b"PyScanCallback", b"PyGattCallback"):
        assert cls in dex, f"{cls.decode()} missing from {apk.name}"


# ----------------------------------------------------------------------
# Python-side callback behaviour (binder-thread entry points)

def test_scan_hit_filters_on_name_and_keeps_mfg_fallback():
    radio = AndroidRadio()

    radio._cb_scan_hit("AA:11", "QCAR-1234", -40, -1)
    radio._cb_scan_hit("BB:22", "SoundBuds", -30, -1)  # wrong name
    radio._cb_scan_hit("CC:33", "Sneaker", -90, 0x1234)  # wrong both
    radio._cb_scan_hit("", "QCAR-1234", -10, -1)  # no address (Java coerces)
    radio._cb_scan_hit("DD:44", "", -50, 0x5452)  # mfg fallback
    radio._cb_scan_hit("EE:55", "QCAR-9999", -20, 0x1234)  # name wins

    # pure filtering contract: what the radio recorded for this scan
    hits = {d.address: d for d in radio._scan_hits.values()}
    assert set(hits) == {"AA:11", "DD:44", "EE:55"}
    assert hits["AA:11"] == Discovered(address="AA:11", name="QCAR-1234", rssi=-40)
    assert hits["DD:44"] == Discovered(address="DD:44", name="", rssi=-50)


async def test_scan_hit_streams_to_callback_on_the_loop():
    radio = AndroidRadio()
    radio._loop = asyncio.get_running_loop()
    seen: list[Discovered] = []
    radio._scan_on_device = seen.append

    radio._cb_scan_hit("AA:11", "QCAR-1234", -40, -1)
    radio._cb_scan_hit("AA:11", "QCAR-1234", -35, -1)  # repeat: no re-emit
    await asyncio.sleep(0)
    assert len(seen) == 1, "a car is announced once per scan"


async def test_gatt_callbacks_resolve_operation_futures():
    radio = AndroidRadio()
    radio._loop = asyncio.get_running_loop()

    fut = radio._connect_fut = radio._loop.create_future()
    radio._cb_services("AA:11", 0)
    await asyncio.wait_for(fut, 1)  # discovery ok -> connect() proceeds

    fut = radio._write_fut = radio._loop.create_future()
    radio._cb_write("AA:11", "fff0", 0)
    await asyncio.wait_for(fut, 1)  # ACKed write completed

    fut = radio._desc_fut = radio._loop.create_future()
    radio._cb_descriptor("AA:11", "fff1", 0)
    await asyncio.wait_for(fut, 1)  # subscribed to telemetry

    fut = radio._connect_fut = radio._loop.create_future()
    radio._cb_conn_state("AA:11", 8, 0)  # STATE_DISCONNECTED, status 8
    with pytest.raises(ConnectionError, match="gatt status"):
        await asyncio.wait_for(fut, 1)

    fut = radio._write_fut = radio._loop.create_future()
    radio._cb_write("AA:11", "fff0", 0x87)
    with pytest.raises(ConnectionError, match="rejected the frame"):
        await asyncio.wait_for(fut, 1)  # the car's ATT rejection is visible


def test_gatt_status_text_hedges_and_distinguishes():
    """The codes do not uniquely establish a cause (8 and 133 are catch-alls
    in the Android stack), so the texts must offer possible causes - never a
    diagnosis - while still distinguishing the codes from each other."""
    from qcar.radio_android import _gatt_status_text

    texts = [_gatt_status_text(s) for s in (8, 19, 62, 133)]
    assert len(set(texts)) == 4  # each code says something different
    for text in texts:
        assert "gatt status" in text  # the raw code stays visible for logs
        assert "may" in text or "possibly" in text  # hedged, not diagnosed
    assert "asleep" in _gatt_status_text(8)
    assert "Bluetooth" in _gatt_status_text(133)


async def test_notify_callback_receives_bytes_via_the_loop():
    radio = AndroidRadio()
    radio._loop = asyncio.get_running_loop()
    got: list[bytes] = []
    radio._notify_cb = got.append

    radio._cb_notify("AA:11", "fff1", b"\x01\x02\x03")
    radio._cb_notify("AA:11", "fff1", None)  # defensive: no payload
    await asyncio.sleep(0)
    assert got == [b"\x01\x02\x03"]


# ------------------------------------------------------------------ routing
# Every PyBleListener method leads with a session generation stamped into
# the per-session Java bridge object; the router delivers on that generation
# and strips it, so a scan cannot steal a live connection's acknowledgements
# and a previous connection to the same car address cannot poison the next
# one.


class _RecordingRadio:
    """Duck-typed AndroidRadio that records every callback it receives."""

    def __init__(self, name):
        self.name = name
        self.calls: list[tuple] = []

    def _cb_scan_hit(self, *args):
        self.calls.append(("_cb_scan_hit", args))

    def _cb_scan_failed(self, *args):
        self.calls.append(("_cb_scan_failed", args))

    def _cb_conn_state(self, *args):
        self.calls.append(("_cb_conn_state", args))

    def _cb_services(self, *args):
        self.calls.append(("_cb_services", args))

    def _cb_write(self, *args):
        self.calls.append(("_cb_write", args))

    def _cb_descriptor(self, *args):
        self.calls.append(("_cb_descriptor", args))

    def _cb_notify(self, *args):
        self.calls.append(("_cb_notify", args))


def test_router_delivers_scan_events_to_their_session():
    router = _CallbackRouter()
    scanning, other = _RecordingRadio("scan"), _RecordingRadio("other")
    gen = router.new_session(scanning)
    router.new_session(other)

    router._cb_scan_hit(gen, "AA", "QCAR-1", -40, 0x5452)
    router._cb_scan_failed(gen, 2)

    # the generation is stripped: handlers keep their plain signatures
    assert scanning.calls == [
        ("_cb_scan_hit", ("AA", "QCAR-1", -40, 0x5452)),
        ("_cb_scan_failed", (2,)),
    ]
    assert other.calls == []


def test_router_delivers_gatt_events_by_generation():
    router = _CallbackRouter()
    car_a, car_b = _RecordingRadio("a"), _RecordingRadio("b")
    gen_a, gen_b = router.new_session(car_a), router.new_session(car_b)

    router._cb_write(gen_a, "AA:11", "fff0", 0)
    router._cb_notify(gen_b, "BB:22", "fff1", b"\x01")
    router._cb_conn_state(gen_a, "AA:11", 0, 0)
    router._cb_services(gen_b, "BB:22", 0)
    router._cb_descriptor(gen_a, "AA:11", "2902", 0)

    assert car_a.calls == [
        ("_cb_write", ("AA:11", "fff0", 0)),
        ("_cb_conn_state", ("AA:11", 0, 0)),
        ("_cb_descriptor", ("AA:11", "2902", 0)),
    ]
    assert car_b.calls == [
        ("_cb_notify", ("BB:22", "fff1", b"\x01")),
        ("_cb_services", ("BB:22", 0)),
    ]


def test_router_scan_does_not_steal_a_connection_ack():
    """Regression: a scan starting next to an open connection used to make
    the scanning radio the global callback owner, so the connected radio's
    write acknowledgements were delivered to it (future unresolved until
    timeout) - exactly what returning to the garage does, since
    GarageScene.enter() scans while the car stays connected."""
    router = _CallbackRouter()
    connected, scanning = _RecordingRadio("conn"), _RecordingRadio("scan")
    conn_gen = router.new_session(connected)
    scan_gen = router.new_session(scanning)

    router._cb_write(scan_gen, "AA:11", "fff0", 0)  # scan-session traffic
    router._cb_write(conn_gen, "AA:11", "fff0", 0)  # the connection's ACK

    assert connected.calls == [("_cb_write", ("AA:11", "fff0", 0))]
    assert scanning.calls == [("_cb_write", ("AA:11", "fff0", 0))]


def test_router_drops_events_after_end_session():
    router = _CallbackRouter()
    radio = _RecordingRadio("a")
    gen = router.new_session(radio)
    router.end_session(gen)  # what disconnect()'s release does

    router._cb_write(gen, "AA:11", "fff0", 0)  # late ACK from the closing link
    assert radio.calls == []


def test_generations_are_not_reused():
    router = _CallbackRouter()
    first, second = _RecordingRadio("1"), _RecordingRadio("2")
    gen1 = router.new_session(first)
    router.end_session(gen1)
    gen2 = router.new_session(second)

    assert gen2 != gen1


def test_router_ignores_unknown_attribute_names():
    router = _CallbackRouter()
    with pytest.raises(AttributeError):
        router.__getattr__("something_else")  # explicit: B018/B009 want the dunder


# ------------------------------------------------------------------
# The same-address reconnect sequence: disconnect A -> reconnect A ->
# begin an operation -> deliver A's OLD callback.  Address-based routing
# cannot distinguish the sessions; the generation token must, and the new
# operation must stay pending with its state untouched.


async def test_late_callback_from_old_session_leaves_new_operation_pending():
    router = _CallbackRouter()

    first = AndroidRadio()  # session 1: connected to AA:11, then disconnected
    gen_old = router.new_session(first)
    router.end_session(gen_old)

    second = AndroidRadio()  # session 2: reconnect to the SAME address
    gen_new = router.new_session(second)
    second._loop = asyncio.get_running_loop()

    # an operation begins on the new connection: a write future is pending
    fut = second._write_fut = second._loop.create_future()

    # the OLD session's callbacks arrive late (same address, stale generation)
    router._cb_write(gen_old, "AA:11", "fff0", 0)  # its ACK, after the fact
    await asyncio.sleep(0)
    assert not fut.done(), "old session's ACK resolved the new session's write"
    assert second._link_up is False, "old session's event changed the link state"

    router._cb_conn_state(gen_old, "AA:11", 8, 0)  # its late state change
    await asyncio.sleep(0)
    assert not fut.done()
    assert second._link_up is False

    # the NEW session's own callback still gets through and resolves it
    router._cb_write(gen_new, "AA:11", "fff0", 0)
    await asyncio.wait_for(fut, 1)


async def test_late_state_change_from_old_session_cannot_flip_new_link():
    router = _CallbackRouter()

    first = AndroidRadio()
    gen_old = router.new_session(first)
    router.end_session(gen_old)

    second = AndroidRadio()
    gen_new = router.new_session(second)
    second._loop = asyncio.get_running_loop()

    # the new connection comes up
    router._cb_conn_state(gen_new, "AA:11", 0, 2)  # STATE_CONNECTED
    await asyncio.sleep(0)
    assert second._link_up is True

    # the old session's late disconnect must not tear the new link down
    router._cb_conn_state(gen_old, "AA:11", 0, 0)  # STATE_DISCONNECTED
    await asyncio.sleep(0)
    assert second._link_up is True


def test_java_listener_matches_interface_method_names():
    # every method the Java interface declares must have a matching Python
    # handler in radio_android.py - signature drift is the realistic failure
    listener = (BRIDGE_DIR / "PyBleListener.java").read_text()
    py = (REPO / "qcar" / "radio_android.py").read_text()
    methods = re.findall(r"void (on\w+)\(", listener)
    assert len(methods) >= 7
    for name in methods:
        assert re.search(rf"def {name}\(self", py), f"missing Python handler {name}"


def test_python_descriptors_match_the_java_interface():
    """The @java_method JNI descriptors must match the Java interface
    EXACTLY, derived independently from both sources: one-sided edits here
    are invisible until the on-device self-test, and a mismatch aborts the
    JVM (or silently mis-routes callbacks) in production. Builds the JNI
    notation from the Java parameter list and compares with the descriptor
    strings in radio_android.py.
    """
    listener = (BRIDGE_DIR / "PyBleListener.java").read_text()
    py = (REPO / "qcar" / "radio_android.py").read_text()

    JNI = {"long": "J", "int": "I", "String": "Ljava/lang/String;", "byte[]": "[B"}

    java_sigs = {}
    for name, params in re.findall(r"void (on\w+)\((.*?)\);", listener, re.S):
        types = [p.strip().split()[0] for p in params.split(",") if p.strip()]
        unknown = [t for t in types if t not in JNI]
        assert not unknown, f"{name}: add JNI mapping for {unknown}"
        java_sigs[name] = "(" + "".join(JNI[t] for t in types) + ")V"
    assert len(java_sigs) >= 7, "interface parse came up short - check the regex"

    py_sigs = {
        name: f"({params}){ret}" for params, ret, name in re.findall(
            r'@java_method\("\(([^)]+)\)(\w+)"\)\s*\n\s*def (on\w+)\(', py
        )
    }
    assert set(py_sigs) == set(java_sigs), (
        f"method sets differ: java-only={set(java_sigs) - set(py_sigs)} "
        f"python-only={set(py_sigs) - set(java_sigs)}"
    )
    for name, sig in java_sigs.items():
        assert py_sigs[name] == sig, f"{name}: python {py_sigs[name]!r} != java {sig!r}"
    # and every event leads with the session generation (except notify,
    # whose byte[] must be first - pyjnius corrupts arrays after primitives)
    for name, sig in java_sigs.items():
        if name == "onNotify":
            assert sig.startswith("([B"), sig
        else:
            assert sig.startswith("(J"), sig


# ------------------------------------------ failed attempts release everything
# Regression for the review finding: an attempt failing on a GATT status
# (not a timeout) used to leak its handle and session - two failed attempts
# left two registered sessions, zero closed handles, and a late callback
# could resolve a later attempt's future.  Same class of hazard for a write
# timeout: ACKs carry no operation id, so the connection cannot be reused.


class _FakeChar:
    def __init__(self):
        self.write_type = None
        self.value = None

    def setWriteType(self, t):
        self.write_type = t

    def setValue(self, v):
        self.value = bytes(v)


class _FakeService:
    def __init__(self):
        self.chars = {}

    def getCharacteristic(self, uuid):
        return self.chars.setdefault(uuid, _FakeChar())


class _FakeGatt:
    """Accepted, but never acknowledged - the timeout path."""

    def __init__(self):
        self.disconnect_calls = 0
        self.close_calls = 0
        self.service = _FakeService()

    def disconnect(self):
        self.disconnect_calls += 1

    def close(self):
        self.close_calls += 1

    def discoverServices(self):
        pass

    def getService(self, uuid):
        return self.service

    def writeCharacteristic(self, ch):
        return True


class _FakeDevice:
    def __init__(self, adapter):
        self.adapter = adapter

    def connectGatt(self, activity, autoconnect, cb, transport):
        gatt = _FakeGatt()
        self.adapter.gatts.append(gatt)
        return gatt


class _FakeAdapter:
    def __init__(self):
        self.gatts = []

    def getRemoteDevice(self, address):
        return _FakeDevice(self)


class _FakeGattCharacteristic:
    WRITE_TYPE_DEFAULT = 2
    WRITE_TYPE_NO_RESPONSE = 1


def _radio_under_test(monkeypatch):
    import types as _types

    from qcar import radio_android as ra

    radio = AndroidRadio()
    router = _CallbackRouter()
    adapter = _FakeAdapter()
    fake_shared = {"router": router, "listener": object()}
    fake_classes = {
        "PyGattCallback": lambda listener, gen: _types.SimpleNamespace(generation=gen),
        "GattCharacteristic": _FakeGattCharacteristic,
    }
    monkeypatch.setattr(ra, "_shared", fake_shared)
    monkeypatch.setattr(ra, "_shared_cbs", lambda: fake_shared)
    monkeypatch.setattr(ra, "_classes", lambda: fake_classes)
    monkeypatch.setattr(radio, "_bt_adapter", lambda: adapter)
    monkeypatch.setattr(radio, "_activity", lambda: object())
    monkeypatch.setattr(radio, "_uuid", lambda s: object())
    return radio, router, adapter


def _start(radio, address="AA:BB:CC:DD:EE:FF"):
    radio._loop = asyncio.get_running_loop()  # connect() normally does this
    return asyncio.create_task(radio._connect_once(address, 5.0))


async def _connect_ok(radio, address="AA:BB:CC:DD:EE:FF"):
    from qcar.radio_android import STATE_CONNECTED

    task = _start(radio, address)
    await asyncio.sleep(0.05)
    radio._cb_conn_state(address, 0, STATE_CONNECTED)
    radio._cb_services(address, 0)
    await task


async def test_failed_status_attempt_closes_handle_and_retires_session(monkeypatch):
    from qcar.radio_android import STATE_DISCONNECTED

    radio, router, adapter = _radio_under_test(monkeypatch)
    task = _start(radio)
    await asyncio.sleep(0.05)
    assert router._sessions, "the attempt owns a live session while connecting"
    assert adapter.gatts and adapter.gatts[0].close_calls == 0

    radio._cb_conn_state("AA:BB:CC:DD:EE:FF", 8, STATE_DISCONNECTED)
    with pytest.raises(ConnectionError):
        await task

    assert adapter.gatts[0].disconnect_calls >= 1, "handle must be released"
    assert adapter.gatts[0].close_calls == 1
    assert router._sessions == {}, "failed attempt must retire its generation"
    assert radio.connected is False


async def test_two_failed_attempts_leave_no_handles_or_sessions(monkeypatch):
    from qcar.radio_android import STATE_DISCONNECTED

    radio, router, adapter = _radio_under_test(monkeypatch)
    for _ in range(2):
        task = _start(radio)
        await asyncio.sleep(0.05)
        radio._cb_conn_state("AA:BB:CC:DD:EE:FF", 8, STATE_DISCONNECTED)
        with pytest.raises(ConnectionError):
            await task
    assert len(adapter.gatts) == 2
    assert [g.close_calls for g in adapter.gatts] == [1, 1]
    assert router._sessions == {}


async def test_cancellation_releases_the_handle_and_session(monkeypatch):
    radio, router, adapter = _radio_under_test(monkeypatch)
    task = _start(radio)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [g.close_calls for g in adapter.gatts] == [1]
    assert router._sessions == {}


async def test_write_timeout_retires_the_connection(monkeypatch):
    from qcar import radio_android as ra
    from qcar.radio import SERVICE_UUID

    radio, router, adapter = _radio_under_test(monkeypatch)
    await _connect_ok(radio)
    assert radio.connected

    monkeypatch.setattr(ra, "WRITE_TIMEOUT", 0.05)
    with pytest.raises(ConnectionError, match="retired"):
        await radio.write(SERVICE_UUID, b"\x01")

    gatt = adapter.gatts[0]
    assert gatt.close_calls == 1 and gatt.disconnect_calls >= 1
    assert router._sessions == {}
    assert radio.connected is False

    # the late ACK for the retired write is dropped...
    radio._cb_write("AA:BB:CC:DD:EE:FF", SERVICE_UUID, 0)
    # ...and the next write fails loudly instead of being falsely acknowledged
    with pytest.raises(ConnectionError):
        await radio.write(SERVICE_UUID, b"\x02")
