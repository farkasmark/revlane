"""Android Bluetooth LE transport - pyjnius calling down into Android's
``android.bluetooth`` API. Runs only inside the APK (python-for-android).

 bleak   : bleak -> D-Bus -> BlueZ -> hci0        (desktop)
 android : this file -> JNI -> BluetoothGatt -> controller
 mock    : qcar/radio_mock.py                      (emulator, CI)

Design notes
------------
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
  characteristic).  If you bump targetSdk to 33+, swap to the byte[] overload
  here.
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

# BluetoothProfile.STATE_* / BluetoothProfile constants
STATE_DISCONNECTED = 0
STATE_CONNECTED = 2
TRANSPORT_LE = 2


class AndroidRadio(Radio):
    def __init__(self, *, adapter: str | None = None) -> None:
        # 'adapter' is a BlueZ hci address on the desktop; Android picks its
        # own controller, so it is ignored here.
        self.adapter = None
        self._gatt = None
        self._link_up = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scan_cb = None
        self._scan_on_device: Callable[[Discovered], None] | None = None
        self._scan_hits: dict[str, Discovered] = {}
        self._gatt_cb = None
        self._connect_fut: asyncio.Future | None = None
        self._write_fut: asyncio.Future | None = None
        self._desc_fut: asyncio.Future | None = None
        self._write_lock = asyncio.Lock()
        self._notify_cb: Callable[[bytes], None] | None = None
        self._chars: dict[str, object] = {}

    # ------------------------------------------------------------------
    # platform helpers

    def _classes(self):
        """Resolve pyjnius + Java classes (cached in radio._jni_classes)."""
        global _jni_classes
        if _jni_classes is None:
            from jnius import autoclass

            _jni_classes = {
                "PythonActivity": autoclass("org.kivy.android.PythonActivity"),
                "BluetoothAdapter": autoclass("android.bluetooth.BluetoothAdapter"),
                "BluetoothManager": autoclass("android.bluetooth.BluetoothManager"),
                "Context": autoclass("android.content.Context"),
                "UUID": autoclass("java.util.UUID"),
                "ScanSettings": autoclass("android.bluetooth.ScanSettings"),
                "ScanSettingsBuilder": autoclass(
                    "android.bluetooth.le.ScanSettings$Builder"
                ),
                "GattCharacteristic": autoclass(
                    "android.bluetooth.BluetoothGattCharacteristic"
                ),
                "GattDescriptor": autoclass(
                    "android.bluetooth.BluetoothGattDescriptor"
                ),
                "ScanCallback": _make_scan_callback(),
                "GattCallback": _make_gatt_callback(),
            }
        return _jni_classes

    def _activity(self):
        return self._classes()["PythonActivity"].mActivity

    def _bt_adapter(self):
        j = self._classes()
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
        return self._classes()["UUID"].fromString(s)

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
        j = self._classes()
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

        self._scan_cb = j["ScanCallback"](self)
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
            self._scan_cb = None
        return sorted(self._scan_hits.values(), key=lambda d: -d.rssi)

    # scan callbacks (binder threads)

    def _cb_scan_result(self, r) -> None:
        with contextlib.suppress(Exception):
            self._handle_scan_result(r)

    def _handle_scan_result(self, r) -> None:
        dev = r.getDevice()
        address = dev.getAddress()
        record = r.getScanRecord()
        name = dev.getName()
        if not name and record is not None:
            name = record.getDeviceName()
        mfg = self._manufacturer_data(record)
        if not looks_like_qcar(name, mfg):
            return
        hit = Discovered(address=address, name=name, rssi=r.getRssi() or 0)
        first = address not in self._scan_hits
        self._scan_hits[address] = hit
        emit = self._scan_on_device
        if emit is not None and first:
            self._on_loop(lambda: emit(hit))

    @staticmethod
    def _manufacturer_data(record):
        """ScanRecord.getManufacturerSpecificData() -> {int: bytes} | None."""
        if record is None:
            return None
        try:
            sa = record.getManufacturerSpecificData()
            if sa is None:
                return None
            out = {}
            for i in range(sa.size()):
                out[int(sa.keyAt(i))] = bytes(sa.valueAt(i))
            return out
        except Exception:
            return None

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

        j = self._classes()
        self._gatt_cb = j["GattCallback"](self)
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
        if gatt is not None:
            with contextlib.suppress(Exception):
                gatt.disconnect()
            with contextlib.suppress(Exception):
                gatt.close()

    # GATT callbacks (binder threads)

    def _cb_conn_state(self, gatt, status: int, new_state: int) -> None:
        if new_state == STATE_CONNECTED:
            self._link_up = True
            with contextlib.suppress(Exception):
                gatt.discoverServices()
        else:
            self._link_up = False
            fut, self._connect_fut = self._connect_fut, None
            self._resolve(
                fut, ConnectionError(f"connect failed, gatt status {status}")
            )

    def _cb_services(self, gatt, status: int) -> None:
        fut, self._connect_fut = self._connect_fut, None
        self._resolve(
            fut,
            None if status == 0 else ConnectionError(
                f"service discovery failed, status {status}"
            ),
        )

    def _cb_write(self, gatt, characteristic, status: int) -> None:
        fut, self._write_fut = self._write_fut, None
        self._resolve(
            fut,
            None if status == 0 else ConnectionError(
                # the car's ATT server NAKs when busy: visible now, not lost
                f"car rejected the frame (gatt status {status:#x})"
            ),
        )

    def _cb_descriptor(self, gatt, descriptor, status: int) -> None:
        fut, self._desc_fut = self._desc_fut, None
        self._resolve(
            fut,
            None if status == 0 else ConnectionError(
                f"subscribe failed, status {status}"
            ),
        )

    def _cb_notify(self, gatt, characteristic) -> None:
        cb = self._notify_cb
        if cb is None:
            return
        try:
            value = bytes(characteristic.getValue())
        except Exception:
            return
        self._on_loop(lambda: cb(value))

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
        j = self._classes()
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
        j = self._classes()
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


# ----------------------------------------------------------------------
# pyjnius callback classes. They need pyjnius at class-creation time (the
# decorator inspects the JVM), so they are factories, built once on Android.


_jni_classes = None


def _make_scan_callback():
    from jnius import PythonJavaClass, java_method

    class _ScanCallback(PythonJavaClass):
        __javainterfaces__ = ["android/bluetooth/le/ScanCallback"]
        __javacontext__ = "app"

        def __init__(self, radio: AndroidRadio) -> None:
            super().__init__()
            self._radio = radio

        # one call per advertisement (report delay 0)
        @java_method("(ILandroid/bluetooth/le/ScanResult;)V")
        def onScanResult(self, callback_type, result):
            self._radio._cb_scan_result(result)

        @java_method("(I)V")
        def onScanFailed(self, error_code):
            self._radio._cb_scan_failed(error_code)

    return _ScanCallback


def _make_gatt_callback():
    from jnius import PythonJavaClass, java_method

    class _GattCallback(PythonJavaClass):
        __javainterfaces__ = ["android/bluetooth/BluetoothGattCallback"]
        __javacontext__ = "app"

        def __init__(self, radio: AndroidRadio) -> None:
            super().__init__()
            self._radio = radio

        @java_method("(Landroid/bluetooth/BluetoothGatt;II)V")
        def onConnectionStateChange(self, gatt, status, new_state):
            self._radio._cb_conn_state(gatt, status, new_state)

        @java_method("(Landroid/bluetooth/BluetoothGatt;I)V")
        def onServicesDiscovered(self, gatt, status):
            self._radio._cb_services(gatt, status)

        @java_method(
            "(Landroid/bluetooth/BluetoothGatt;"
            "Landroid/bluetooth/BluetoothGattCharacteristic;I)V"
        )
        def onCharacteristicWrite(self, gatt, characteristic, status):
            self._radio._cb_write(gatt, characteristic, status)

        # legacy signature - see the module docstring re targetSdk 31
        @java_method(
            "(Landroid/bluetooth/BluetoothGatt;"
            "Landroid/bluetooth/BluetoothGattCharacteristic;)V"
        )
        def onCharacteristicChanged(self, gatt, characteristic):
            self._radio._cb_notify(gatt, characteristic)

        @java_method(
            "(Landroid/bluetooth/BluetoothGatt;"
            "Landroid/bluetooth/BluetoothGattDescriptor;I)V"
        )
        def onDescriptorWrite(self, gatt, descriptor, status):
            self._radio._cb_descriptor(gatt, descriptor, status)

    return _GattCallback
