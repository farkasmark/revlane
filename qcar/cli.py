"""qcarctl - open controller for Brandbase "QCAR" Bluetooth toy cars."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from pathlib import Path

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from . import APP_NAME, __version__
from .car import CMD_CHAR_UUID, SERVICE_UUID, TEL_CHAR_UUID, QCar, find
from .crypto import AES_KEY
from .profiles import Garage
from .protocol import (
    SPEED_NORMAL,
    SPEED_TURBO,
    Command,
    Telemetry,
    decode_command_frame,
    parse_telemetry,
)

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"



# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def resolve(args) -> str:
    if getattr(args, "address", None):
        return args.address
    print("scanning for QCAR cars ...")
    cars: list[BLEDevice] = await find(timeout=args.scan_timeout)
    if not cars:
        sys.exit("no QCAR car found - is it switched on and in range?")
    for d in cars:
        print(f"  {d.address}  {d.name}")
    if len(cars) > 1 and not getattr(args, "yes", False):
        sys.exit("more than one car; pass an address (or --yes to take the first)")
    return cars[0].address


def hud(car: QCar, cmd: Command, extra: str = "") -> str:
    t = car.telemetry
    batt = f"{t.battery:>3}%" if t else "  n/a"
    return (
        f"\r{GREEN}{str(cmd):<28}{RESET} batt {batt} "
        f"{DIM}tx={car.tx_count}{RESET} {extra:<28}"
    )


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------


async def cmd_scan(args) -> None:
    cars = await find(timeout=args.scan_timeout)
    if not cars:
        print("no QCAR car found")
        return
    for d in cars:
        print(f"{d.address}  {d.name or '(unnamed)'}")


async def cmd_monitor(args) -> None:
    address = await resolve(args)
    async with QCar(address, adapter=args.adapter) as car:
        print(f"connected {address}  (ctrl {CMD_CHAR_UUID[:8]} -> {SERVICE_UUID[:8]}, "
              f"tel {TEL_CHAR_UUID[:8]})")

        def show(t: Telemetry) -> None:
            print(f"[{time.strftime('%H:%M:%S')}] {t}")

        car.add_telemetry_listener(show)
        print("waiting for telemetry (the car reports roughly every 60 s) ... ctrl-c to quit")
        while True:
            await asyncio.sleep(1)


async def cmd_send(args) -> None:
    cmd = Command(
        forward=args.forward,
        backward=args.backward,
        left=args.left,
        right=args.right,
        lights=args.lights,
        speed=SPEED_TURBO if args.turbo else args.speed,
    )
    plain = cmd.to_plaintext()
    print(f"plain  {plain.hex(' ')}")
    print(f"crypto {cmd.encrypt().hex()}")
    if args.dry_run:
        return
    address = await resolve(args)
    response = False if args.no_ack else True
    async with QCar(address, adapter=args.adapter) as car:
        if args.for_ <= 0:
            await car.write(cmd, response=response)
        else:
            await car.burst(cmd, args.for_, interval=args.interval, response=response)
        print(f"sent {car.tx_count} frames to {address} "
              f"({'Write Request' if response else 'Write Command'})")


def _render(car: QCar):
    """HUD callback for DriveLoop."""

    def render(cmd: Command, extra: str) -> None:
        sys.stdout.write(hud(car, cmd, extra))
        sys.stdout.flush()

    return render


async def finish(car: QCar, loop=None) -> None:
    """Always leave the car stopped, and report how the session ended."""
    with contextlib.suppress(Exception):
        await car.write(Command())
    detail = ""
    if loop is not None and (loop.errors or loop.reconnects):
        detail = f" ({loop.errors} retried writes, {loop.reconnects} reconnects)"
    sys.stdout.write("\n" + YELLOW + "stopped." + RESET + detail + "\n")


async def cmd_drive(args) -> None:
    from .drive import DriveLoop
    from .input_kbd import KeyboardSession, KeyState

    if not sys.stdin.isatty():
        # fail before we grab the car, not after
        sys.exit("keyboard control needs a real terminal (run it from a shell)")

    address = await resolve(args)
    state = KeyState(mode=args.mode, hold_ttl=args.hold_ttl)
    state.turbo = args.turbo
    state.lights = args.lights
    state.speed = SPEED_TURBO if args.turbo else SPEED_NORMAL

    help_text = (
        "W/A/S/D or arrows  move   SPACE  stop   L  lights   T  turbo   +/- speed   Q  quit"
    )
    async with QCar(address, adapter=args.adapter) as car:
        print(f"{GREEN}connected {address}{RESET}  key mode: {args.mode}")
        print(DIM + help_text + RESET)
        with KeyboardSession(state):
            loop = DriveLoop(
                car,
                state.command,
                interval=args.interval,
                status=lambda: state.status,
                render=_render(car),
                should_quit=lambda: state.quit,
            )
            try:
                await loop.run()
            finally:
                await finish(car, loop)


async def cmd_gamepad(args) -> None:
    from .drive import DriveLoop
    from .input_gamepad import HAVE_EVDEV, GamepadSession, GamepadState, list_gamepads

    if not HAVE_EVDEV:
        sys.exit("gamepad support needs `pip install evdev`")
    if args.list:
        for path, name in list_gamepads():
            print(f"{path}  {name}")
        return

    address = await resolve(args)
    state = GamepadState(deadzone=args.deadzone)
    async with QCar(address, adapter=args.adapter) as car:
        with GamepadSession(state, args.device) as pad:
            print(f"{GREEN}connected {address}{RESET}  pad: {pad.path}")
            loop = DriveLoop(
                car,
                state.command,
                interval=args.interval,
                status=lambda: state.status,
                render=_render(car),
                should_quit=lambda: state.quit,
            )
            try:
                await loop.run()
            finally:
                await finish(car, loop)


def lan_ips() -> list[str]:
    """Best-effort list of addresses a phone on the same wifi could reach."""
    import socket

    out: list[str] = []
    try:  # ask the kernel which interface it would use to reach the internet
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        out.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in out and not ip.startswith("127."):
                out.append(ip)
    except OSError:
        pass
    return out


def ascii_qr(text: str) -> str | None:
    """QR code if the `qrcode` package happens to be installed, else nothing."""
    try:
        import qrcode  # type: ignore
    except ImportError:
        return None
    import io

    buf = io.StringIO()
    qr = qrcode.QRCode(border=1)
    qr.add_data(text)
    qr.make(fit=True)
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()


def cmd_web(args) -> None:
    """Serve the phone controller; blocks until Ctrl-C."""
    from .gui.backend import CarBackend
    from .web.server import serve

    garage = _garage(args)
    backend = CarBackend(adapter=args.adapter, interval=args.interval)
    backend.start()
    web = serve(backend, garage, host=args.host, port=args.port, stale=args.stale,
                verbose=args.verbose)
    port = web.port
    urls = [f"http://{ip}:{port}" for ip in (lan_ips() or ["127.0.0.1"])]
    # flush so the URL is readable immediately, even when piped or backgrounded
    print(f"{APP_NAME} web controller on {args.host}:{port}", flush=True)
    print("open one of these on your phone (same wifi):", flush=True)
    for u in urls:
        print("   ", u, flush=True)
    qr = ascii_qr(urls[0]) if args.qr else None
    if qr:
        print(qr, flush=True)
    elif args.qr:
        print("(pip install qrcode to get a QR code)", flush=True)
    print(f"{len(garage)} saved car(s). Ctrl-C to stop.\n", flush=True)
    backend.scan(5.0)
    if args.car:
        want = args.car.lower()
        prof = garage.by_id(args.car) or next(
            (p for p in garage if p.name.lower() == want or p.address.lower() == args.car.lower()),
            None,
        )
        if prof is None:
            print(f"no saved car matching {args.car!r}; connect it from the page instead")
        else:
            backend.connect(prof.address)
            print(f"connecting to {prof.name} ({prof.address}) ...")
    last = None
    try:
        while True:
            time.sleep(0.5)
            if backend.state != last:
                last = backend.state
                tel = backend.telemetry
                extra = f" battery {tel.battery}%" if tel else ""
                print(f"  state={backend.state}{extra} tx={backend.tx} "
                      f"errors={backend.errors} live={web.controller.live}", flush=True)
    except KeyboardInterrupt:
        print("\nstopping - leaving the car stopped")
    finally:
        try:
            backend.set_intent(Command())
        finally:
            web.shutdown()
            backend.stop()


def cmd_gui(args) -> None:
    """Blocking: the GUI owns the main thread and runs its own loop."""
    from .gui.app import App

    garage = Garage.load(Path(args.config) if args.config else None)
    app = App(
        garage=garage,
        adapter=args.adapter,
        size=(args.width, args.height),
        fullscreen=args.fullscreen,
        video_driver=args.video_driver,
        interval=args.interval,
        software=args.software,
    )
    app.run(
        frames=args.frames,
        screenshot=args.screenshot,
        run_seconds=args.run_seconds,
    )


# --------------------------------------------------------------------------
# profiles (no radio needed)
# --------------------------------------------------------------------------


def _garage(args) -> Garage:
    return Garage.load(Path(args.config) if getattr(args, "config", None) else None)


def cmd_profile(args) -> None:

    g = _garage(args)
    action = args.profile_cmd
    if action == "list":
        if not len(g):
            print("no saved cars")
            return
        for p in g.sorted():
            print(f"{p.id}  {p.name:<26} {p.address}  cap {p.speed_cap:>3}%  "
                  f"{p.odometer:>9}  batt {p.last_battery if p.last_battery is not None else '-'}")
        print(f"\n{len(g)} car(s) in {g.path}")
        return
    if action == "add":
        p = g.add(args.name, args.address, speed_cap=args.cap, turbo_default=args.turbo,
                  lights_default=args.lights, notes=args.notes or "")
        if args.color:
            p.accent = args.color if args.color.startswith("#") else "#" + args.color
        print(f"saved {p}")
        print(f"-> {g.save()}")
        return
    if action == "remove":
        prof = g.by_id(args.which) or next(
            (p for p in g if p.name.lower() == args.which.lower()), None
        )
        if prof is None:
            sys.exit(f"no car named/id {args.which!r}")
        g.remove(prof.id)
        print(f"removed {prof.name}")
        g.save()
        return
    if action == "path":
        print(g.path)
        return
    sys.exit(f"unknown profile command {action!r}")


async def cmd_probe(args) -> None:
    from .probe import run_probe

    address = await resolve(args)
    async with QCar(address, adapter=args.adapter) as car:
        await run_probe(car, which=args.which, countdown=not args.fast)


async def cmd_decode(args) -> None:
    print(f"key {AES_KEY.hex()}")
    for frame in args.frames:
        raw = bytes.fromhex(frame.replace(":", "").replace(" ", ""))
        if len(raw) != 16:
            print(f"{frame}: {len(raw)} bytes, not a 16 byte block")
            continue
        tel = parse_telemetry(raw)
        if tel:
            print(f"{frame} -> telemetry {tel}")
        else:
            print(f"{frame} -> {decode_command_frame(raw)}")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qcarctl",
        description="Open Bluetooth controller for Brandbase QCAR / Shell Racing toy cars.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, scan=True):
        sp.add_argument("address", nargs="?", help="MAC of the car (omit to auto-select)")
        sp.add_argument("--adapter", help="hci device, e.g. hci0")
        if scan:
            sp.add_argument("--scan-timeout", type=float, default=8.0)
            sp.add_argument("--yes", "-y", action="store_true", help="take the first car found")

    sp = sub.add_parser("scan", help="list QCAR cars in range")
    sp.add_argument("--scan-timeout", type=float, default=8.0)
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("monitor", help="connect and print decrypted telemetry")
    common(sp)
    sp.set_defaults(func=cmd_monitor)

    sp = sub.add_parser("send", help="send a single command (or a timed burst)")
    common(sp)
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--forward", "-f", action="store_true")
    g.add_argument("--backward", "-b", action="store_true")
    sp.add_argument("--left", "-l", action="store_true")
    sp.add_argument("--right", "-r", action="store_true")
    sp.add_argument("--lights", action="store_true")
    sp.add_argument("--turbo", action="store_true", help="speed 100 (same as --speed 100)")
    sp.add_argument("--speed", type=int, default=SPEED_NORMAL, help="0-100")
    sp.add_argument(
        "--for", dest="for_", type=float, default=0.0, help="drive N seconds, then stop"
    )
    sp.add_argument(
        "--interval", type=float, default=0.05, help="seconds between frames in a burst"
    )
    sp.add_argument(
        "--no-ack",
        action="store_true",
        help="use unacknowledged Write Commands (the car IGNORES these - debugging only)",
    )
    sp.add_argument("--dry-run", action="store_true", help="print the frame, do not send")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("drive", help="interactive keyboard driving")
    common(sp)
    sp.add_argument("--mode", choices=("toggle", "hold"), default="toggle")
    sp.add_argument("--hold-ttl", type=float, default=0.55)
    sp.add_argument(
        "--interval",
        type=float,
        default=0.08,
        help="seconds between frames (each write is ACKed, ~45 ms round trip)",
    )
    sp.add_argument("--lights", action="store_true")
    sp.add_argument("--turbo", action="store_true")
    sp.set_defaults(func=cmd_drive)

    sp = sub.add_parser("gamepad", help="drive with a joystick/gamepad (needs evdev)")
    common(sp)
    sp.add_argument("--device", help="/dev/input/eventN")
    sp.add_argument("--list", action="store_true", help="list candidate pads and exit")
    sp.add_argument("--deadzone", type=float, default=0.15)
    sp.add_argument("--interval", type=float, default=0.08)
    sp.set_defaults(func=cmd_gamepad)

    sp = sub.add_parser("web", help="serve a touch controller for phones over the LAN")
    sp.add_argument("--host", default="0.0.0.0", help="0.0.0.0 lets phones connect")
    sp.add_argument("--port", type=int, default=8753)
    sp.add_argument("--adapter")
    sp.add_argument("--config", help="profiles.json to use instead of the default")
    sp.add_argument("--interval", type=float, default=0.08)
    sp.add_argument(
        "--stale",
        type=float,
        default=0.5,
        help="seconds without a control POST before the car is forced to STOP",
    )
    sp.add_argument("--car", help="name or MAC of a saved car to connect to immediately")
    sp.add_argument("--qr", action="store_true", help="print a QR code of the URL")
    sp.set_defaults(func=cmd_web)

    sp = sub.add_parser("gui", help="SDL2 garage + touch drive pad")
    sp.add_argument("--width", type=int, default=1024)
    sp.add_argument("--height", type=int, default=640)
    sp.add_argument("--fullscreen", "-f", action="store_true", help="kiosk / phone / tablet")
    sp.add_argument("--adapter")
    sp.add_argument("--config", help="profiles.json to use instead of the default")
    sp.add_argument("--interval", type=float, default=0.08)
    sp.add_argument(
        "--video-driver",
        help="force SDL_VIDEODRIVER (x11, wayland, offscreen, kmsdrm)",
    )
    sp.add_argument(
        "--software",
        action="store_true",
        help="SDL software renderer - use this if you get a GLX / X Error crash",
    )
    sp.add_argument("--frames", type=int, help="exit after N frames (testing)")
    sp.add_argument("--run-seconds", type=float, default=0.0, help="exit after N seconds")
    sp.add_argument("--screenshot", help="write a PNG of the rendered frame")
    sp.set_defaults(func=cmd_gui)

    sp = sub.add_parser("profile", help="manage saved cars")
    sp.add_argument("--config", help="profiles.json to use instead of the default")
    psub = sp.add_subparsers(dest="profile_cmd", required=True)
    psub.add_parser("list", help="show saved cars")
    psub.add_parser("path", help="print the store location")
    pa = psub.add_parser("add", help="add or update a car")
    pa.add_argument("name")
    pa.add_argument("address")
    pa.add_argument("--cap", type=int, default=80, help="max throttle percent")
    pa.add_argument("--turbo", action="store_true")
    pa.add_argument("--lights", action="store_true")
    pa.add_argument("--color", help="accent, e.g. ffb300")
    pa.add_argument("--notes")
    pr = psub.add_parser("remove", help="delete a car by id or name")
    pr.add_argument("which")
    sp.set_defaults(func=cmd_profile)

    sp = sub.add_parser("probe", help="scripted field-by-field hardware probe")
    common(sp)
    sp.add_argument(
        "--which",
        choices=("all", "motion", "lights", "speed", "aux"),
        default="all",
    )
    sp.add_argument("--fast", action="store_true", help="skip countdowns")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("decode", help="decrypt raw 16 byte frames")
    sp.add_argument("frames", nargs="+")
    sp.set_defaults(func=cmd_decode)

    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        if asyncio.iscoroutinefunction(args.func):
            asyncio.run(args.func(args))
        else:
            args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted - car left stopped")
    except ConnectionError as exc:
        sys.exit(f"connection failed: {exc}")
    except BleakError as exc:
        # a blipping link or a cranky GATT server should not be a traceback
        sys.exit(
            f"bluetooth error: {exc}\n"
            "the car stops on its own when the frame stream pauses, so it is "
            "safe - just power-cycle it and retry"
        )


if __name__ == "__main__":
    main()
