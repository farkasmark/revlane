"""The transport seam: factory routing + the mock car's firmware simulation.

These tests run the *mock* backend only - no radio, no permissions - which is
exactly what the Android emulator will do at runtime (it has no Bluetooth).
"""

from __future__ import annotations

import asyncio

import pytest

from qcar.crypto import encrypt_block
from qcar.protocol import Command, parse_telemetry
from qcar.radio import Discovered, looks_like_qcar, make_radio
from qcar.radio_mock import MOCK_ADDRESS, MockRadio


# ---------------------------------------------------------------- factory


def test_env_var_picks_backend(monkeypatch):
    monkeypatch.setenv("QCAR_RADIO", "mock")
    assert isinstance(make_radio(), MockRadio)


def test_env_var_rejects_nonsense(monkeypatch):
    monkeypatch.setenv("QCAR_RADIO", "smoke-signals")
    with pytest.raises(ValueError):
        make_radio()


def test_desktop_default_is_bleak(monkeypatch):
    monkeypatch.delenv("QCAR_RADIO", raising=False)
    monkeypatch.delenv("ANDROID_ARGUMENT", raising=False)
    from qcar.radio_bleak import BleakRadio

    assert isinstance(make_radio(), BleakRadio)


# ------------------------------------------------------- the QCAR filter


def test_qcar_match_rules():
    assert looks_like_qcar("QCAR-0701A4")
    assert looks_like_qcar("qcar-1234")
    assert looks_like_qcar(None, {0x5452: b"..."})
    assert looks_like_qcar("QCute-thing") is False  # prefix is QCAR, not QC
    assert looks_like_qcar("SOME_HEADPHONES") is False
    assert looks_like_qcar(None) is False


# ------------------------------------------------------------ mock radio


async def test_scan_finds_the_mock_car_and_filters_decoys():
    r = MockRadio()
    hits = await r.scan(timeout=1)
    assert [h.address for h in hits] == [MOCK_ADDRESS]

    streamed: list[Discovered] = []
    await r.scan(timeout=1, on_device=streamed.append)
    assert [h.address for h in streamed] == [MOCK_ADDRESS]


async def test_connect_rejects_unknown_address():
    r = MockRadio()
    with pytest.raises(ConnectionError):
        await r.connect("00:00:00:00:00:00")
    assert not r.connected


async def test_write_is_decrypted_and_drives_simulation():
    r = MockRadio()
    await r.connect(MOCK_ADDRESS)
    tel: list[bytes] = []
    await r.start_notify("d44b...9601", tel.append)

    await r.write("d44b...9600", Command(forward=True, lights=True).encrypt())
    assert r.last_command.forward
    assert r.last_command.lights

    # forward wins over backward, like the firmware
    await r.write("d44b...9600", Command(forward=True, backward=True).encrypt())
    assert r.last_command.forward and not r.last_command.backward

    await r.disconnect()
    assert not r.connected


async def test_telemetry_notifications_are_real_frames():
    r = MockRadio()
    await r.connect(MOCK_ADDRESS)
    tel: list[bytes] = []
    await r.start_notify("d44b...9601", tel.append)

    await asyncio.sleep(1.2)  # > 2 telemetry periods (0.5 s)
    assert len(tel) >= 2, "notifications keep flowing"

    # every frame must be a valid, decryptable VBT telemetry frame
    parsed = [parse_telemetry(f) for f in tel]
    assert all(t is not None for t in parsed)
    seqs = [t.seq for t in parsed]
    assert seqs == sorted(seqs), "sequence counter increases"
    assert all(0 <= t.battery <= 100 for t in parsed)

    # battery drains while driving, not while parked
    before = parsed[-1].battery
    await r.write("d44b...9600", Command(forward=True).encrypt())
    await asyncio.sleep(1.1)
    after = [t for t in (parse_telemetry(f) for f in tel) if t][-1].battery
    assert after < before


async def test_disconnect_stops_telemetry():
    r = MockRadio()
    await r.connect(MOCK_ADDRESS)
    tel: list[bytes] = []
    await r.start_notify("d44b...9601", tel.append)
    await r.disconnect()
    await asyncio.sleep(1.1)
    assert len(tel) == 0, "no notifications after disconnect"


async def test_write_latency_mirrors_ack_round_trip():
    """The ~45 ms ACKed-write pace is part of the contract the GUI relies on."""
    import time

    r = MockRadio()
    await r.connect(MOCK_ADDRESS)
    t0 = time.monotonic()
    await r.write("d44b...9600", Command().encrypt())
    assert 0.04 <= time.monotonic() - t0 < 0.5


async def test_garbage_frames_are_ignored():
    r = MockRadio()
    await r.connect(MOCK_ADDRESS)
    await r.write("d44b...9600", b"not-a-frame")  # wrong size -> ignored
    await r.write("d44b...9600", encrypt_block(bytes(16)))  # no magic -> ignored
    assert r.last_command == Command()
