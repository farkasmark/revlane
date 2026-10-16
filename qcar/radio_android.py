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
  on the Java side. The listener proxy is built once by ``prime()`` on the
  main thread (proxy creation is main-thread-only) and retained for the
  process lifetime.  Each scan/connection then creates its own plain Java
  callback object - a normal constructor call, fine on any thread - stamped
  with a session *generation*: a token allocated by the router that every
  event leads with. ``_CallbackRouter`` delivers each event to the session
  that owns its generation; events from an ended session (late ACKs from a
  closed link, or a previous connection to the same car address) are lookup
  misses and dropped.
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


def _gatt_status_text(status: int) -> str:
    """Human text for the GATT status codes we actually see in the field.

    Android surfaces these raw (e.g. status=8).  The codes do NOT uniquely
    identify a cause - 8 and 133 in particular are catch-alls - so the text
    names the layer that failed and offers possible causes, never a
    diagnosis.  The raw code stays in the message for the log.
    """
    text = {
        8: "no answer from the car - it may be asleep or out of range",
        19: "the car dropped the link - possibly its battery or sleep timer",
        62: "the phone's Bluetooth stack may have failed to establish the link",
        133: "the phone's Bluetooth stack failed - toggling Bluetooth may help",
    }.get(status, "bluetooth error")
    return f"{text} (gatt status {status})"

