"""Web controller tests: cap enforcement, the watchdog, and profile CRUD.

A real HTTP server on an ephemeral port and a fake radio - the same assertions
hold against a phone.
"""

import json
import math
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import ADDR, FOUND, FakeBackend

from qcar.profiles import Garage
from qcar.protocol import Command
from qcar.web.server import WebControl


@pytest.fixture
def web(tmp_path):
    made = []

    def _make(fleet=None, stale=0.5, port=0):
        garage = Garage(tmp_path / "profiles.json")
        for name, addr in fleet or []:
            garage.add(name, addr)
        backend = FakeBackend(FOUND)
        ctl = WebControl(backend, garage, host="127.0.0.1", port=port, stale=stale).start()
        made.append(ctl)
        return ctl, backend, garage

    yield _make
    for c in made:
        c.shutdown()


def get(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post(port, path, body=None, raw=None):
    data = raw if raw is not None else json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---------------------------------------------------------------- page


def test_index_is_served(web):
    ctl, _b, _g = web()
    code, body = get(ctl.port, "/")
    assert code == 200
    html = body.decode()
    assert "revlane" in html
    assert "/api/intent" in html and "/api/state" in html


def test_unknown_paths_404(web):
    ctl, _b, _g = web()
    code, _ = get(ctl.port, "/nope.txt")
    assert code == 404
    code, _ = post(ctl.port, "/api/nope", {})
    assert code == 404


# ---------------------------------------------------------------- state


def test_state_shape(web):
    ctl, _b, _g = web()
    code, raw = get(ctl.port, "/api/state")
    st = json.loads(raw)
    assert code == 200
    for key in ("state", "profiles", "nearby", "intent", "plain", "cap", "live", "tx"):
        assert key in st
    assert st["nearby"][0]["address"] == ADDR
    assert st["live"] is False, "nobody has driven yet"


def test_scan_then_connect(web):
    ctl, backend, _g = web()
    post(ctl.port, "/api/scan", {})
    assert backend.scans >= 1
    code, res = post(ctl.port, "/api/connect", {"address": ADDR.lower()})
    assert code == 200 and res["address"] == ADDR
    assert backend.connects == [ADDR]
    _code, raw = get(ctl.port, "/api/state")
    assert json.loads(raw)["state"] == "connected"


def test_connect_requires_address(web):
    ctl, _b, _g = web()
    code, _ = post(ctl.port, "/api/connect", {})
    assert code == 400


# ---------------------------------------------------------------- intent


def test_intent_marks_controller_live(web):
    ctl, backend, _g = web()
    post(ctl.port, "/api/intent", {"forward": True, "speed": 70})
    assert backend.intent().forward is True
    assert ctl.controller.live is True
    assert backend.intent().speed == 70


def test_default_cap_is_applied_when_a_car_is_saved(web):
    """The page asks for 100; the saved profile says otherwise."""
    ctl, backend, _g = web(fleet=[("Capped", ADDR)])
    post(ctl.port, "/api/intent", {"forward": True, "speed": 100})
    assert backend.intent().speed == 80, "the page must not be able to exceed the cap"


def test_cap_follows_the_connected_car(web):
    ctl, backend, garage = web()
    p = garage.add("Slow poke", ADDR)
    p.speed_cap = 30
    garage.save()
    post(ctl.port, "/api/connect", {"address": ADDR})
    post(ctl.port, "/api/intent", {"forward": True, "speed": 100})
    assert backend.intent().speed == 30


def test_garbage_speed_is_not_fatal(web):
    ctl, backend, _g = web()
    code, _ = post(ctl.port, "/api/intent", {"forward": True, "speed": "lots"})
    assert code == 200
    assert backend.intent().speed <= 80


def test_malformed_body_is_not_fatal(web):
    ctl, _b, _g = web()
    code, _ = post(ctl.port, "/api/intent", raw=b"{not json at all")
    assert code == 200


# ---------------------------------------------------------------- watchdog


def test_watchdog_stops_a_dead_controller(web):
    ctl, backend, _g = web(stale=0.2)
    post(ctl.port, "/api/intent", {"forward": True})
    assert backend.intent().forward is True
    time.sleep(0.5)  # the phone died mid-drive
    assert ctl.controller.live is False
    assert backend.intent() == Command(), "silence must mean stopped, not 'keep going'"


def test_heartbeat_keeps_the_intent(web):
    ctl, backend, _g = web(stale=0.4)
    post(ctl.port, "/api/intent", {"forward": True})
    for _ in range(6):
        time.sleep(0.08)
        post(ctl.port, "/api/heartbeat", {})
    assert backend.intent().forward is True, "an alive page must not be fight-stoppped"


def test_watchdog_does_not_claim_to_be_alive(web):
    """Regression: the watchdog used to refresh its own heartbeat, so `live`
    stayed True forever after the first trip and the page lied about it."""
    ctl, backend, _g = web(stale=0.2)
    post(ctl.port, "/api/intent", {"forward": True})
    time.sleep(0.45)
    for _ in range(3):
        _code, raw = get(ctl.port, "/api/state")
        assert json.loads(raw)["live"] is False
        time.sleep(0.05)
    # a fresh command must bring it back to life
    post(ctl.port, "/api/intent", {"forward": True})
    _code, raw = get(ctl.port, "/api/state")
    st = json.loads(raw)
    assert st["live"] is True and st["intent"]["forward"] is True


def test_explicit_stop_clears(web):
    ctl, backend, _g = web()
    post(ctl.port, "/api/intent", {"forward": True, "left": True, "lights": True})
    post(ctl.port, "/api/stop")
    assert backend.intent() == Command()


# ---------------------------------------------------------------- profiles


def test_profile_save_and_delete_roundtrip(web, tmp_path):
    ctl, _b, garage = web()
    code, res = post(ctl.port, "/api/profile/save", {
        "address": ADDR, "name": "Marci's BMW M3", "cap": 55,
        "accent": "#1e88e5", "turbo": True, "notes": "home car",
    })
    assert code == 200 and res["ok"]
    assert garage.find(ADDR).speed_cap == 55
    assert "BMW M3" in json.loads(tmp_path.joinpath("profiles.json").read_text())["cars"][0]["name"]

    # a second save with the same address updates rather than duplicates
    post(ctl.port, "/api/profile/save", {"address": ADDR, "name": "Renamed", "cap": 40})
    assert len(garage) == 1
    assert garage.find(ADDR).name == "Renamed"
    pid = garage.find(ADDR).id
    post(ctl.port, "/api/profile/delete", {"id": pid})
    assert len(garage) == 0


def test_profile_cap_is_clamped(web):
    ctl, _b, garage = web()
    post(ctl.port, "/api/profile/save", {"address": ADDR, "name": "Fast", "cap": 9999})
    assert garage.find(ADDR).speed_cap == 100
    post(ctl.port, "/api/profile/save", {"address": ADDR, "name": "Slow", "cap": -20})
    assert garage.find(ADDR).speed_cap == 10


def test_profile_save_requires_address(web):
    ctl, _b, _g = web()
    code, res = post(ctl.port, "/api/profile/save", {"name": "Ghost car"})
    assert res["ok"] is False


# ------------------------------------------------------- pad direction maths
#
# The pad's sign convention has already been wrong once (negating dy when
# reading the pointer and again when deriving the direction cancelled out, so
# "up" sent reverse). Node is available in dev, so the maths is checked
# directly instead of by staring at signs.

PAD_JS = Path(__file__).resolve().parent.parent / "qcar" / "web" / "pad.js"
INDEX_HTML = Path(__file__).resolve().parent.parent / "qcar" / "web" / "index.html"
node = shutil.which("node")
needs_node = pytest.mark.skipif(node is None, reason="node not installed")


def node_eval(script: str) -> dict:
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def js(expr: str) -> dict:
    return node_eval(f"const p=require({str(PAD_JS)!r}); console.log(JSON.stringify({expr}));")


@needs_node
def test_finger_above_centre_is_forward():
    """dy is screen space: positive is DOWN, so up must mean forward."""
    up = js("p.axisFlags(0, -1)")
    assert up == {"forward": True, "backward": False, "left": False, "right": False}


@needs_node
def test_finger_below_centre_is_backward():
    down = js("p.axisFlags(0, 1)")
    assert down == {"forward": False, "backward": True, "left": False, "right": False}


@needs_node
def test_steering_and_deadzone():
    assert js("p.axisFlags(-1, 0)")["left"] is True
    assert js("p.axisFlags(1, 0)")["right"] is True
    assert js("p.axisFlags(0.1, 0.1)") == {
        "forward": False, "backward": False, "left": False, "right": False,
    }, "a thumb resting near the centre must not drive"
    diag = js("p.axisFlags(-0.9, -0.9)")
    assert diag == {"forward": True, "backward": False, "left": True, "right": False}
    # forward and backward can never both be set
    for dx in (-1, -0.3, 0, 0.3, 1):
        for dy in (-1, -0.3, 0, 0.3, 1):
            f = js(f"p.axisFlags({dx}, {dy})")
            assert not (f["forward"] and f["backward"])
            assert not (f["left"] and f["right"])


@needs_node
def test_keyboard_and_pad_either_source_drives():
    merged = js("p.mergeFlags(p.axisFlags(0,-1), {a:true})")
    assert merged["forward"] is True and merged["left"] is True
    assert js("p.mergeFlags(p.axisFlags(0,0), {})")["forward"] is False


@needs_node
def test_vector_is_clamped_to_the_pad():
    v = node_eval(
        f"const p=require({str(PAD_JS)!r});"
        "const v=p.clampVec(3,4); console.log(JSON.stringify({x:v[0],y:v[1]}));"
    )
    assert math.isclose(math.hypot(v["x"], v["y"]), 1.0, rel_tol=1e-9)
    # a vector already inside the pad is left alone
    small = node_eval(
        f"const p=require({str(PAD_JS)!r});"
        "const v=p.clampVec(0.2,-0.1); console.log(JSON.stringify({x:v[0],y:v[1]}));"
    )
    assert small == {"x": 0.2, "y": -0.1}


def test_index_uses_the_shared_module_not_inline_math():
    """Guards against the old inline, double-negated version coming back."""
    html = INDEX_HTML.read_text()
    assert 'src="/pad.js"' in html
    assert "-p.y>dz" not in html
    assert "QPAD.axisFlags" in html


def test_static_js_is_served(web):
    ctl, _b, _g = web()
    code, body = get(ctl.port, "/pad.js")
    assert code == 200
    assert b"axisFlags" in body


def test_only_allowlisted_files_are_served(web):
    ctl, _b, _g = web()
    for path in ("/../pyproject.toml", "/%2e%2e/pyproject.toml", "/server.py", "/pad.js/../index.html"):
        code, _ = get(ctl.port, path)
        assert code == 404, f"{path} must not be reachable"
