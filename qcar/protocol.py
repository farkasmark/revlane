"""Frame layout for the Brandbase "QCAR" (Shell Motorsports / Shell Racing) cars.

Direction: client -> car, written (no response) to characteristic
``d44bc439-abfd-45a2-b575-925416129600`` inside service ``0000fff0-...``,
AES-128-ECB encrypted, exactly 16 bytes, repeated at ~10-100 Hz.  The car goes
 limp (stops) if the stream stops for longer than roughly a second, so a
controller must keep sending its "idle" frame while it is connected.

offset  len  meaning
------  ---  -------------------------------------------------------------
0       1    unknown / unused (always observed as 0x00)
1       3    magic ``CTL`` (0x43 0x54 0x4c)
4       1    1 = drive forward
5       1    1 = drive backward   (ignored when offset 4 is non-zero)
6       1    1 = steer left
7       1    1 = steer right       (ignored when offset 6 is non-zero)
8       1    0 = lights on, 1 = lights off  (inverted!)
9       1    throttle in percent: 0x50 (80) normal, 0x64 (100) turbo
10      6    zero padding

Direction: car -> client, notified from ``...9601``, AES-decrypted:

offset  len  meaning
------  ---  -------------------------------------------------------------
0       1    sequence counter, +1 per notification since power-on
1       3    magic ``VBT`` (0x56 0x42 0x54)
4       1    battery charge, percent
5      11    zero padding
"""

from __future__ import annotations

import dataclasses

from .crypto import BLOCK_SIZE, decrypt_block, encrypt_block

CMD_MAGIC = b"CTL"
TEL_MAGIC = b"VBT"

SPEED_NORMAL = 0x50
SPEED_TURBO = 0x64
SPEED_MAX = 0x64

DEFAULT_SEND_INTERVAL = 0.05  # seconds between repeated frames


@dataclasses.dataclass(frozen=True)
class Command:
    """Logical drive state.  ``forward`` wins over ``backward``, etc."""

    forward: bool = False
    backward: bool = False
    left: bool = False
    right: bool = False
    lights: bool = False
    speed: int = SPEED_NORMAL

    @property
    def turbo(self) -> bool:
        return self.speed >= SPEED_TURBO

    @property
    def moving(self) -> bool:
        return self.forward or self.backward

    def with_(self, **changes) -> Command:
        return dataclasses.replace(self, **changes)

    def to_plaintext(self) -> bytes:
        speed = max(0, min(SPEED_MAX, int(self.speed)))
        p = bytearray(BLOCK_SIZE)
        p[0] = 0x00
        p[1:4] = CMD_MAGIC
        p[4] = 1 if self.forward else 0
        p[5] = 1 if (self.backward and not self.forward) else 0
        p[6] = 1 if self.left else 0
        p[7] = 1 if (self.right and not self.left) else 0
        p[8] = 0 if self.lights else 1
        p[9] = speed
        return bytes(p)

    def encrypt(self) -> bytes:
        return encrypt_block(self.to_plaintext())

    def __str__(self) -> str:
        bits = "".join(
            c if on else "."
            for c, on in (
                ("F", self.forward),
                ("B", self.backward),
                ("L", self.left),
                ("R", self.right),
                ("l", self.lights),
                ("T", self.turbo),
            )
        )
        return f"[{bits}] speed={self.speed}"


@dataclasses.dataclass(frozen=True)
class Telemetry:
    seq: int
    battery: int

    def __str__(self) -> str:
        return f"seq={self.seq} battery={self.battery}%"


def parse_telemetry(frame: bytes) -> Telemetry | None:
    """Decrypt and decode a notification frame.

    Returns ``None`` if the frame does not look like a telemetry frame (the
    car may notify other things on the same characteristic).
    """
    try:
        p = decrypt_block(bytes(frame))
    except ValueError:
        return None
    if p[1:4] != TEL_MAGIC:
        return None
    return Telemetry(seq=p[0], battery=p[4])


def decode_command_frame(frame: bytes) -> str:
    """Human readable dump of a command frame (useful when sniffing)."""
    p = decrypt_block(bytes(frame))
    return (
        f"plain={p.hex(' ')}  "
        f"fwd={p[4]} back={p[5]} left={p[6]} right={p[7]} "
        f"lights={'on' if p[8] == 0 else 'off'} speed={p[9]}"
    )
