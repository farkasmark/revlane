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
 * Everything arrives on binder threads; the Python side re-marshals onto its
 * own asyncio loop. All arguments are plain types so pyjnius has an easy job.
 * The scan callback carries only the manufacturer ID (never the data bytes):
 * car matching uses IDs/names only, and the on-device self-test showed
 * pyjnius corrupts ByteArray wrappers for signatures with ints before a
 * byte[] - see tools/run_bridge_selftest.sh.
 */
public interface PyBleListener {

    /** One advertisement, unfiltered; mfgId &lt; 0 means absent. */
    void onScanHit(String address, String name, int rssi, int mfgId);

    /** The scan itself failed (see ScanCallback.OnScanFailed* constants). */
    void onScanFailed(int errorCode);

    /** Connection state changed; newState uses BluetoothProfile.STATE_*. */
    void onGattState(String address, int status, int newState);

    /** Service discovery finished; status 0 == success. */
    void onServicesDiscovered(String address, int status);

    /** ACKed write completed; status != 0 means the car rejected the frame. */
    void onCharacteristicWrite(String address, String charUuid, int status);

    /** CCCD write completed (notification subscribe). */
    void onDescriptorWrite(String address, String charUuid, int status);

    /** Notification payload from a characteristic.
     *  (String, String, byte[]) - no primitives before the array: the one
     *  byte[] shape pyjnius marshals reliably; matches onNotify above. */
    void onNotify(String address, String charUuid, byte[] value);
}