class _CallbackRouter:
    """Delivers ``PyBleListener`` events to the radio that owns them.

    Every ``PyBleListener`` method leads with a **generation** - a session
    token allocated here and stamped into the Java bridge object at creation
    (``PyScanCallback(listener, gen)`` / ``PyGattCallback(listener, gen)``;
    plain Java constructors, so the worker thread may create them).  A
    session owns its generation from ``new_session()`` until
    ``end_session()``; delivery is a dict lookup on the generation.

    This is what makes sequential sessions to the *same car address* safe:
    the address cannot tell two connections apart, but generations can.  A
    late ACK from a closed connection carries the old generation - a lookup
    miss after ``end_session()`` - so it can neither resolve the next
    connection's pending write future nor change its link state.  (A
    ``fut.done()`` guard on the handler side cannot do this: it protects a
    *completed* future, and the new session's future is pending.)  The
    backend serialises *operations* on its worker loop, but Android callback
    lifetimes outlive the operations that started them.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, AndroidRadio] = {}
        self._next_generation = 1

    def new_session(self, radio: AndroidRadio) -> int:
        """Allocate a generation and make ``radio`` own it (call before
        startScan/connectGatt, so no callback can race the registration)."""
        generation = self._next_generation
        self._next_generation += 1
        self._sessions[generation] = radio
        return generation

    def end_session(self, generation: int) -> None:
        """Release ownership: later events for this session are dropped."""
        self._sessions.pop(generation, None)

    def __getattr__(self, name: str):
        if not name.startswith("_cb_"):
            raise AttributeError(name)

        def forward(*args) -> None:
            # every callback leads with its session's generation; the owning
            # radio gets the event without it (its handlers never see it)
            radio = self._sessions.get(args[0])
            if radio is not None:
                getattr(radio, name)(*args[1:])

        return forward


#: shared callback objects built by ``prime()`` on the main thread
#: (the listener proxy and the router; per-session bridge objects are
#: created by scan()/connect())
_shared: dict | None = None


def _shared_cbs() -> dict:
    """The prime()-built shared objects (proxy creation is main-thread only)."""
    if _shared is None:
        raise ConnectionError(
            "BLE bridge not primed - select QCAR_RADIO=android in main.py"
        )
    return _shared


def prime() -> None:
    """Resolve every Java class up front, on the main (Java-attached) thread.

    pyjnius resolves classes through JNI ``FindClass``, whose classloader
    context comes from how the *current thread* is attached to the JVM: the
    bootstrap/UI thread sees the APK's PathClassLoader, but a plain Python
    thread attaches bare and only sees the system loader
    (``DexPathList[[directory "."]]) - where our ``android-src`` bridge
    classes do not exist, so every lookup there raises
    ``ClassNotFoundException``.  ``_classes()`` caches its result module-wide,
    so calling this once from ``main.py`` makes all later worker-thread use
    (backend scans, GATT) safe.

    Class *resolution* is only half of it: creating a ``PythonJavaClass``
    proxy instance also needs the main thread (``find_javaclass`` for the
    interfaces plus the thread's context classloader for
    ``Proxy.newProxyInstance``).  So ``prime()`` builds the listener proxy
    here, once, bound to the ``_CallbackRouter``.  The per-session bridge
    objects (``PyScanCallback``/``PyGattCallback``) are *plain* Java objects
    - normal constructors on a cached class, no proxy machinery - so
    scan()/connect() may create those on the worker thread, one per session,
    each stamped with its generation.
    """
    global _shared
    _classes()
    if _shared is None:
        from jnius import autoclass

        # warm pyjnius's own lazily-resolved handler class (used by every
        # proxy creation) - same thread-context rule as above
        autoclass("org.jnius.NativeInvocationHandler")
        router = _CallbackRouter()
        listener = _classes()["PyBleListenerImpl"](router)
        _shared = {"router": router, "listener": listener}
        # the listener is retained for the process lifetime: Android holds a
        # raw pointer into it and may call it at any time
        log.info("android: BLE bridge primed (%s classes)", len(_jni_classes))

# BluetoothProfile.STATE_* / transport constants
STATE_DISCONNECTED = 0
STATE_CONNECTED = 2
TRANSPORT_LE = 2
WRITE_TIMEOUT = 10.0

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
    "ArrayList": "java.util.ArrayList",
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

            # every method leads with the session generation (a Java long);
            # it is passed through to the router, which routes on it and
            # strips it - the radio handlers never see it

            @java_method("(JLjava/lang/String;Ljava/lang/String;II)V")
            def onScanHit(self, generation, address, name, rssi, mfg_id):
                self._radio._cb_scan_hit(generation, address, name, rssi, mfg_id)

            @java_method("(JI)V")
            def onScanFailed(self, generation, error_code):
                self._radio._cb_scan_failed(generation, error_code)

            @java_method("(JLjava/lang/String;II)V")
            def onGattState(self, generation, address, status, new_state):
                self._radio._cb_conn_state(generation, address, status, new_state)

            @java_method("(JLjava/lang/String;I)V")
            def onServicesDiscovered(self, generation, address, status):
                self._radio._cb_services(generation, address, status)

            @java_method("(JLjava/lang/String;Ljava/lang/String;I)V")
            def onCharacteristicWrite(self, generation, address, char_uuid, status):
                self._radio._cb_write(generation, address, char_uuid, status)

            @java_method("(JLjava/lang/String;Ljava/lang/String;I)V")
            def onDescriptorWrite(self, generation, address, char_uuid, status):
                self._radio._cb_descriptor(generation, address, char_uuid, status)

            # the byte[] comes first: pyjnius corrupts ByteArray wrappers
            # when primitives precede the array (bridge self-test finding)
            @java_method("([BJLjava/lang/String;Ljava/lang/String;)V")
            def onNotify(self, value, generation, address, char_uuid):
                self._radio._cb_notify(generation, address, char_uuid, value)

        _pyble_listener_cls = PyBleListener
        _jni_classes["PyBleListenerImpl"] = _pyble_listener_cls
    return _jni_classes


class AndroidRadio(Radio):
    def __init__(self, *, adapter: str | None = None) -> None:
        # 'adapter' is a BlueZ hci address on the desktop; Android picks its
        # own controller, so it is ignored here.
        self.adapter = None
        self.address: str | None = None  # set by connect(); informational
        self._generation: int | None = None  # set by scan()/connect()
        self._scan_cb = None  # retained: Android may call it after stopScan
        self._gatt_cb = None  # retained: Android may call it during close
        self._gatt = None
        self._link_up = False
        self._loop: asyncio.AbstractEventLoop | None = None
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
        until_seen: str | None = None,
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

        # Own a session for the duration of the scan only: events carry its
        # generation, and a scan running next to an open connection cannot
        # steal that connection's acknowledgements (see _CallbackRouter).
        # The bridge object is per scan, tagged with the session generation,
        # so a late hit from a finished scan is dropped instead of landing in
        # the next scan's results.
        cbs = _shared_cbs()
        router = cbs["router"]
        generation = router.new_session(self)
        self._generation = generation
        scan_cb = _classes()["PyScanCallback"](cbs["listener"], generation)
        self._scan_cb = scan_cb  # retained until the next scan replaces it
        # no system ScanFilter: the QCAR name/mfg rules live in one place
        # (radio.looks_like_qcar), and filtering in Python keeps the Java
        # surface minimal. LOW_LATENCY because the scan window is short.
        settings = (
            j["ScanSettingsBuilder"]()
            .setScanMode(j["ScanSettings"].SCAN_MODE_LOW_LATENCY)
            .setReportDelay(0)
            .build()
        )
        # pyjnius passes Python lists as arrays, and startScan's overloads
        # take java.util.List - so the (empty) filter list must be a real one
        filters = j["ArrayList"]()
        try:
            scanner.startScan(filters, settings, scan_cb)
        except Exception as exc:  # SecurityException when permission missing
            router.end_session(generation)
            raise ConnectionError(f"could not start BLE scan: {exc}") from exc

        try:
            deadline = self._loop.time() + timeout
            while self._loop.time() < deadline:
                # until_seen: connect()'s presence check polls for one address
                # and stops the moment it appears - a healthy car is usually
                # seen in well under a second, so the pre-connect cost is
                # near zero
                if until_seen is not None and until_seen in self._scan_hits:
                    break
                await asyncio.sleep(0.1)
        finally:
            with contextlib.suppress(Exception):
                scanner.stopScan(scan_cb)
            router.end_session(generation)
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

    async def connect(
        self, address: str, timeout: float = 12.0, retries: int = 2
    ) -> None:
        """Connect, with a presence pre-check and automatic retries.

        Field behaviour this absorbs: a car that is asleep or out of range
        makes connectGatt burn its whole timeout (the stack answers with
        gatt status 8), and a phone stack that has failed a few connects
        wedges on that address until Bluetooth is toggled.  Seeing the car
        advertise first turns "20 s blind hang, generic error" into "2 s -
        the car is not advertising, wake it", and the retries absorb the
        transient status 8/133 failures that used to send users hunting for
        workarounds (deleting profiles, toggling Bluetooth).
        """
        self._loop = asyncio.get_running_loop()
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            # see the car advertise first: a silent car means asleep or out
            # of range, and failing fast with that beats a blind hang (the
            # scan itself also gives the stack a beat to settle)
            hits = await self.scan(
                timeout=2.5 if attempt == 0 else 1.5, until_seen=address
            )
            if address not in {h.address for h in hits}:
                raise ConnectionError(
                    f"{address}: the car is not advertising right now. It may "
                    "be asleep (it sleeps when idle - power-cycle it), out of "
                    "range, or its battery may be flat"
                )
            try:
                await self._connect_once(address, timeout)
                return
            except ConnectionError as exc:
                last_error = exc
                log.warning("connect attempt %d failed: %s", attempt + 1, exc)
                if attempt < retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise ConnectionError(
            f"{address}: {last_error}. Possible fixes, in order: retry, "
            "power-cycle the car, toggle the phone's Bluetooth off and on"
        ) from last_error

    async def _connect_once(self, address: str, timeout: float) -> None:
        ad = self._bt_adapter()
        dev = ad.getRemoteDevice(address)
        if dev is None:
            raise ConnectionError(f"{address}: device not found")

        cbs = _shared_cbs()
        # own a session *before* connectGatt: the state callback can arrive
        # on a binder thread the moment the stack accepts the request, and it
        # is routed by this connection's generation.  The bridge object is
        # per connection, tagged with that generation, so a late ACK from a
        # previous connection to the same car cannot resolve this one's
        # pending futures or change its state (see _CallbackRouter).
        router = cbs["router"]
        generation = router.new_session(self)
        self._generation = generation
        self.address = address
        gatt_cb = _classes()["PyGattCallback"](cbs["listener"], generation)
        self._gatt_cb = gatt_cb  # retained until the next connect replaces it
        self._connect_fut = self._loop.create_future()
        # TRANSPORT_LE: the cars are BLE-only; without this some dual-mode
        # phones try BR/EDR first and the connect just hangs.
        try:
            self._gatt = dev.connectGatt(
                self._activity(), False, gatt_cb, TRANSPORT_LE
            )
        except Exception as exc:  # SecurityException without BLUETOOTH_CONNECT
            router.end_session(generation)
            raise ConnectionError(
                f"could not connect to {address}: {exc} - is the app granted "
                "BLUETOOTH_CONNECT?"
            ) from exc
        try:
            await asyncio.wait_for(self._connect_fut, timeout)
        except asyncio.TimeoutError:
            await self.disconnect()
            raise ConnectionError(f"{address} did not answer the link request") from None
        except BaseException:
            # a status failure, a service-discovery failure or cancellation:
            # the attempt did not succeed, so it must release its handle and
            # retire its session exactly like a timeout - otherwise failed
            # attempts accumulate open GATT connections and live generations,
            # and a late callback can resolve a later attempt's future
            await self.disconnect()
            raise
        log.info("android: connected to %s", address)

    @property
    def connected(self) -> bool:
        return self._link_up and self._gatt is not None

    async def disconnect(self) -> None:
        gatt, self._gatt = self._gatt, None
        self._link_up = False
        self._notify_cb = None
        self._chars.clear()
        # release session ownership *before* closing: later events for this
        # generation (late ACKs from the closing link) are dropped by the
        # router instead of being delivered to whatever radio touches the
        # stack next - including a reconnect to the same car address
        if self._generation is not None and _shared is not None:
            _shared["router"].end_session(self._generation)
        # the bridge object stays referenced on the radio: Android may still
        # invoke it during/after close
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
                fut, ConnectionError(f"connect failed: {_gatt_status_text(status)}")
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
            try:
                await asyncio.wait_for(self._write_fut, WRITE_TIMEOUT)
            except asyncio.TimeoutError:
                # the ACK may still arrive after we give up, and ACKs carry
                # no operation id: this frame's outcome is unknowable, so
                # the connection cannot be reused - a late ACK would resolve
                # the NEXT write's future.  Retire the link; the reconnect
                # logic builds a fresh one.
                await self.disconnect()
                raise ConnectionError(
                    f"write to {char_uuid} timed out - connection retired"
                ) from None
