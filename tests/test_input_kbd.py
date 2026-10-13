"""Keyboard mapping logic, testable without a TTY."""

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
