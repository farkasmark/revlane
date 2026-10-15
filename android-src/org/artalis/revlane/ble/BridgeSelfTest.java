package org.artalis.revlane.ble;

/**
 * Test-only helper for tools/run_bridge_selftest.sh (never called by the app).
 *
 * Lets an on-device self-test invoke every PyBleListener method through the
 * JVM with synthetic arguments, proving the pyjnius @java_method descriptors,
 * the interface binding and the Java->Python marshalling without needing
 * Bluetooth hardware. All arguments are constructed on the Java side - the
 * same direction production traffic flows (framework-created byte[] crossing
 * exactly one JNI boundary). Inert in production; ships as a few hundred
 * bytes.
 */
public final class BridgeSelfTest {

    private BridgeSelfTest() {
    }

    public static void fireScanHit(PyBleListener l, long generation) {
        l.onScanHit(generation, "AA:11", "QCAR-1234", -40, 0x5452);
    }

    /** Nameless advertisement - the bridge classes coerce null to ""
     *  before calling the listener (null Strings crash pyjnius). */
    public static void fireScanHitNoName(PyBleListener l, long generation) {
        l.onScanHit(generation, "BB:22", "", -50, -1);
    }

    public static void fireScanFailed(PyBleListener l, long generation) {
        l.onScanFailed(generation, 3);
    }

    public static void fireGattState(PyBleListener l, long generation) {
        l.onGattState(generation, "AA:11", 8, 0);
    }

    public static void fireServicesDiscovered(PyBleListener l, long generation) {
        l.onServicesDiscovered(generation, "AA:11", 0);
    }

    public static void fireCharacteristicWrite(PyBleListener l, long generation) {
        l.onCharacteristicWrite(generation, "AA:11", "0000fff0-0000-1000-8000-00805f9b34fb", 0);
    }

    public static void fireDescriptorWrite(PyBleListener l, long generation) {
        l.onDescriptorWrite(generation, "AA:11", "00002902-0000-1000-8000-00805f9b34fb", 0);
    }

    /** The byte[] comes first in the signature: pyjnius corrupts ByteArray
     *  wrappers when primitives precede the array (see PyBleListener). */
    public static void fireNotify(PyBleListener l, long generation, int b0) {
        l.onNotify(new byte[] {(byte) b0, 0x5A}, generation, "AA:11",
                "0000fff0-0000-1000-8000-00805f9b34fb");
    }

    /** Java-side instanceof - pyjnius proxies do not expose getClass(). */
    public static boolean checkInstance(Object o) {
        return o instanceof PyBleListener;
    }
}
