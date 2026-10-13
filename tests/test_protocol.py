"""Known-answer tests.

The vectors below come from two independent sources:

* ``scrool/qcar-docs`` - the "typical message to keep device steady" ciphertext
* frames we captured off our own car (QCAR-0701A4) with tools/listen.py
"""

import pytest

from qcar.crypto import AES_KEY, decrypt_block, encrypt_block
from qcar.protocol import Command, parse_telemetry

STEADY_PLAIN = bytes.fromhex("0043544c000001000050000000000000".replace(" ", ""))
STEADY_CRYPTO = bytes.fromhex("e1055f54d880f49c2ce547267f930bf2")

# (ciphertext, seq, battery) - captured live from QCAR-0701A4
TELEMETRY_VECTORS = [
    ("8b b6 0b c3 c5 d3 db 64 9b ff 98 72 e8 40 e6 61".replace(" ", ""), 0x07, 95),
    ("63 4e ee c2 87 15 5b 3e 20 e7 cd cd fd f1 a1 af".replace(" ", ""), 0x08, 95),
    ("8d a8 3c 9e 29 80 fa 06 f8 17 92 16 d8 f4 ef aa".replace(" ", ""), 0x09, 95),
    ("4b 64 21 19 75 ea 73 2e 77 35 2a 79 14 a4 f2 fc".replace(" ", ""), 0x0A, 95),
    ("7e 41 b3 21 09 f1 ca cb 0a f5 b6 a7 88 1d 93 f1".replace(" ", ""), 0x0B, 95),
]


def test_key():
    assert AES_KEY == bytes.fromhex("34522a5b7a6e492c08090a9d8d2a23f8")
    assert len(AES_KEY) == 16


def test_steady_frame_from_docs():
    assert encrypt_block(STEADY_PLAIN) == STEADY_CRYPTO
    assert decrypt_block(STEADY_CRYPTO) == STEADY_PLAIN


@pytest.mark.parametrize("hexstr,seq,battery", TELEMETRY_VECTORS)
def test_live_telemetry(hexstr, seq, battery):
    frame = bytes.fromhex(hexstr)
    tel = parse_telemetry(frame)
    assert tel is not None
    assert (tel.seq, tel.battery) == (seq, battery)


def test_non_telemetry_is_none():
    assert parse_telemetry(encrypt_block(Command(forward=True).to_plaintext())) is None


def test_roundtrip():
    cmd = Command(forward=True, left=True, lights=True, speed=100)
    assert decrypt_block(encrypt_block(cmd.to_plaintext())) == cmd.to_plaintext()


def test_frame_layout():
    p = Command(forward=True, left=True, lights=True, speed=100).to_plaintext()
    assert p[1:4] == b"CTL"
    assert (p[4], p[5], p[6], p[7], p[8], p[9]) == (1, 0, 1, 0, 0, 100)
    assert p[10:] == bytes(6)


def test_stop_frame():
    p = Command().to_plaintext()
    assert p.hex() == "0043544c000000000150000000000000".replace(" ", "")
    assert p[1:4] == b"CTL"
    assert p[8] == 1  # lights off


def test_precedence():
    p = Command(forward=True, backward=True, left=True, right=True).to_plaintext()
    assert (p[4], p[5]) == (1, 0)
    assert (p[6], p[7]) == (1, 0)


def test_speed_is_clamped():
    assert Command(speed=999).to_plaintext()[9] == 0x64
    assert Command(speed=-5).to_plaintext()[9] == 0


def test_bad_length():
    with pytest.raises(ValueError):
        encrypt_block(b"short")
