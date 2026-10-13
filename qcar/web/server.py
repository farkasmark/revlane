"""LAN web controller: the phone is a touch pad, this box holds the radio.

Why plain HTTP instead of WebSockets: ``CarBackend`` is already thread-safe and
poll-driven, so an ``http.server`` worker thread can drive it directly - no
event-loop bridging, no reconnect logic, no JS websocket polyfill, and it works
through any browser on any OS including iOS. A LAN round trip is a couple of
milliseconds and the car only needs ~12 Hz, so polling at 10 Hz is invisible.

Safety is enforced *server side*, never trusted to the client:

  * every control POST must arrive within ``stale`` seconds of the previous one,
    otherwise the monitor thread pushes STOP - a locked screen, a closed tab, a
    dead wifi NIC or a killed browser all stop the car
  * the speed cap comes from the saved profile and is applied here, after
    parsing, so a tampered page cannot exceed it
  * only one control source at a time; the desktop GUI and the web page will
    fight over ``set_intent`` if you run both
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..profiles import Garage, Profile
from ..protocol import SPEED_MAX, SPEED_NORMAL, Command

INDEX = Path(__file__).with_name("index.html")
DEFAULT_PORT = 8753
DEFAULT_CAP = 80
# only these files next to this module are ever served - no path traversal
STATIC = {"/pad.js": ("pad.js", "text/javascript; charset=utf-8")}


@dataclasses.dataclass
class ClientInfo:
    id: str
    label: str
    last_seen: float


class Controller:
    """Everything the HTTP layer needs, with no pygame and no asyncio."""

    def __init__(
        self,
        backend,
        garage: Garage,
        *,
        stale: float = 0.5,
        on_log=lambda msg: None,
    ) -> None:
        self.backend = backend
        self.garage = garage
        self.stale = stale
        self.on_log = on_log
        self._lock = threading.Lock()
        self._last_input = 0.0
        self._intent = Command()
        self._stopping = False
        self.started = time.monotonic()

    # ---- input --------------------------------------------------------

    def touch(self) -> None:
        self._last_input = time.monotonic()

    @property
    def last_input(self) -> float:
        return self._last_input

    @property
    def live(self) -> bool:
        return (time.monotonic() - self._last_input) < self.stale

    def effective_cap(self) -> int:
        prof = self.profile()
        return int(prof.speed_cap if prof else DEFAULT_CAP)

    def profile(self) -> Profile | None:
        addr = getattr(self.backend, "address", None)
        return self.garage.find(addr) if addr else None

    def set_intent(self, payload: dict) -> Command:
        cap = self.effective_cap()
        try:
            speed = int(payload.get("speed", SPEED_NORMAL))
        except (TypeError, ValueError):
            speed = SPEED_NORMAL
        cmd = Command(
            forward=bool(payload.get("forward")),
            backward=bool(payload.get("backward")),
            left=bool(payload.get("left")),
            right=bool(payload.get("right")),
            lights=bool(payload.get("lights")),
            speed=max(0, min(cap, speed)),
        )
        with self._lock:
            self._intent = cmd
            self._stopping = False
        self.touch()
        self.backend.set_intent(cmd)
        return cmd

    def stop(self, *, heartbeat: bool = True) -> Command:
        """Park the car.

        ``heartbeat`` should only be False when *we* decided to stop (the
        watchdog): refreshing the clock there would make the controller look
        alive to itself and to the page forever after the first trip.
        """
        with self._lock:
            self._intent = Command()
            self._stopping = True
        if heartbeat:
            self.touch()
        self.backend.set_intent(Command())
        return self._intent

    def intent(self) -> Command:
        with self._lock:
            return self._intent

    def sweep(self) -> bool:
        """Called regularly; forces STOP when the controller has gone quiet.

        Returns True if it had to intervene.
        """
        if self.live or self._stopping:
            return False
        # push STOP once; CarBackend keeps streaming the current intent, so the
        # car stays stopped until a real command arrives
        self.stop(heartbeat=False)
        self.on_log("control stream stalled -> STOP")
        return True

    # ---- state for the page --------------------------------------------

    def state(self) -> dict[str, Any]:
        b = self.backend
        tel = getattr(b, "telemetry", None)
        prof = self.profile()
        intent = self.intent()
        return {
            "state": getattr(b, "state", "offline"),
            "address": getattr(b, "address", None),
            "car": None if prof is None else {"name": prof.name, "accent": prof.accent,
                                              "cap": prof.speed_cap, "id": prof.id},
            "telemetry": None if tel is None else {"seq": tel.seq, "battery": tel.battery},
            "tx": getattr(b, "tx", 0),
            "errors": getattr(b, "errors", 0),
            "rate": round(getattr(b, "fps_tx", 0.0), 1),
            "intent": {
                "forward": intent.forward,
                "backward": intent.backward,
                "left": intent.left,
                "right": intent.right,
                "lights": intent.lights,
                "speed": intent.speed,
            },
            "plain": intent.to_plaintext().hex(),
            "cap": self.effective_cap(),
            "live": self.live,
            "nearby": [
                {
                    "address": d.address,
                    "name": d.label,
                    "rssi": d.rssi,
                    "saved": self.garage.find(d.address) is not None,
                    "profile_name": (self.garage.find(d.address) or Profile("?", d.address)).name,
                }
                for d in getattr(b, "found", [])
            ],
            "profiles": [
                {
                    "id": p.id,
                    "name": p.name,
                    "address": p.address,
                    "accent": p.accent,
                    "cap": p.speed_cap,
                    "turbo": p.turbo_default,
                    "lights": p.lights_default,
                    "notes": p.notes,
                    "battery": p.last_battery,
                    "seen": p.last_seen_text,
                    "odometer": p.odometer,
                }
                for p in self.garage.sorted()
            ],
            "uptime": round(time.monotonic() - self.started, 1),
        }


class Handler(BaseHTTPRequestHandler):
    controller: Controller  # injected by serve()
    verbose = False

    # ---- plumbing -----------------------------------------------------

    def log_message(self, fmt, *args) -> None:  # noqa: A002
        if Handler.verbose:
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 64 * 1024:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode() or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # ---- routes -------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            except OSError as exc:
                self._json({"error": f"index.html missing: {exc}"}, 500)
            return
        if path == "/api/state":
            self._json(self.controller.state())
            return
        if path in STATIC:
            name, ctype = STATIC[path]
            try:
                self._send(200, Path(__file__).with_name(name).read_bytes(), ctype)
            except OSError as exc:
                self._json({"error": f"{name} missing: {exc}"}, 500)
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        c = self.controller
        path = self.path.split("?", 1)[0]
        body = self._body()
        try:
            if path == "/api/intent":
                cmd = c.set_intent(body)
                self._json({"ok": True, "speed": cmd.speed})
            elif path == "/api/stop":
                c.stop()
                self._json({"ok": True})
            elif path == "/api/heartbeat":
                c.touch()
                self._json({"ok": True, "live": True})
            elif path == "/api/scan":
                c.backend.scan(float(body.get("timeout", 4.0)))
                self._json({"ok": True})
            elif path == "/api/connect":
                addr = str(body.get("address", "")).strip().upper()
                if not addr:
                    self._json({"error": "address required"}, 400)
                    return
                c.backend.connect(addr)
                self._json({"ok": True, "address": addr})
            elif path == "/api/disconnect":
                c.stop()
                c.backend.disconnect()
                self._json({"ok": True})
            elif path == "/api/profile/save":
                self._json(self._save_profile(body))
            elif path == "/api/profile/delete":
                ok = c.garage.remove(str(body.get("id", "")))
                c.garage.save()
                self._json({"ok": ok})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001 - never take the server down
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _save_profile(self, body: dict) -> dict:
        c = self.controller
        address = str(body.get("address", "")).strip().upper()
        if not address:
            return {"ok": False, "error": "address required"}
        name = str(body.get("name", "")).strip() or c.garage.suggest_name(address)
        prof = c.garage.add(name, address)
        if body.get("accent"):
            prof.accent = str(body["accent"])
        try:
            prof.speed_cap = max(10, min(SPEED_MAX, int(body.get("cap", prof.speed_cap))))
        except (TypeError, ValueError):
            pass
        prof.turbo_default = bool(body.get("turbo", prof.turbo_default))
        prof.lights_default = bool(body.get("lights", prof.lights_default))
        if body.get("notes") is not None:
            prof.notes = str(body["notes"])[:280]
        c.garage.save()
        return {"ok": True, "id": prof.id, "name": prof.name}


class Monitor(threading.Thread):
    """Pushes STOP when the control stream goes quiet."""

    def __init__(self, controller: Controller, period: float = 0.05) -> None:
        super().__init__(daemon=True, name="qcar-web-watchdog")
        self.controller = controller
        self.period = period
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.controller.sweep()
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self.period)

    def halt(self) -> None:
        self._stop.set()


class WebControl:
    """The HTTP server plus its watchdog, as one lifecycled object."""

    def __init__(
        self,
        backend,
        garage: Garage,
        *,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        stale: float = 0.5,
        verbose: bool = False,
    ) -> None:
        self.controller = Controller(backend, garage, stale=stale)
        Handler.controller = self.controller
        Handler.verbose = verbose
        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.monitor = Monitor(self.controller)
        self.host = host
        self.port = self.httpd.server_address[1]

    def start(self) -> WebControl:
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True, name="qcar-web")
        self._thread.start()
        self.monitor.start()
        return self

    def shutdown(self) -> None:
        self.controller.stop()
        self.monitor.halt()
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=2.0)

    def __enter__(self) -> WebControl:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.shutdown()


def serve(
    backend,
    garage: Garage,
    *,
    host: str = "0.0.0.0",
    port: int = DEFAULT_PORT,
    stale: float = 0.5,
    verbose: bool = False,
) -> WebControl:
    """Start the web controller (already running); caller owns shutdown."""
    return WebControl(backend, garage, host=host, port=port, stale=stale, verbose=verbose).start()
