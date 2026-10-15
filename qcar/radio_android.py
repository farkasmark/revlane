"""Android Bluetooth LE transport - pyjnius calling down into Android's
``android.bluetooth`` API. Runs only inside the APK (python-for-android).

 bleak   : bleak -> D-Bus -> BlueZ -> hci0        (desktop)
 android : this file -> JNI -> BluetoothGatt -> controller
 mock    : qcar/radio_mock.py                      (emulator, CI)

Design notes
------------
* The framework's ``ScanCallback`` and ``BluetoothGattCallback`` are abstract
  classes, and pyjnius can only *implement Java interfaces* - so the Java
  side of the bridge lives in ``android-src/org/artalis/revlane/ble/`` (added
  to the APK via buildozer's ``android.add_src``): ``PyScanCallback`` and
  ``PyGattCallback`` extend the framework callbacks and forward every event
  to a ``PyBleListener``, which the Python side implements with a
  ``PythonJavaClass``. The Java bridge flattens ScanResults into plain types
  on the Java side. Both the Python listener and the Java callback objects
  are retained on the radio for as long as Android may call into them.
* Android GATT callbacks arrive on **binder threads**; they are bridged onto
  the caller's asyncio loop with ``call_soon_threadsafe`` and per-operation
  futures, so the rest of the code stays plain async.
* GATT allows exactly one outstanding write.  ``write()`` serialises on an
  ``asyncio.Lock`` and awaits the ``onCharacteristicWrite`` callback, which
  also surfaces the car's ATT rejections (status != 0) instead of dropping
  them.
* Writes use ``WRITE_TYPE_DEFAULT`` = an ACKed Write *Request*.  The car
  silently discards unacknowledged writes (see car.py) - this is load-bearing.
* We implement the *legacy* ``onCharacteristicChanged(gatt, characteristic)``
  callback and target SDK 31: Android's compat shim keeps calling the legacy
  signature for apps targeting < 33 (it replays the value into the
  characteristic).  If you bump targetSdk to 33+, add the byte[] overloads to
  the Java bridge.
* Runtime permissions (BLUETOOTH_CONNECT/SCAN on 31+, ACCESS_FINE_LOCATION on
  older) are requested by main.py at startup; without them the scanner and
  connectGatt throw SecurityException, surfaced here as ConnectionError.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from .radio import (
    CCCD_UUID,
    SERVICE_UUID,
    Discovered,
    Radio,
    looks_like_qcar,
)

log = logging.getLogger("qcar")

# BluetoothProfile.STATE_* / transport constants
STATE_DISCONNECTED = 0
STATE_CONNECTED = 2
TRANSPORT_LE = 2

#: Fully qualified names of every Java class the radio resolves at runtime.
#: The bridge classes live in android-src/ (compiled into the APK by gradle);
#: the rest are framework classes - note ScanSettings lives in *.le*.
JAVA_CLASSES = {
    "PyBleListener": "org.artalis.revlane.ble.PyBleListener",
    "PyScanCallback": "org.artalis.revlane.ble.PyScanCallback",
    "PyGattCallback": "org.artalis.revlane.ble.PyGattCallback",
    "PythonActivity": "org.kivy.android.PythonActivity",
    "BluetoothAdapter": "android.bluetooth.BluetoothAdapter",
    "BluetoothManager": "android.bluetooth.BluetoothManager",
    "Context": "android.content.Context",
    "UUID": "java.util.UUID",
    "ScanSettings": "android.bluetooth.le.ScanSettings",
    "ScanSettingsBuilder": "android.bluetooth.le.ScanSettings$Builder",
    "GattCharacteristic": "android.bluetooth.BluetoothGattCharacteristic",
    "GattDescriptor": "android.bluetooth.BluetoothGattDescriptor",
}

# pyjnius machinery - imported lazily so this module stays importable
# (and testable) off-device.
_jni_classes = None
_pyble_listener_cls = None


def _classes():
    """Resolve pyjnius + the Java classes we need (once, on device)."""
    global _jni_classes, _pyble_listener_cls
    if _jni_classes is None:
        from jnius import autoclass

        _jni_classes = {name: autoclass(fqn) for name, fqn in JAVA_CLASSES.items()}
    if _pyble_listener_cls is None:
        from jnius import PythonJavaClass, java_method

        class PyBleListener(PythonJavaClass):
            """Implements the Java ``PyBleListener`` interface.

            Java (interface -> OK for pyjnius) calls into Python from binder
            threads; every method just forwards to the radio's ``_cb_*``
            handlers, which do the loop marshalling.
            """

            __javainterfaces__ = ["org/artalis/revlane/ble/PyBleListener"]
            __javacontext__ = "app"

            def __init__(self, radio: AndroidRadio) -> None:
                super().__init__()
                self._radio = radio

            @java_method("(Ljava/lang/String;Ljava/lang/String;II)V")
            def onScanHit(self, address, name, rssi, mfg_id):
                self._radio._cb_scan_hit(address, name, rssi, mfg_id)

            @java_method("(I)V")
            def onScanFailed(self, error_code):
                self._radio._cb_scan_failed(error_code)

            @java_method("(Ljava/lang/String;II)V")
            def onGattState(self, address, status, new_state):
                self._radio._cb_conn_state(address, status, new_state)

            @java_method("(Ljava/lang/String;I)V")
            def onServicesDiscovered(self, address, status):
                self._radio._cb_services(address, status)

            @java_method("(Ljava/lang/String;Ljava/lang/String;I)V")
            def onCharacteristicWrite(self, address, char_uuid, status):
                self._radio._cb_write(address, char_uuid, status)

            @java_method("(Ljava/lang/String;Ljava/lang/String;I)V")
            def onDescriptorWrite(self, address, char_uuid, status):
                self._radio._cb_descriptor(address, char_uuid, status)

            @java_method("(Ljava/lang/String;Ljava/lang/String;[B)V")
            def onNotify(self, address, char_uuid, value):
                self._radio._cb_notify(address, char_uuid, value)

        _pyble_listener_cls = PyBleListener
        _jni_classes["PyBleListenerImpl"] = _pyble_listener_cls
    return _jni_classes


class AndroidRadio(Radio):
    def __init__(self, *, adapter: str | None = None) -> None:
        # 'adapter' is a BlueZ hci address on the desktop; Android picks its
        # own controller, so it is ignored here.
        self.adapter = None
        self._gatt = None
        self._link_up = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # Retained for the required lifetime: Android holds raw pointers into
        # the pyjnius objects, so they must outlive every possible callback.
        self._scan_listener = None
        self._scan_cb = None
        self._gatt_listener = None
        self._gatt_cb = None
        self._scan_on_device: Callable[[Discovered], None] | None = None
        self._scan_hits: dict[str, Discovered] = {}
        self._connect_fut: asyncio.Future | None = None
        self._write_fut: asyncio.Future | None = None
        self._desc_fut: asyncio.Future | None = None
        self._write_lock = asyncio.Lock()
        self._notify_cb: Callable[[bytes], None] | None = None
        self._chars: dict[str, object] = {}

    # ------------------------------------------------------------------
    # platform helpers

    def _activity(self):
        return _classes()["PythonActivity"].mActivity

    def _bt_adapter(self):
        j = _classes()
        activity = self._activity()
        if activity is None:
            raise ConnectionError("no Android activity - are we really in the APK?")
        manager = activity.getSystemService(j["Context"].BLUETOOTH_SERVICE)
        ad = (
            manager.getAdapter()
            if manager
            else j["BluetoothAdapter"].getDefaultAdapter()
        )
        if ad is None:
            raise ConnectionError("this device has no Bluetooth adapter")
        if not ad.isEnabled():
            raise ConnectionError("Bluetooth is turned off - enable it and retry")
        return ad

    def _uuid(self, s: str):
        return _classes()["UUID"].fromString(s)

    def _on_loop(self, fn) -> None:
        """Run fn on the asyncio loop thread (we may be on a binder thread)."""
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(fn)

    def _resolve(self, fut: asyncio.Future | None, exc: Exception | None = None) -> None:
        if fut is None or fut.done():
            return

        def _do():
            if fut.done():
                return
            if exc is not None:
                fut.set_exception(exc)
            else:
                fut.set_result(None)

        self._on_loop(_do)

    # ------------------------------------------------------------------
    # scanning

    async def scan(
        self,
        timeout: float = 8.0,
        on_device: Callable[[Discovered], None] | None = None,
    ) -> list[Discovered]:
        j = _classes()
        self._loop = asyncio.get_running_loop()
        self._scan_hits = {}
        self._scan_on_device = on_device

        ad = self._bt_adapter()
        scanner = ad.getBluetoothLeScanner()
        if scanner is None:
            raise ConnectionError(
                "BLE scanner unavailable - is Bluetooth enabled and is the app "
                "granted BLUETOOTH_SCAN / ACCESS_FINE_LOCATION?"
            )

        # Both objects stay referenced (see the lifetime note in __init__):
        # the Java callback is registered with the stack, the Python listener
        # is what it calls back into.
        self._scan_listener = j["PyBleListenerImpl"](self)
        self._scan_cb = j["PyScanCallback"](self._scan_listener)
        # no system ScanFilter: the QCAR name/mfg rules live in one place
        # (radio.looks_like_qcar), and filtering in Python keeps the Java
        # surface minimal. LOW_LATENCY because the scan window is short.
        settings = (
            j["ScanSettingsBuilder"]()
            .setScanMode(j["ScanSettings"].SCAN_MODE_LOW_LATENCY)
            .setReportDelay(0)
            .build()
        )
        try:
            scanner.startScan([], settings, self._scan_cb)
        except Exception as exc:  # SecurityException when permission missing
            raise ConnectionError(f"could not start BLE scan: {exc}") from exc

        try:
            await asyncio.sleep(timeout)
        finally:
            with contextlib.suppress(Exception):
                scanner.stopScan(self._scan_cb)
            # keep _scan_cb/_scan_listener referenced until the next scan
            # replaces them: late deliveries must not hit a dead proxy
        return sorted(self._scan_hits.values(), key=lambda d: -d.rssi)

    # scan callbacks (binder threads, plain Python types from the bridge;
    # the Java side passes only the manufacturer ID - see PyBleListener.java
    # for why the data bytes never cross the boundary)

    def _cb_scan_hit(self, address, name, rssi, mfg_id) -> None:
        with contextlib.suppress(Exception):
            self._handle_scan_hit(address, name, rssi, mfg_id)

    def _handle_scan_hit(self, address, name, rssi, mfg_id) -> None:
        if not address:
            return
        mfg = {mfg_id: b""} if (mfg_id is not None and mfg_id >= 0) else None
        if not looks_like_qcar(name, mfg):
            return
        hit = Discovered(address=address, name=name, rssi=int(rssi or 0))
        first = address not in self._scan_hits
        self._scan_hits[address] = hit
        emit = self._scan_on_device
        if emit is not None and first:
            self._on_loop(lambda: emit(hit))

    def _cb_scan_failed(self, error_code: int) -> None:
        log.warning("android: BLE scan failed, error %s", error_code)

    # ------------------------------------------------------------------
    # connection

    async def connect(self, address: str, timeout: float = 20.0) -> None:
        self._loop = asyncio.get_running_loop()
        ad = self._bt_adapter()
        dev = ad.getRemoteDevice(address)
        if dev is None:
            raise ConnectionError(f"{address}: device not found")

        j = _classes()
        self._gatt_listener = j["PyBleListenerImpl"](self)
        self._gatt_cb = j["PyGattCallback"](self._gatt_listener)
        self._connect_fut = self._loop.create_future()
        # TRANSPORT_LE: the cars are BLE-only; without this some dual-mode
        # phones try BR/EDR first and the connect just hangs.
        try:
            self._gatt = dev.connectGatt(
                self._activity(), False, self._gatt_cb, TRANSPORT_LE
            )
        except Exception as exc:  # SecurityException without BLUETOOTH_CONNECT
            raise ConnectionError(
                f"could not connect to {address}: {exc} - is the app granted "
                "BLUETOOTH_CONNECT?"
            ) from exc
        try:
            await asyncio.wait_for(self._connect_fut, timeout)
        except asyncio.TimeoutError:
            await self.disconnect()
            raise ConnectionError(
                f"{address} did not answer. Is the car switched on? If another "
                "program still holds the link, power-cycle the car."
            ) from None
        log.info("android: connected to %s", address)

    @property
    def connected(self) -> bool:
        return self._link_up and self._gatt is not None

    async def disconnect(self) -> None:
        gatt, self._gatt = self._gatt, None
        self._link_up = False
        self._notify_cb = None
        self._chars.clear()
        # _gatt_cb/_gatt_listener stay referenced: Android may still invoke
        # the callback during/after close, and the next connect replaces them
        if gatt is not None:
            with contextlib.suppress(Exception):
                gatt.disconnect()
            with contextlib.suppress(Exception):
                gatt.close()

    # GATT callbacks (binder threads, plain Python types from the bridge)

    def _cb_conn_state(self, address, status: int, new_state: int) -> None:
        if new_state == STATE_CONNECTED:
            self._link_up = True
            with contextlib.suppress(Exception):
                self._gatt.discoverServices()
        else:
            self._link_up = False
            fut, self._connect_fut = self._connect_fut, None
            self._resolve(
                fut, ConnectionError(f"connect failed, gatt status {status}")
            )

    def _cb_services(self, address, status: int) -> None:
        fut, self._connect_fut = self._connect_fut, None
        self._resolve(
            fut,
            None if status == 0 else ConnectionError(
                f"service discovery failed, status {status}"
            ),
        )

    def _cb_write(self, address, char_uuid, status: int) -> None:
        fut, self._write_fut = self._write_fut, None
        self._resolve(
            fut,
            None if status == 0 else ConnectionError(
                # the car's ATT server NAKs when busy: visible now, not lost
                f"car rejected the frame (gatt status {status:#x})"
            ),
        )

    def _cb_descriptor(self, address, char_uuid, status: int) -> None:
        fut, self._desc_fut = self._desc_fut, None
        self._resolve(
            fut,
            None if status == 0 else ConnectionError(
                f"subscribe failed, status {status}"
            ),
        )

    def _cb_notify(self, address, char_uuid, value) -> None:
        cb = self._notify_cb
        if cb is None:
            return
        try:
            payload = bytes(value)
        except Exception:
            return
        self._on_loop(lambda: cb(payload))

    # ------------------------------------------------------------------
    # traffic

    def _characteristic(self, char_uuid: str):
        ch = self._chars.get(char_uuid)
        if ch is not None:
            return ch
        if self._gatt is None:
            raise ConnectionError("not connected")
        svc = self._gatt.getService(self._uuid(SERVICE_UUID))
        ch = svc.getCharacteristic(self._uuid(char_uuid)) if svc else None
        if ch is None:
            raise ValueError(f"characteristic {char_uuid} not found")
        self._chars[char_uuid] = ch
        return ch

    async def start_notify(
        self, char_uuid: str, cb: Callable[[bytes], None]
    ) -> None:
        j = _classes()
        try:
            ch = self._characteristic(char_uuid)
        except ValueError:
            return  # mirror the desktop backend: telemetry is optional
        self._notify_cb = cb
        self._gatt.setCharacteristicNotification(ch, True)
        desc = ch.getDescriptor(self._uuid(CCCD_UUID))
        if desc is None:
            return
        desc.setValue(j["GattDescriptor"].ENABLE_NOTIFICATION_VALUE)
        self._desc_fut = self._loop.create_future()
        if not self._gatt.writeDescriptor(desc):
            self._desc_fut = None
            return
        with contextlib.suppress(asyncio.TimeoutError, ConnectionError):
            await asyncio.wait_for(self._desc_fut, 5)

    async def write(
        self, char_uuid: str, data: bytes, *, response: bool = True
    ) -> None:
        j = _classes()
        async with self._write_lock:  # one outstanding GATT operation
            ch = self._characteristic(char_uuid)
            ch.setWriteType(
                j["GattCharacteristic"].WRITE_TYPE_DEFAULT
                if response
                else j["GattCharacteristic"].WRITE_TYPE_NO_RESPONSE
            )
            ch.setValue(bytes(data))
            self._write_fut = self._loop.create_future()
            if not self._gatt.writeCharacteristic(ch):
                self._write_fut = None
                raise ConnectionError("GATT write rejected (operation in flight?)")
            await asyncio.wait_for(self._write_fut, 10)
