"""Open controller library for "QCAR" Bluetooth toy cars.

These are the die-cast RC cars sold in the Brandbase *Shell Motorsports
Collection* / *Shell Racing* promotion and normally driven by the closed
`nl.brandbase.shellsupercars` app. Brand names appear here only to say which
cars this library interoperates with.

revlane is not affiliated with, endorsed by, or connected to Brandbase B.V. or
Shell; the names are trademarks of their respective owners.
"""

from .car import QCar, find
from .crypto import AES_KEY, decrypt_block, encrypt_block
from .protocol import Command, Telemetry, parse_telemetry

__version__ = "0.1.0"

#: Our own brand. Deliberately contains no third-party mark: the place a
#: project gets into trademark trouble is the surfaces that look like
#: endorsement - the window title, the package name, the page <title>.
#: Third-party names belong in prose that describes compatibility, not in
#: branding. See tests/test_branding.py.
APP_NAME = "revlane"

#: where the garage lives, e.g. ~/.config/revlane/profiles.json
CONFIG_DIR = "revlane"

__all__ = [
    "AES_KEY",
    "APP_NAME",
    "CONFIG_DIR",
    "Command",
    "QCar",
    "Telemetry",
    "decrypt_block",
    "encrypt_block",
    "find",
    "parse_telemetry",
]
