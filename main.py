"""APK entry point - python-for-android's SDL2 bootstrap runs this file.

On the desktop this stays out of the way: ``python main.py`` just forwards to
the ``qcarctl`` CLI. Inside the APK it requests the Bluetooth runtime
permissions, picks the radio backend and starts the SDL2 garage full-screen.

Radio choice on Android (see qcar/radio.py): ``QCAR_RADIO`` wins if set
(debug builds can hard-code it here), otherwise an **emulator** gets the mock
- it has no Bluetooth at all, no virtual HCI, no host passthrough - and a
**real phone** gets the real Android BLE transport.
"""

from __future__ import annotations

import os


def _in_apk() -> bool:
    return bool(os.environ.get("ANDROID_ARGUMENT"))


def _on_emulator() -> bool:
    """Best-effort emulator detection through android.os.Build."""
    try:
        from jnius import autoclass

        build = autoclass("android.os.Build")
        fingerprint = (build.FINGERPRINT or "").lower()
        model = (build.MODEL or "").lower()
        product = (build.PRODUCT or "").lower()
        markers = ("generic", "sdk_gphone", "emulator", "vbox", "vsoc")
        return (
            any(m in fingerprint for m in markers)
            or "emulator" in model
            or product.startswith("sdk")
            or model.startswith("sdk")
        )
    except Exception:
        return False


def _request_ble_permissions() -> None:
    """Dangerous permissions must be asked for at runtime (API 23+).

    Requesting a permission the OS does not know is harmless, so we ask for
    the union of the pre-31 and post-31 BLE sets and let each Android version
    ignore what does not apply.
    """
    try:
        from android.permissions import Permission, request_permissions

        request_permissions(
            [
                Permission.BLUETOOTH_CONNECT,
                Permission.BLUETOOTH_SCAN,
                Permission.ACCESS_FINE_LOCATION,
                Permission.ACCESS_COARSE_LOCATION,
                Permission.BLUETOOTH,
                Permission.BLUETOOTH_ADMIN,
            ]
        )
    except Exception:
        # headless/service contexts or odd builds: the radio will raise a
        # clear ConnectionError when it actually cannot scan
        pass


def _run_android() -> None:
    _request_ble_permissions()
    if not os.environ.get("QCAR_RADIO"):
        os.environ["QCAR_RADIO"] = "mock" if _on_emulator() else "android"
    if os.environ["QCAR_RADIO"] == "android":
        # must happen on this (bootstrap/Java-attached) thread: worker
        # threads attach to the JVM without a classloader context and
        # cannot resolve our android-src bridge classes - see prime()
        from qcar import radio_android

        radio_android.prime()
    # p4a never sets HOME, and an app uid has no passwd entry, so
    # expanduser("~") stays a literal "~" - give libraries that do the sane
    # writable directory (qcar/profiles.py handles it explicitly itself)
    os.environ.setdefault("HOME", os.environ.get("ANDROID_PRIVATE", "/data/local/tmp"))

    import pygame  # noqa: E402  (after permissions; SDL starts below anyway)

    from qcar.gui.app import App

    class AndroidApp(App):
        """Home button minimises; back button exits cleanly."""

        def handle_event(self, ev) -> bool:
            back = getattr(pygame, "K_AC_BACK", None)
            if back is not None and getattr(ev, "key", None) == back:
                return False
            return super().handle_event(ev)

    app = AndroidApp(size=(1600, 900), fullscreen=True)
    app.run()


if _in_apk():
    _run_android()
else:
    from qcar.cli import main  # noqa: E402

    main()
