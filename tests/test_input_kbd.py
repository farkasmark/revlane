"""Keyboard mapping logic, testable without a TTY."""

import pytest

from qcar.input_kbd import CSI, KeyState, _parse


def test_escape_sequences():
    # arrows are decoded to their WASD equivalent
    assert _parse(b"\x1b[A") == ["w"]
    assert _parse(b"\x1b[D") == ["a"]
    assert _parse(b"w") == ["w"]
    assert _parse(b"ws") == ["w", "s"]
    assert _parse(b"\x1bOA") == ["\x1b", "O", "A"]  # SS3 not handled, but safe


def test_toggle_mode():
    # repeat_block=0 so a scripted second press counts as a deliberate tap
    st = KeyState(mode="toggle", repeat_block=0.0)
    st.handle("w")
    assert st.command().forward
    st.handle("w")
    assert not st.command().forward


def test_autorepeat_is_ignored_in_toggle_mode():
    st = KeyState(mode="toggle", repeat_block=10.0)
    st.handle("w")
    st.handle("w")  # keyboard auto-repeat, must not cancel
    assert st.command().forward


def test_forward_wins_over_backward():
    st = KeyState(mode="toggle")
    st.handle("w")
    st.handle("s")
    cmd = st.command()
    assert cmd.backward and not cmd.forward  # last press wins


def test_arrows_and_letters_share_state():
    st = KeyState(mode="toggle", repeat_block=0.0)
    st.handle("w")
    for key in _parse(b"\x1b[A"):  # up arrow == 'w' == same toggle
        st.handle(key)
    assert not st.command().forward


def test_space_clears_everything():
    st = KeyState(mode="toggle")
    for k in "wad":
        st.handle(k)
    st.handle(" ")
    cmd = st.command()
    assert not (cmd.forward or cmd.backward or cmd.left or cmd.right)


def test_lights_and_turbo_toggles():
    st = KeyState(mode="toggle")
    st.handle("l")
    assert st.command().lights
    st.handle("t")
    assert st.command().speed == 100
    st.handle("t")
    assert st.command().speed == 80


def test_speed_step():
    st = KeyState(mode="toggle")
    st.handle("+")
    st.handle("+")
    assert st.command().speed == 90
    for _ in range(20):
        st.handle("-")
    assert st.command().speed == 10


def test_hold_mode_expires():
    st = KeyState(mode="hold", hold_ttl=0.0)
    st.handle("w")
    # hold_ttl of 0 means it is already stale by the time we read it
    assert not st.command().forward


def test_hold_mode_alive_while_repeating():
    st = KeyState(mode="hold", hold_ttl=5.0, repeat_block=0.0)
    st.handle("w")
    assert st.command().forward


def test_quit_keys():
    st = KeyState()
    st.handle("q")
    assert st.quit


def test_csi_table():
    assert CSI == {"A": "w", "B": "s", "C": "d", "D": "a"}


def test_arrow_drives_the_same_flag_as_letter():
    st = KeyState(mode="toggle")
    for key in _parse(b"\x1b[A"):
        st.handle(key)
    assert st.command().forward


# --------------------------------------------------------------------------
# auto-repeat timing (regression: repeat suppression used to skip the hold
# timestamp refresh, killing held keys after one repeat_block)

class _Clock:
    """Scriptable stand-in for time.monotonic() inside qcar.input_kbd."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def monotonic(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture()
def clock(monkeypatch):
    # start at 1.0 like a real monotonic clock (uptime): at exactly 0.0 a
    # first keypress looks like a repeat (prev defaults to 0.0)
    c = _Clock(1.0)
    monkeypatch.setattr("qcar.input_kbd.time", c)
    return c


def test_hold_survives_realistic_repeat_beyond_ttl(clock):
    # a real keyboard repeats ~30 times/s after a ~0.5 s initial delay;
    # the whole point of hold mode is that this keeps the key alive
    st = KeyState(mode="hold", hold_ttl=0.55, repeat_block=0.12)
    st.handle("w")  # press at t=0
    assert st.command().forward
    clock.advance(0.5)  # initial repeat delay
    for _ in range(37):  # 1.5 s of 40 ms repeats -> 2.0 s total, >> hold_ttl
        clock.advance(0.04)
        st.handle("w")
        assert st.command().forward, f"held key died at t={clock.t:.2f}"


def test_hold_expires_when_repeats_stop(clock):
    st = KeyState(mode="hold", hold_ttl=0.55, repeat_block=0.12)
    st.handle("w")
    clock.advance(0.5)
    for _ in range(25):  # hold for a second via repeats
        clock.advance(0.04)
        st.handle("w")
    assert st.command().forward
    clock.advance(0.6)  # ...then the user lets go: no more repeats
    assert not st.command().forward, "hold must expire once repeats stop"
    st.handle("w")  # a fresh press revives it
    assert st.command().forward


def test_hold_refreshes_on_arrow_repeats_too(clock):
    st = KeyState(mode="hold", hold_ttl=0.55, repeat_block=0.12)
    for key in _parse(b"\x1b[A"):  # arrow press
        st.handle(key)
    clock.advance(0.5)
    for _ in range(25):
        clock.advance(0.04)
        st.handle("w")  # same flag, letter repeat
    assert st.command().forward


def test_toggle_mode_ignores_long_autorepeat_burst(clock):
    st = KeyState(mode="toggle", repeat_block=0.12)
    st.handle("w")  # press: on
    clock.advance(0.04)
    for _ in range(25):  # key held for a second, repeats keep coming
        clock.advance(0.04)
        st.handle("w")
        assert st.command().forward, "repeat leaked through: toggled off"
    clock.advance(0.2)  # past repeat_block: this is a genuine new press
    st.handle("w")
    assert not st.command().forward


def test_hold_opposite_exclusion_survives_repeats(clock):
    st = KeyState(mode="hold", hold_ttl=0.55, repeat_block=0.12)
    st.handle("w")
    clock.advance(0.4)
    st.handle("w")  # keep forward alive
    st.handle("s")  # press backward: excludes forward
    assert st.command().backward and not st.command().forward
    clock.advance(0.4)
    for _ in range(10):  # only 's' keeps repeating now
        clock.advance(0.04)
        st.handle("s")
    assert st.command().backward, "held 's' must stay alive via repeats"
    assert not st.command().forward, "'w' was never released-and-repressed"
    clock.advance(0.6)  # repeats stop entirely
    assert not st.command().backward and not st.command().forward
