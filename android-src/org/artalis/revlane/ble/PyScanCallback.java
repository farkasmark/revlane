package org.artalis.revlane.ble;

import android.bluetooth.BluetoothDevice;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanRecord;
import android.util.SparseArray;

/**
 * Extends the framework's abstract ScanCallback (which pyjnius cannot
 * implement directly - it can only implement interfaces) and forwards every
 * event to the Python {@link PyBleListener}, flattening the ScanResult into
 * plain types on the Java side.
 *
 * One instance per scan session, constructed with the session's generation
 * token; every forwarded event leads with it, so the Python router can drop
 * late events from an already-ended scan instead of delivering them to
 * whichever radio scans next.
 *
 * Lifetime: the Python radio retains this object for as long as the scan is
 * registered (and until the next scan replaces it); instances must not be
 * garbage collected while Android may still call into them.
 */
public class PyScanCallback extends ScanCallback {

    private final PyBleListener listener;
    private final long generation;

    public PyScanCallback(PyBleListener listener, long generation) {
        this.listener = listener;
        this.generation = generation;
    }

    @Override
    public void onScanResult(int callbackType, ScanResult result) {
        if (listener == null || result == null) {
            return;
        }
        BluetoothDevice dev = result.getDevice();
        // null strings must never cross into pyjnius: GetStringChars on a
        // NULL jstring aborts the process (seen in the bridge self-test)
        String address = dev != null ? dev.getAddress() : "";
        String name = dev != null ? dev.getName() : "";
        ScanRecord record = result.getScanRecord();
        if (name == null && record != null) {
            name = record.getDeviceName();
        }
        if (name == null) {
            name = "";
        }
        int mfgId = -1;
        if (record != null) {
            SparseArray<byte[]> sa = record.getManufacturerSpecificData();
            if (sa != null && sa.size() > 0) {
                mfgId = sa.keyAt(0);  // id only - the data bytes are never used
            }
        }
        listener.onScanHit(generation, address, name, result.getRssi(), mfgId);
    }

    @Override
    public void onBatchScanResults(java.util.List<ScanResult> results) {
        if (listener == null || results == null) {
            return;
        }
        for (ScanResult r : results) {
            onScanResult(0, r);
        }
    }

    @Override
    public void onScanFailed(int errorCode) {
        if (listener != null) {
            listener.onScanFailed(generation, errorCode);
        }
    }
}
