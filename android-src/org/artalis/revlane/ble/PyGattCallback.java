package org.artalis.revlane.ble;

import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;

/**
 * Extends the framework's abstract BluetoothGattCallback (pyjnius can only
 * implement interfaces) and forwards GATT events to the Python {@link
 * PyBleListener}, keyed by the session generation, the remote device address
 * and the characteristic UUID.
 *
 * Only the *legacy* onCharacteristicChanged(BluetoothGatt,
 * BluetoothGattCharacteristic) overload is overridden on purpose: the app
 * targets SDK 31, so the framework keeps calling the legacy signature and
 * replays the value into the characteristic. If the targetSdk ever moves to
 * 33+, add the byte[] overloads here (see qcar/radio_android.py).
 *
 * One instance per connection session, constructed with the session's
 * generation token. Sequential connections to the same car address are
 * otherwise indistinguishable to the Python router: a late ACK from the
 * closed connection would resolve the next connection's pending write
 * future. With the token, stale-session events are dropped.
 *
 * Lifetime: the Python radio retains this object for the whole connection
 * (and until the next connection replaces it).
 */
public class PyGattCallback extends BluetoothGattCallback {

    private final PyBleListener listener;
    private final long generation;

    public PyGattCallback(PyBleListener listener, long generation) {
        this.listener = listener;
        this.generation = generation;
    }

    @Override
    public void onConnectionStateChange(BluetoothGatt gatt, int status, int newState) {
        if (listener == null || gatt == null) {
            return;
        }
        BluetoothDevice dev = gatt.getDevice();
        listener.onGattState(generation, address(dev), status, newState);
    }

    @Override
    public void onServicesDiscovered(BluetoothGatt gatt, int status) {
        if (listener == null || gatt == null) {
            return;
        }
        listener.onServicesDiscovered(generation, address(gatt.getDevice()), status);
    }

    @Override
    public void onCharacteristicWrite(BluetoothGatt gatt,
                                      BluetoothGattCharacteristic characteristic,
                                      int status) {
        if (listener == null) {
            return;
        }
        listener.onCharacteristicWrite(generation, addressOf(gatt), uuid(characteristic), status);
    }

    @Override
    public void onDescriptorWrite(BluetoothGatt gatt,
                                  BluetoothGattDescriptor descriptor,
                                  int status) {
        if (listener == null) {
            return;
        }
        listener.onDescriptorWrite(generation, addressOf(gatt), uuid(descriptor), status);
    }

    @Override
    public void onCharacteristicChanged(BluetoothGatt gatt,
                                        BluetoothGattCharacteristic characteristic) {
        if (listener == null || characteristic == null) {
            return;
        }
        byte[] value = characteristic.getValue();
        listener.onNotify(value, generation, addressOf(gatt), uuid(characteristic));
    }

    // null strings must never cross into pyjnius: GetStringChars on a NULL
    // jstring aborts the process (seen in the bridge self-test)
    private static String address(BluetoothDevice dev) {
        return dev != null ? dev.getAddress() : "";
    }

    private static String addressOf(BluetoothGatt gatt) {
        return gatt != null ? address(gatt.getDevice()) : "";
    }

    private static String uuid(BluetoothGattCharacteristic c) {
        return (c != null && c.getUuid() != null) ? c.getUuid().toString() : "";
    }

    private static String uuid(BluetoothGattDescriptor d) {
        return (d != null && d.getUuid() != null) ? d.getUuid().toString() : "";
    }
}
