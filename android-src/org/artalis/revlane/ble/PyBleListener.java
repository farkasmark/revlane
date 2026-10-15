package org.artalis.revlane.ble;

/**
 * Events forwarded from the Android Bluetooth stack to the Python radio
 * (qcar/radio_android.py, which implements this interface through pyjnius).
 *
 * pyjnius's PythonJavaClass can implement Java *interfaces* but not abstract
 * classes, and the framework's ScanCallback / BluetoothGattCallback are
 * abstract classes - hence this interface plus the two bridge classes in
 * this package that extend the framework callbacks and delegate here.
 *
 * Every method leads with a **generation**: a session token the Python side
 * allocates per scan/connection and stamps into the bridge object at
 * creation time (PyScanCallback / PyGattCallback constructors). Sequential
 * sessions to the same car address are otherwise indistinguishable - a late
 * ACK from a closed connection would resolve the *next* connection's pending
 * write future, and a late onGattState would corrupt its link state. With
 * the token, an event from an ended session is a stale-generation lookup
 * miss on the Python side and is dropped. Callers MUST forward the
 * generation they were constructed with, unmodified.
 *
 * Everything arrives on binder threads; the Python side re-marshals onto its
 * own asyncio loop. All arguments are plain types so pyjnius has an easy job.
 * The scan callback carries only the manufacturer ID (never the data bytes):
 * car matching uses IDs/names only, and the on-device self-test showed
 * pyjnius corrupts ByteArray wrappers for signatures with ints before a
 * byte[] - see tools/run_bridge_selftest.sh.
 */
public interface PyBleListener {

    /** One advertisement, unfiltered; mfgId &lt; 0 means absent. */
    void onScanHit(long generation, String address, String name, int rssi, int mfgId);

    /** The scan itself failed (see ScanCallback.OnScanFailed* constants). */
    void onScanFailed(long generation, int errorCode);

    /** Connection state changed; newState uses BluetoothProfile.STATE_*. */
    void onGattState(long generation, String address, int status, int newState);

    /** Service discovery finished; status 0 == success. */
    void onServicesDiscovered(long generation, String address, int status);

    /** ACKed write completed; status != 0 means the car rejected the frame. */
    void onCharacteristicWrite(long generation, String address, String charUuid, int status);

    /** CCCD write completed (notification subscribe). */
    void onDescriptorWrite(long generation, String address, String charUuid, int status);

    /** Notification payload from a characteristic.  The byte[] comes first
     *  on purpose: pyjnius corrupts ByteArray wrappers when primitives
     *  precede the array (seen in the bridge self-test), so nothing may sit
     *  before it. */
    void onNotify(byte[] value, long generation, String address, String charUuid);
}
