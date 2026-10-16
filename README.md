# revlane

An open, Linux-native controller for **QCAR** Bluetooth RC toy cars — the
die-cast models sold in the *Shell Motorsports Collection* / *Shell Racing*
promotion and normally driven by the closed, account-tied "Shell Racing" app.

> revlane is **not affiliated with, endorsed by, or connected to Brandbase B.V.
> or Shell**. "Brandbase", "QCAR" and "Shell Racing" are trademarks of their
> respective owners and are used here only to describe which toy cars this
> software works with.

The car is driven by 16 byte AES-128-ECB frames over BLE. Everything here is
implemented from scratch against our own hardware; see [PROTOCOL.md](PROTOCOL.md)
for the reverse-engineered spec and [credits](#prior-art) for the people who
documented it first.

Three front ends share one library: a **terminal CLI**, **`qcarctl gui`** (an
SDL2 garage + touch drive pad), and **`qcarctl web`**, which serves the same
control surface to your phone over the LAN.

> **The gotcha.** The control characteristic advertises `write` *and*
> `write-without-response`, but the firmware **silently discards unacknowledged
> writes** — no error, no reaction. You must send ACKed **Write Requests**.
> This is why a first attempt at a controller usually appears to "not work".
> `qcarctl` does the right thing for you.

```
$ .venv/bin/qcarctl scan
00:3C:DE:07:01:A4  QCAR-0701A4

$ .venv/bin/qcarctl drive
connected 00:3C:DE:07:01:A4  key mode: toggle
W/A/S/D or arrows  move   SPACE  stop   L  lights   T  turbo   +/- speed   Q  quit
[FB LR l T] speed=100   batt  95% tx=412
```

## Install

Requires BlueZ ≥ 5.55 and Python ≥ 3.10.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[gui]'      # bleak + cryptography + pygame-ce (SDL2)
.venv/bin/pip install -e '.[gui,gamepad,dev]'
.venv/bin/pip install qrcode            # optional: `qcarctl web --qr`
```

No root, no pairing, no bond — the car accepts plain GATT writes from anyone
in Bluetooth range.

## Commands

| command | what it does |
|-----------|---------------------------------------------------------------|
| `qcarctl gui` | **SDL2 garage + touch drive pad** (see below) |
| `qcarctl web` | **serve a touch controller to your phone over the LAN** (see below) |
| `qcarctl scan` | list QCAR cars in range |
| `qcarctl monitor` | connect and print decrypted telemetry (battery, seq) |
| `qcarctl drive` | keyboard driving in the terminal |
| `qcarctl gamepad` | drive with a gamepad (`pip install evdev`) |
| `qcarctl profile` | list / add / remove saved cars from the shell |
| `qcarctl send` | send one frame or a timed burst, `--dry-run` to just print it |
| `qcarctl probe` | scripted field-by-field probe for an unfamiliar car |
| `qcarctl decode HEX…` | decrypt raw 16 byte frames |

`address` is optional everywhere — with no argument `qcarctl` scans and picks the
only QCAR in range. Examples:

```bash
qcarctl send --forward --turbo --for 2      # 2 s of forward, then stop
qcarctl send --dry-run --left --speed 90    # show the AES frame, don't send
qcarctl drive --mode hold                   # hold-to-drive instead of toggle
qcarctl gamepad --device /dev/input/event7
qcarctl decode 8bb60bc3c5d3db649bff9872e840e661
```

## The garage GUI

```bash
qcarctl gui                      # windowed
qcarctl gui --fullscreen         # tablet / phone
qcarctl gui --width 390 --height 720 --video-driver kmsdrm   # small screen
```

**GARAGE** — hit SCAN, tap ADD on a car, and give it a real name. Profiles keep
an accent colour, a speed cap, turbo/lights defaults, notes, plus the boring
useful bits (battery at last sight, when you last drove it, total time driven):

```
21649fab  Marci's BMW M3   00:3C:DE:07:01:A4  cap  80%  41 min  batt 95
```

They live in `$XDG_CONFIG_HOME/revlane/profiles.json` — plain JSON, so it is
diffable and syncable.

**DRIVE** — an analog thumb pad (drag: up/down is throttle, left/right is
steering, diagonals combine), a big red STOP, turbo and lights toggles, a speed
slider, and a telemetry strip (pad axes, the raw command bytes, frames/s, write
errors, battery). WASD and the arrow keys work too.

Unlike the terminal, the GUI gets *real key and touch release events*, so here
hold-to-drive actually works — release your thumb and it stops instantly.

Layout is fully responsive and asserted in `tests/test_layout.py` from 360x640
(phone portrait) to 1920x1080: nothing overlaps, nothing runs off screen, and if
the grid cannot fit your whole fleet it pages instead of hiding cars.

Safety in the GUI:
- losing window focus releases every held control and stops the car
- leaving the DRIVE scene stops it and records the session
- the radio thread has its own watchdog: if the render loop wedges for >0.5 s,
  it stops forwarding your intent and pushes STOP

### Mobile / APK

There is now an Android APK (`buildozer.spec`, `main.py`), and the radio sits
behind a transport seam (`qcar/radio.py`) with three backends:

| backend | platform | stack |
|-----------|------------|--------------------------------------------|
| `bleak` | Linux et al. | bleak → D-Bus → BlueZ (unchanged behaviour) |
| `android` | the APK | pyjnius → `BluetoothGatt` (ACKed writes!) |
| `mock` | emulator, CI | in-process virtual car (`qcar/radio_mock.py`) |

`QCAR_RADIO=bleak|android|mock` overrides; otherwise the APK uses `android`
and the desktop uses `bleak`. The GUI, CLI and drive loop are untouched above
the seam.

Build the APK (first build downloads the SDK/NDK and compiles Python + recipes;
expect tens of minutes):

```bash
.venvs/buildozer/bin/buildozer android debug      # -> bin/revlane-*-debug.apk
```

Two p4a recipe fixes ship in `android-recipes/` (wired via
`p4a.local_recipes`): pygame bumped to 2.6.1 (2.1.0's Cython-0.29-generated C
cannot compile against CPython 3.14) plus its missing SIMD blitter sources,
Cython bumped to 3.1.x, and a `-lpython3.14` link fix for cryptography's abi3
Rust extension. Drop them when p4a fixes these upstream.

Testing:

- **Emulator**: the Android emulator has **no Bluetooth at all** — no virtual
  HCI, no host passthrough. `main.py` detects an emulator and defaults to the
  mock car, so the whole app is testable end to end minus the radio.
- **Real phone**: `adb install bin/revlane-*-debug.apk`, grant the Bluetooth
  permissions, and the `android` backend talks to the car directly. BLE runtime
  permissions are requested at launch; the transport targets SDK 31 (see the
  note in `qcar/radio_android.py` before bumping to 33+).

The practical answer on a phone is still also the
**[web controller](#web-controller--drive-it-from-your-phone)** below: the radio
stays on a Linux box and only the touch pad runs on your phone, so it works on
Android *and* iOS with nothing installed.

### Headless rendering (CI / design review)

```bash
qcarctl gui --video-driver offscreen --run-seconds 6 --screenshot /tmp/garage.png
tools/gui_shot.py --outdir /tmp/shots     # every scene, several screen sizes
```

If the window dies with an `X Error ... (GLX)` abort instead of a Python
traceback, your X stack has no usable GL context (common under Xwayland) — add
`--software`, which sets `SDL_RENDER_DRIVER` before SDL initialises.

### Layout gate: the glyph-scan regression tests

`tests/test_gui.py` contains **glyph-level layout gates** (`test_gate_*`): they
render real frames offscreen, capture every text blit and assert no two
glyph masks touch — across screen sizes and UI states (garage, editor modal
with toasts, drive pad). Rect arithmetic is not enough: font boxes include
padding the glyphs do not use, which is exactly how three overlapping-label
bugs reached the phone.

**If you revamp the UI, these tests are part of the definition of done:**

- keep every scan green — a collision is a real overlap a user will see;
- new screens, widgets or states must be added to the scanned states;
- redesigned layouts must update the pinpoint assertions (card bounds, nearby
  cell containment, header stack) alongside the geometry they assert.

## Web controller — drive it from your phone

```bash
qcarctl web --qr          # prints the LAN URL and a QR code
```

This box keeps the Bluetooth link and serves a touch controller to any browser on
your wifi — point your phone at `http://<this-machine>:8753`. Same garage, same
profiles, same drive pad, nothing to install on the phone.

```
                          HTTP (LAN, ~2 ms)
  phone browser  ─────────────────────────────►  qcarctl web
  (touch pad)                                     │  Controller: cap + watchdog
                                                  │  CarBackend  (own thread)
                                                  ▼
                                                BLE ──► car
```

The page POSTs its intent ~10×/second, which doubles as the keep-alive. Plain
HTTP rather than WebSockets, because `CarBackend` is already thread-safe and
poll-driven: an `http.server` worker drives it directly, so there is no
event-loop bridging, no reconnect logic, no JS polyfill — and it works on iOS,
where no amount of SDL2 packaging would have helped.

Safety is decided on the **server**, never trusted to a web page:

| what happens | result |
|---|---|
| you release the pad | next POST is a stop frame |
| tab backgrounded, phone locked, browser killed, wifi dead | POSTs stop, so after `--stale` seconds (default 0.5) the watchdog pushes STOP |
| page asks for 100% on a car capped at 80% | server clamps to 80% |
| malformed or absent JSON body | ignored, server stays up |

```bash
qcarctl web --port 8753 --stale 0.5 --car "Marci's BMW M3"   # connect at startup
qcarctl web --host 127.0.0.1                                 # keep it off the LAN
```

It is **unauthenticated by design** — anyone on your wifi can drive the car. Keep
it on your own network, or bind `127.0.0.1` / use an SSH tunnel. Drive from one
controller at a time: the GUI and the page both write to the same intent.

The bottom strip shows `tx` (frames the radio wrote), `sent` (commands the page
pushed) and RSSI, because "the car stuttered" has three very different causes —
page, link, or firmware — and you can tell them apart from the numbers.

## Library

```python
import asyncio
from qcar import QCar, Command, find

async def main():
    car_dev = (await find())[0]
    async with QCar(device=car_dev) as car:
        await car.burst(Command(forward=True, speed=100), seconds=1.5)
        await car.write(Command())        # stop
        print(car.telemetry)              # Telemetry(seq=12, battery=95)

asyncio.run(main())
```

`Command` is the logical drive state (`forward`, `backward`, `left`, `right`,
`lights`, `speed` 0–100). `Command.encrypt()` gives the exact bytes that go on
the wire; `Command.to_plaintext()` the pre-crypto frame.

## Safety notes

* **The car stops when the frame stream pauses**, so `drive`/`gamepad` keep
  sending your current state at `--interval` seconds. If the process dies, the
  car goes limp rather than running away.
* `drive`, `gamepad`, `send` and `probe` all use ACKed Write Requests, which
  also means a dropped Bluetooth link fails loudly instead of leaving the car
  listening to a stale command.
* `drive`/`gamepad` retry transient write failures (the car's stack does
  occasionally answer with `Unlikely Error`) and reconnect if the link drops.
  Recovery is safe: a missing frame already means "stopped".
* Leaving `drive`/`gamepad`/`send --for` always writes an explicit stop frame.
* `--speed` is capped at 100 (`0x64`, the app's "turbo"). There is no braking
  command — "stop" is *throttle off*, so the car coasts.
* `web` forces STOP from a server-side monitor thread once no control POST has
  arrived for `--stale` seconds — a locked or crashed phone cannot leave the car
  driving. Its speed cap is applied after parsing, so a tampered page is still
  bounded by the saved profile.
* Only one central can be connected at a time. Close the official app first.
* Only one *controller* at a time too: `gui`, `web`, `drive` and `gamepad` all
  write to the same intent and will fight each other.
* This is a toy on 86 mAh LiPo. Long bursts on turbo will sag the pack and the
  BLE link gets flaky when it does.

## Troubleshooting

**`connection failed: … not advertising`** — the car is off, out of range, or
still held by another central. `bluetoothctl disconnect <addr>` and retry, or
power-cycle the car.

**`BleakDeviceNotFoundError` right after a crash** — same cause; BlueZ only
knows about the device while it advertises.

**Car ignores everything** — almost always one of two things: (a) you sent an
unacknowledged Write Command instead of a Write Request (see the note at the
top), or (b) the car wants a steady stream, so use
`qcarctl send --for 2 …` rather than one frame. Then run `qcarctl probe` and
watch which fields the car actually reacts to.

**`GATT Protocol Error: Unlikely Error`** — the car's embedded stack sometimes
forgets to ACK under load. `drive`/`gamepad` retry and reconnect around it; if
it ever escapes, the car is already stopped and safe. `tools/live_drive_loop_test.py`
reproduces the load profile without needing a TTY.

**Keys feel stuck / two taps needed** — a raw TTY can't report key *release*;
`--mode hold` trades a ~0.5 s stop lag for hold-to-drive. For true analog
control use `gamepad`.

**Phone cannot reach `http://<box>:8753`** — the page never leaves the phone, so
check in this order: same wifi network (a guest network usually has *client
isolation*, which blocks exactly this), then the firewall
(`sudo firewall-cmd --add-port=8753/tcp` or `sudo ufw allow 8753/tcp`), then use
the address `qcarctl web` prints for your LAN — the `172.17.x.x` one is a Docker
bridge and is not reachable from wifi.

**Car stutters on the web pad** — read the strip at the bottom. If `sent` stops
climbing while you hold the pad, the browser is dropping the gesture. If `sent`
climbs but `tx` does not, it is the radio (watch RSSI: below about -85 dBm
discovery and writes both get unreliable). If both climb and it still stutters,
the car is reacting to the frame rate — raise `--interval` or move closer.

## Repo layout

```
qcar/
  crypto.py          AES-128-ECB + the fixed Brandbase key
  protocol.py        CTL command frames / VBT telemetry, field tables
  car.py             QCar bleak client: find, connect, write (ACKed), stream
  drive.py           resilient command loop: retry, backoff, reconnect
  profiles.py        the garage: named saved cars, JSON store, odometer
  input_kbd.py       raw-TTY keyboard (toggle + hold modes)
  input_gamepad.py   evdev gamepad (optional)
  probe.py           scripted hardware probe
  cli.py             qcarctl entry point
  gui/
    app.py           window, scene stack, pointer, toasts, headless frames
    theme.py         palette + metrics that scale with the window
    widgets.py       buttons, hold buttons, toggles, slider, battery gauge
    base.py          Scene base class
    backend.py       CarBackend: BLE on its own thread + watchdog (no pygame)
    garage.py        garage scene: scan, profile cards, editor modal
    drive.py         drive scene: analog pad, STOP, turbo/lights, telemetry
  web/
    server.py        ThreadingHTTPServer + Controller + STOP watchdog
    index.html       the phone page (inline CSS/JS, no build step, no CDN)
    pad.js           pad direction maths, unit-tested under node
tools/               one-off probes and live smoke tests, not linted
  scan_dump.py       raw advertisement dump for every visible BLE device
  gatt_dump.py       full GATT table + descriptor values
  listen.py          subscribe to everything, log decrypted frames
  live_drive_loop_test.py  scripted no-TTY end-to-end test against a real car
  gui_shot.py        render every scene to PNG without a car
  gui_live_test.py   real-backend GUI smoke test (blinks the headlights)
  web_frame_trace.py trace which AES frames actually reach the car
tests/               102 tests, `pytest tests`
  conftest.py        fake radio backend + synthetic tap/hold/key helpers
  test_protocol.py   known-answer vectors (docs + live captures)
  test_input_kbd.py  key mapping, toggle/hold, auto-repeat suppression
  test_drive.py      recovery from ATT 0x0E, reconnect, give-up path
  test_gui.py        taps, holds, typing, caps, focus loss, telemetry
  test_layout.py     geometry: overlap, overflow, reachability at 7 sizes
  test_web.py        HTTP surface, caps, watchdog timing, pad direction
```

The radio, the protocol and the safety logic are deliberately independent of both
front ends: `CarBackend` imports no pygame, which is what lets the web server and
the SDL app share one implementation and the tests drive both.

## Why the GUI is pygame-ce and not Kivy

pygame-ce *is* SDL2, it has no runtime outside the interpreter, and its event
model already unifies finger and cursor input - which is exactly what a
hold-to-drive pad needs. It also keeps the whole stack importable and testable
headless (`SDL_VIDEODRIVER=offscreen`), so the UI is covered by the same
`pytest` run as the protocol. Kivy would have bought widget scrollbars at the
cost of a much heavier dependency for a two-screen app.

## Prior art

* [`scrool/qcar-docs`](https://github.com/scrool/qcar-docs) — the protocol write-up
* [`tmk907/RacingCarsController`](https://github.com/tmk907/RacingCarsController) — `BrandbaseCar.cs`
* [`HimC29/Shell-Motorsports-Collection-Web-Controller`](https://github.com/HimC29/Shell-Motorsports-Collection-Web-Controller)
* [`Elpoida/ShellRacingController`](https://github.com/Elpoida/ShellRacingController)
* [`whilefalse841/SRL-Car-Controller`](https://github.com/whilefalse841/SRL-Car-Controller) /
  `mesailde/ShellRacingRemote` — the *different* `SL-*` "Shell Racing Legends" protocol

## License

MIT — see [LICENSE](LICENSE). It covers this source only: the *Shell* name and
logos belong to their owner, and the AES key is a fact about the toy, not a
copyrightable work.
