package org.artalis.revlane.ble;

import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;

/**
 * Extends the framework's abstract BluetoothGattCallback (pyjnius can only
 * implement interfaces) and forwards GATT events to the Python {@link
 * PyBleListener}, keyed by the remote device address and characteristic UUID.
 *
 * Only the *legacy* onCharacteristicChanged(BluetoothGatt,
 * BluetoothGattCharacteristic) overload is overridden on purpose: the app
 * targets SDK 31, so the framework keeps calling the legacy signature and
 * replays the value into the characteristic. If the targetSdk ever moves to
 * 33+, add the byte[] overloads here (see qcar/radio_android.py).
 *
 * Lifetime: the Python radio retains this object for the whole connection.
 */
public class PyGattCallback extends BluetoothGattCallback {

    private final PyBleListener listener;

    public PyGattCallback(PyBleListener listener) {
        this.listener = listener;
    }

    @Override
    public void onConnectionStateChange(BluetoothGatt gatt, int status, int newState) {
        if (listener == null || gatt == null) {
            return;
        }
        BluetoothDevice dev = gatt.getDevice();
        listener.onGattState(address(dev), status, newState);
    }

    @Override
    public void onServicesDiscovered(BluetoothGatt gatt, int status) {
        if (listener == null || gatt == null) {
            return;
        }
        listener.onServicesDiscovered(address(gatt.getDevice()), status);
    }

    @Override
    public void onCharacteristicWrite(BluetoothGatt gatt,
                                      BluetoothGattCharacteristic characteristic,
                                      int status) {
        if (listener == null) {
            return;
        }
        listener.onCharacteristicWrite(addressOf(gatt), uuid(characteristic), status);
    }

    @Override
    public void onDescriptorWrite(BluetoothGatt gatt,
                                  BluetoothGattDescriptor descriptor,
                                  int status) {
        if (listener == null) {
            return;
        }
        listener.onDescriptorWrite(addressOf(gatt), uuid(descriptor), status);
    }

    @Override
    public void onCharacteristicChanged(BluetoothGatt gatt,
                                        BluetoothGattCharacteristic characteristic) {
        if (listener == null || characteristic == null) {
            return;
        }
        byte[] value = characteristic.getValue();
        listener.onNotify(addressOf(gatt), uuid(characteristic), value);
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
