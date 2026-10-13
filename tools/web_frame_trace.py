"""Trace exactly which AES frames reach the car while a browser holds forward.

Answers two questions with numbers instead of guesses:

  * does a held control keep producing frames, or just one?
  * how long does each ACKed write take (the link is strictly sequential, so
    frame rate == 1 / write latency)?

Run: .venv/bin/python tools/web_frame_trace.py [seconds]
"""

import asyncio
import json
import statistics
import sys
import threading
import time
import urllib.request

sys.path.insert(0, ".")

from qcar import car as carmod  # noqa: E402
from qcar.gui.backend import CarBackend  # noqa: E402
from qcar.profiles import Garage  # noqa: E402
from qcar.web.server import WebControl  # noqa: E402

ADDR = "00:3C:DE:07:01:A4"
PORT = 8811
SECS = float(sys.argv[1] if len(sys.argv) > 1 else 3.0)

# ---- record every frame the backend writes to the car -----------------
frames: list[tuple[float, bytes]] = []
latencies: list[float] = []
_orig_write = carmod.QCar.write


async def traced_write(self, cmd, response=True):
    plain = cmd.to_plaintext()
    t0 = time.monotonic()
    try:
        return await _orig_write(self, cmd, response=response)
    finally:
        dt = time.monotonic() - t0
        frames.append((t0, plain))
        latencies.append(dt)


carmod.QCar.write = traced_write


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


def main():
    garage = Garage()
    backend = CarBackend(interval=0.08)
    backend.start()
    web = WebControl(backend, garage, host="127.0.0.1", port=PORT, stale=0.5).start()

    print("connecting ...")
    post("/api/connect", {"address": ADDR})
    time.sleep(4)
    print(f"  state={backend.state}\n")

    frames.clear()
    latencies.clear()

    stop = threading.Event()

    def browser():
        """Exactly what index.html does while you hold the pad: 10 Hz intent."""
        while not stop.is_set():
            try:
                post("/api/intent", {"forward": True, "speed": 60})
            except Exception as exc:  # noqa: BLE001
                print("  POST failed:", exc)
            time.sleep(0.1)

    th = threading.Thread(target=browser, daemon=True)
    th.start()
    time.sleep(SECS)
    stop.set()
    th.join(timeout=2)

    fwd = sum(1 for _, p in frames if p[4] and not p[5])
    stop_frames = sum(1 for _, p in frames if not p[4] and not p[5])
    back = sum(1 for _, p in frames if p[5] and not p[4])
    print(f"browser held FORWARD for {SECS}s at 10 Hz")
    print(f"  frames written to car : {len(frames)}  ({len(frames)/SECS:.1f}/s)")
    print(f"  forward frames        : {fwd}")
    print(f"  STOP frames           : {stop_frames}")
    print(f"  backward frames       : {back}")
    if latencies:
        s = sorted(latencies)
        print(f"  write latency         : min {s[0]*1000:.0f} ms  "
              f"median {statistics.median(s)*1000:.0f} ms  "
              f"p90 {s[int(len(s)*0.9)]*1000:.0f} ms  max {s[-1]*1000:.0f} ms")
        print(f"  -> theoretical max    : {1000/statistics.median(s):.1f} frames/s")
    gaps = [b[0] - a[0] for a, b in zip(frames, frames[1:])]
    if gaps:
        g = sorted(gaps)
        print(f"  gap between frames    : median {statistics.median(g)*1000:.0f} ms  "
              f"max {g[-1]*1000:.0f} ms  (a long gap = the car coasting into its own failsafe)")
    print("\nfirst 12 frames (offset 4=fwd 5=rev 9=speed):")
    for t, p in frames[:12]:
        print(f"   {p.hex()}  fwd={p[4]} rev={p[5]} spd={p[9]}")

    post("/api/stop")
    time.sleep(0.5)
    backend.set_intent(__import__("qcar.protocol", fromlist=["Command"]).Command())
    web.shutdown()
    backend.stop()
    time.sleep(0.5)
    asyncio.set_event_loop(asyncio.new_event_loop())


main()
