[app]
title = revlane
package.name = revlane
package.domain = org.artalis
source.dir = .
source.include_exts = py,png,jpg,md,txt
source.exclude_dirs = tests,tools,docs,.venv,.pytest_cache,.ruff_cache,build,.buildozer,revlane.egg-info

# SDL2 garage + drive pad: pygame recipe pulls sdl2, sdl2_ttf, sdl2_image,
# sdl2_mixer; cryptography pulls openssl + cffi (Rust build); pyjnius is the
# Java bridge for the real (non-mock) BLE transport - the emulator's mock
# radio never imports it, so the bridge self-test is what catches it missing
requirements = python3,pygame,cryptography,pyjnius

# pygame 2.1.0's Cython-generated C needs a CPython 3.12+ fix - see
# android-recipes/pygame/__init__.py
p4a.local_recipes = android-recipes

# Java side of the BLE bridge: ScanCallback/BluetoothGattCallback are
# abstract classes (pyjnius can only implement interfaces), so these extend
# them and forward events to Python - see qcar/radio_android.py
android.add_src = android-src

version = 0.1.0
orientation = landscape
fullscreen = True

# BLE needs BLUETOOTH_CONNECT/SCAN (API 31+) and ACCESS_FINE_LOCATION (<=30);
# the pre-31 BLUETOOTH* ones cover older phones. main.py asks at runtime.
android.permissions = BLUETOOTH,BLUETOOTH_ADMIN,BLUETOOTH_SCAN,BLUETOOTH_CONNECT,ACCESS_FINE_LOCATION,ACCESS_COARSE_LOCATION

# targetSdk 31 keeps Android's legacy onCharacteristicChanged path (see
# qcar/radio_android.py); bumping to 33+ means swapping that callback.
android.api = 31
android.minimum_api = 26

# the phone APK; for the x86_64 emulator build override with:
#   buildozer setdefault android.archs x86_64   (or edit this line)
android.archs = arm64-v8a

android.accept_sdk_license = True
android.allow_backup = False

log_level = 2

[buildozer]
log_level = 2
warn_on_root = 1
# pin python-for-android: buildozer clones from git, don't chase master
p4a.commit = v2026.05.09
