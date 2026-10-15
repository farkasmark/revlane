#!/usr/bin/env bash
# Run the BLE bridge self-test on a connected Android device/emulator.
#
# Validates the pyjnius layer of qcar/radio_android.py without Bluetooth:
# class loading, proxy creation, and synthetic Java->Python callbacks
# (asserting round-tripped arguments, which exercises every @java_method
# JNI descriptor). What it can NOT validate: actual BLE dispatch and GATT
# behaviour - that needs a phone and a car.
#
# Mechanism: the app's entry point (files/app/main.py[c]) is swapped for
# tools/bridge_selftest_main.py, the app is relaunched, and the verdict is
# read from logcat ("I python" lines, BRIDGE-SELFTEST markers). The original
# entry point is restored afterwards, and the app relaunched normally.
#
# Prerequisites: a debug build installed (bin/revlane-*-debug.apk) that
# contains the BridgeSelfTest shim (android-src/, compiled into the APK),
# and a device attached to adb. Run from the repo root:
#   tools/run_bridge_selftest.sh
set -euo pipefail

PKG=org.artalis.revlane
ADB="${ADB:-$HOME/.buildozer/android/platform/android-sdk/platform-tools/adb}"
SCRIPT="$(cd "$(dirname "$0")" && pwd)/bridge_selftest_main.py"

device() {
  if [ -n "${ANDROID_SERIAL:-}" ]; then echo "$ANDROID_SERIAL"; return; fi
  "$ADB" devices | awk 'NR>1 && $2=="device" {print $1; exit}'
}
DEV=$(device)
[ -n "$DEV" ] || { echo "no adb device attached"; exit 2; }
echo "== device: $DEV"

"$ADB" -s "$DEV" shell pm list packages | grep -q "$PKG" || {
  echo "$PKG not installed - build and install bin/revlane-*-debug.apk first"; exit 2;
}

# warm-up launch: after a (re)install, p4a re-extracts the python bundle on
# first start, which would clobber the swapped-in entry point
"$ADB" -s "$DEV" shell am force-stop "$PKG"
"$ADB" -s "$DEV" shell am start -n "$PKG/org.kivy.android.PythonActivity" >/dev/null
sleep 15
"$ADB" -s "$DEV" shell am force-stop "$PKG"

# quote the whole remote command: adb shell drops one level of quoting,
# so the inner sh -c argument must survive as a single string on-device
as_app() { "$ADB" -s "$DEV" shell "run-as $PKG sh -c '$1'"; }

restore() {
  as_app "rm -f files/app/main.py" || true
  as_app "if [ -f files/app/main.pyc.bak ]; then mv files/app/main.pyc.bak files/app/main.pyc; fi" || true
}
trap restore EXIT

# idempotency: undo any leftovers from an aborted previous run
as_app "if [ -f files/app/main.pyc.bak ]; then mv files/app/main.pyc.bak files/app/main.pyc; fi"
as_app "rm -f files/app/main.py"

as_app "mv files/app/main.pyc files/app/main.pyc.bak"  # rename aside: getEntryPoint prefers main.pyc
"$ADB" -s "$DEV" push "$SCRIPT" /data/local/tmp/bridge_selftest_main.py >/dev/null
as_app "cp /data/local/tmp/bridge_selftest_main.py files/app/main.py"

"$ADB" -s "$DEV" logcat -c
"$ADB" -s "$DEV" shell am force-stop "$PKG"
"$ADB" -s "$DEV" shell am start -n "$PKG/org.kivy.android.PythonActivity" >/dev/null
echo "== self-test running (app entry point swapped), polling logcat..."

verdict=""
for _ in $(seq 1 60); do
  sleep 2
  verdict=$("$ADB" -s "$DEV" shell "logcat -d | grep 'I python' | grep 'BRIDGE-SELFTEST: VERDICT'" | tail -1 || true)
  [ -n "$verdict" ] && break
done

echo "== log:"
"$ADB" -s "$DEV" shell "logcat -d | grep 'I python' | grep 'BRIDGE-SELFTEST'" | sed 's/^.*I python *: //'

"$ADB" -s "$DEV" shell am force-stop "$PKG"

if echo "$verdict" | grep -q "VERDICT PASS"; then
  echo "== BRIDGE SELF-TEST PASSED"
  result=0
else
  echo "== BRIDGE SELF-TEST FAILED (or timed out) - see log above"
  result=1
fi

restore                       # put the real entry point back (trap no-ops after)
trap - EXIT
"$ADB" -s "$DEV" shell am start -n "$PKG/org.kivy.android.PythonActivity" >/dev/null
exit $result
