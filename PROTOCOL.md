# QCAR BLE protocol — reverse engineering notes

Target: `QCAR-0701A4`, a Brandbase Bluetooth RC car sold as part of the
**Shell Motorsports Collection** / **Shell Racing** promotion (controlled by
the `nl.brandbase.shellsupercars` "Shell Racing" app).

Everything below was confirmed against our own hardware on Linux/BlueZ and
cross-checked with [`scrool/qcar-docs`][qcar-docs] and
[`tmk907/RacingCarsController`][tmk907].

[qcar-docs]: https://github.com/scrool/qcar-docs
[tmk907]: https://github.com/tmk907/RacingCarsController

## Identification

```
$ tools/scan_dump.py 15
00:3C:DE:07:01:A4  name='QCAR-0701A4'
     uuids: 0000fff0-0000-1000-8000-00805f9b34fb
     mfg  0x5452: 00 3c
```

* name: `QCAR-` + upper-case hex suffix of the MAC
* manufacturer id `0x5452` = `"TR"`, data = first two bytes of the MAC
* advertised service `0xFFF0` — this is NXP's *QPP* serial profile shell
  ([AN11846](https://www.nxp.com/docs/en/application-note/AN11846.pdf)), hence
  the `d44bc439-abfd-45a2-b575-9254161296xx` characteristic UUIDs.

## GATT table (as read from our car)

```
Primary 0000fff0-...  handle 0x0010
  char d44bc439-abfd-45a2-b575-925416129600  h=0x0011  [write, write-no-resp]
       desc 2901 = "TR1911R02-08A"          <- board/firmware id
  char d44bc439-abfd-45a2-b575-92541612960a  h=0x0014  [write, write-no-resp]
       desc 2901 = "11A"
  char d44bc439-abfd-45a2-b575-92541612960b  h=0x0017  [write, write-no-resp]
  char d44bc439-abfd-45a2-b575-925416129601  h=0x001a  [notify]

Primary 0000fd00-...  handle 0x001d
  char 0000fd01-...  h=0x001e  [write-no-resp]
  char 0000fd02-...  h=0x0020  [write, notify]

Primary 00001801 (Generic Attribute)
  char 00002a05 (Service Changed) h=0x000d [read, indicate] = 01 00 ff ff
```

`9600` / `9601` are the drive/telemetry pair. `960a`, `960b`, `fd01`, `fd02`
are undocumented and unexplored (DFU? the app's game/audio features?).

## Encryption

AES-**128-ECB**, no IV, **no padding**, one 16 byte block per BLE packet, in
both directions, with one hardcoded key:

```
34 52 2a 5b 7a 6e 49 2c 08 09 0a 9d 8d 2a 23 f8
```

Because ECB is stateless and the key is global, no handshake, pairing or bond
is needed: any nearby device can drive any car. There is no replay protection
(the command frame has no counter — only the car→client telemetry does).

## Client → car: command frame

Write to `...9600`, 16 plaintext bytes, then encrypt.

| off | len | value | meaning |
|-----|-----|-------|---------|
| 0   | 1   | `0x00` | unused |
| 1   | 3   | `"CTL"` | magic |
| 4   | 1   | 0/1 | drive forward |
| 5   | 1   | 0/1 | drive backward (ignored if offset 4 set) |
| 6   | 1   | 0/1 | steer left |
| 7   | 1   | 0/1 | steer right (ignored if offset 6 set) |
| 8   | 1   | **0 = lights on**, 1 = off | inverted flag |
| 9   | 1   | percent | throttle: `0x50` (80) normal, `0x64` (100) turbo |
| 10  | 6   | `0x00` | padding |

The car drops to a standstill if the frame stream pauses, so a controller must
re-send its current state continuously (~10 ms in the official app; 50-100 ms
works fine - see `qcarctl drive --interval`).

### You MUST use acknowledged Write Requests

This is the single easiest thing to get wrong, and it looks like "the protocol
is broken". The characteristic advertises **both** `write` and
`write-without-response`, but the firmware's ATT layer accepts unacknowledged
Write Commands and then throws them away - no error, no effect. Motor and
headlights simply never react.

Verified on our car, same frames both ways:

| ATT op | frames sent | motor | lights |
|--------|-------------|-------|--------|
| Write Command (`WR_CMD`, no ACK) | 146 | no reaction | no reaction |
| Write Request (`WR_REQ`, ACKed) | 26 | **drives** | **lights on** |

`QCar.write()` therefore defaults to `response=True`. The ACK has a second
benefit: vendor rejections surface as ATT errors (the firmware does use
custom error `0x80`, e.g. when you try to read the `2901` descriptor on `960b`)
instead of vanishing.

The cost is one round trip per frame - measured ~46 ms on this car's preferred
connection interval, so ~22-26 Hz. That is plenty; the car does not notice.
(Asking for a faster loop than that does not help: the link, not the CPU,
bottlenecks it.)

### Sporadic ATT 0x0E `Unlikely Error`

Under load the car's GATT server sometimes answers a perfectly valid Write
Request with ATT error `0x0E` ("unlikely error") instead of ACKing. It is
sporadic - not reproducible on demand, roughly one crash per few minutes of
hammering in our first build - and it is the embedded stack being flaky, not a
protocol misunderstanding.

Handle it the way `qcar/drive.py` does: retry with a small backoff, escalate to
a reconnect if errors pile up, and never let it escape as a traceback. This is
safe by construction - the car has *already* gone limp because a frame was
missing, so a failed write degrades to "stopped", never to "runaway".

Known-answer vector from the wild ("keep steady" example, note `left=1`):

```
plain  00 43 54 4c 00 00 01 00 00 50 00 00 00 00 00 00
crypto e1 05 5f 54 d8 80 f4 9c 2c e5 47 26 7f 93 0b f2
```

## Car → client: battery telemetry

Notify on `...9601`, decrypt each 16 byte block:

| off | len | value | meaning |
|-----|-----|-------|---------|
| 0   | 1   | 1..n | counter, +1 per notification since power-on |
| 1   | 3   | `"VBT"` | magic |
| 4   | 1   | percent | battery charge |
| 5   | 11  | `0x00` | padding |

Cadence: fast for the first few seconds after subscribing, then roughly every
60 s. Our car reported `95 %` (`0x5f`) in every frame captured.

```
$ .venv/bin/qcarctl decode 8bb60bc3c5d3db649bff9872e840e661
-> telemetry seq=7 battery=95%
```

## Connection quirks

* Preferred connection interval is wide (300–320 ms per `qcar-docs`), so the
  effective frame rate is bounded by the link, not by our send loop.
* BlueZ only exposes a device in D-Bus while it advertises. Once something is
  connected the car stops advertising, so *connect → scan → connect* fails; if
  a previous run died, `bluetoothctl disconnect <addr>` (or power-cycle the
  car) before retrying. `QCar.connect()` scans first for exactly this reason.
* Only one central at a time. The official app and `qcarctl` cannot both hold
  the link.

## Unknown / TODO

* `960a`, `960b`, `fd01`, `fd02` - never probed.
* Whether offset 0 is truly unused.
* Whether offset 9 is a true analog throttle below the app's `0x50` floor
  (`qcarctl probe --which speed` walks 32/64/80/100 - report what you see).
* `Shell Racing Legends` cars advertise as `SL-<model name>` and use a
  **different** protocol (`mesailde/ShellRacingRemote`); this repo does not
  implement them.
