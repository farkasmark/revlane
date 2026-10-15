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
from qcar.radio_android import JAVA_CLASSES, AndroidRadio

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


def test_java_listener_matches_interface_method_names():
    # every method the Java interface declares must have a matching Python
    # handler in radio_android.py - signature drift is the realistic failure
    listener = (BRIDGE_DIR / "PyBleListener.java").read_text()
    py = (REPO / "qcar" / "radio_android.py").read_text()
    methods = re.findall(r"void (on\w+)\(", listener)
    assert len(methods) >= 7
    for name in methods:
        assert re.search(rf"def {name}\(self", py), f"missing Python handler {name}"
        assert f'"{name}"' not in py or f"def {name}(self" in py


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


async def test_notify_callback_receives_bytes_via_the_loop():
    radio = AndroidRadio()
    radio._loop = asyncio.get_running_loop()
    got: list[bytes] = []
    radio._notify_cb = got.append

    radio._cb_notify("AA:11", "fff1", b"\x01\x02\x03")
    radio._cb_notify("AA:11", "fff1", None)  # defensive: no payload
    await asyncio.sleep(0)
    assert got == [b"\x01\x02\x03"]
