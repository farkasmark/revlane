"""AES-128-ECB codec for the Brandbase "QCAR" Bluetooth toy cars.

The car firmware uses a *single fixed* AES-128 key in ECB mode with no IV and
no padding.  Both directions of the link use it:

  * command frames we write to ``...9600``  (plaintext -> ciphertext)
  * telemetry frames we are notified on ``...9601`` (ciphertext -> plaintext)

Key recovered from the official "Shell Racing" app
(``nl.brandbase.shellsupercars``) and independently documented in
https://github.com/scrool/qcar-docs .
"""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

AES_KEY = bytes.fromhex("34522a5b7a6e492c08090a9d8d2a23f8")

BLOCK_SIZE = 16


def _cipher() -> Cipher:
    return Cipher(algorithms.AES(AES_KEY), modes.ECB())


def encrypt_block(plain: bytes) -> bytes:
    """Encrypt exactly one 16 byte plaintext block."""
    if len(plain) != BLOCK_SIZE:
        raise ValueError(f"frame must be {BLOCK_SIZE} bytes, got {len(plain)}")
    c = _cipher().encryptor()
    return c.update(plain) + c.finalize()


def decrypt_block(cipher_bytes: bytes) -> bytes:
    """Decrypt exactly one 16 byte ciphertext block."""
    if len(cipher_bytes) != BLOCK_SIZE:
        raise ValueError(f"frame must be {BLOCK_SIZE} bytes, got {len(cipher_bytes)}")
    c = _cipher().decryptor()
    return c.update(cipher_bytes) + c.finalize()
