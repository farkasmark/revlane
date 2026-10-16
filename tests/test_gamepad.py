"""Gamepad input: evdev code-name alias handling.

evdev's event tables map a numeric code to one name *or a tuple of aliases*,
and the first alias is not always the label a mapping expects (0x134 is
``BTN_WEST`` first, ``BTN_Y`` second).  Matching therefore has to happen
against the whole alias set.

Regression: the code called nonexistent ``ecodes.ABS_by``/``ecodes.BTN_by``
helpers, so every event raised AttributeError (uncatched by the KeyError
handler) and no gamepad ever worked.
"""

import pytest

from qcar import input_gamepad as ig

HAVE_EVDEV = ig.HAVE_EVDEV


def test_single_name_code():
    assert ig._code_names({1: "ABS_X"}, 1) == {"ABS_X"}


def test_alias_tuple_code():
    assert ig._code_names({2: ("BTN_A", "BTN_SOUTH")}, 2) == {"BTN_A", "BTN_SOUTH"}


def test_missing_code_yields_no_names():
    assert ig._code_names({}, 99) == frozenset()


def test_bad_mapping_yields_no_names():
    assert ig._code_names(None, 1) == frozenset()


@pytest.mark.skipif(not HAVE_EVDEV, reason="evdev not installed")
def test_real_evdev_tables_expose_aliases():
    from evdev import ecodes

    assert "BTN_A" in ig._code_names(ecodes.BTN, 0x130)
    # the canonical name for 0x134 is BTN_WEST - BTN_Y only appears as an
    # alias, which is exactly why single-name lookups broke the mapping
    assert ig._code_names(ecodes.BTN, 0x134) == {"BTN_WEST", "BTN_Y"}
    assert "BTN_DPAD_UP" in ig._code_names(ecodes.BTN, 0x220)
    assert ig._code_names(ecodes.ABS, 0) == {"ABS_X"}


def _fake_event(type_, code, value):
    class _Ev:
        pass

    ev = _Ev()
    ev.type = type_
    ev.code = code
    ev.value = value
    return ev


@pytest.mark.skipif(not HAVE_EVDEV, reason="evdev not installed")
def test_on_event_routes_through_aliases():
    """A pad whose buttons resolve to alias-first names still drives."""
    from evdev import ecodes

    sess = ig.GamepadSession.__new__(ig.GamepadSession)  # skip USB device setup
    sess.state = ig.GamepadState()
    sess.abs_range = {0: (-1, 1), 1: (-1, 1)}

    # left stick: ABS_Y negative = stick up = forward
    sess._on_event(_fake_event(ecodes.EV_ABS, 1, -1))
    assert sess.state.fwd == 1.0

    # BTN_A (0x130) is turbo-while-held, however the table aliases it.
    # Turbo maps to SPEED_TURBO in command() - st.speed keeps the driver's
    # pace so releasing the button restores it (it used to latch 100).
    sess._on_event(_fake_event(ecodes.EV_KEY, 0x130, 1))
    assert sess.state.turbo is True and sess.state.command().speed == ig.SPEED_TURBO
    sess._on_event(_fake_event(ecodes.EV_KEY, 0x130, 0))
    assert sess.state.turbo is False and sess.state.speed == ig.SPEED_NORMAL

    # BTN_Y (0x134) must not be mistaken for turbo/left/stop - it does nothing
    sess._on_event(_fake_event(ecodes.EV_KEY, 0x134, 1))
    assert sess.state.turbo is False and sess.state.lights is False

    # BTN_X (0x133) toggles lights
    sess._on_event(_fake_event(ecodes.EV_KEY, 0x133, 1))
    assert sess.state.lights is True

    # BTN_B (0x131) is a hard stop of the held controls
    sess.state.fwd, sess.state.steer = 0.5, 0.5
    sess._on_event(_fake_event(ecodes.EV_KEY, 0x131, 1))
    assert sess.state.fwd == 0.0 and sess.state.steer == 0.0

    # BTN_START (0x13b) quits
    sess._on_event(_fake_event(ecodes.EV_KEY, 0x13B, 1))
    assert sess.state.quit is True


# ------------------------------------------------- reader death and turbo

def _session_with_fake_dev(read_impl):
    import os
    import types

    sess = ig.GamepadSession.__new__(ig.GamepadSession)  # skip USB device setup
    sess.state = ig.GamepadState()
    sess.abs_range = {0: (-1, 1), 1: (-1, 1)}
    fd = os.open(os.devnull, os.O_RDONLY)  # always ready for select
    sess.dev = types.SimpleNamespace(fd=fd, read=read_impl)
    sess._stop = __import__("threading").Event()
    return sess, fd


@pytest.mark.skipif(not HAVE_EVDEV, reason="evdev not installed")
def test_reader_death_clears_controls_and_quits():
    """The pad vanishing mid-drive must release the controls and stop the
    command loop - a bare return left forward=True latched forever."""

    def boom():
        # like evdev's read(): the error surfaces during ITERATION, not on
        # the call - the handler must force the generator inside its try
        raise OSError("device removed")
        yield  # pragma: no cover - makes this a generator

    sess, fd = _session_with_fake_dev(boom)
    sess.state.fwd = 1.0
    sess.state.steer = 0.5
    sess.state.turbo = True
    try:
        sess._loop()  # must return on its own
    finally:
        import os
        os.close(fd)
    st = sess.state
    assert st.fwd == 0.0 and st.steer == 0.0 and st.turbo is False
    assert st.quit is True


@pytest.mark.skipif(not HAVE_EVDEV, reason="evdev not installed")
def test_select_failure_clears_controls_and_quits():
    import os

    sess, fd = _session_with_fake_dev(lambda: [])
    os.close(fd)  # select on it raises OSError (EBADF)
    sess.state.fwd = 1.0
    sess._loop()
    st = sess.state
    assert st.fwd == 0.0 and st.quit is True


@pytest.mark.skipif(not HAVE_EVDEV, reason="evdev not installed")
def test_turbo_release_restores_the_driver_speed():
    from evdev import ecodes

    from qcar.protocol import SPEED_NORMAL, SPEED_TURBO

    sess = ig.GamepadSession.__new__(ig.GamepadSession)
    sess.state = ig.GamepadState()
    sess.abs_range = {0: (-1, 1), 1: (-1, 1)}
    st = sess.state
    st.fwd = 1.0

    sess._on_event(_fake_event(ecodes.EV_KEY, 0x130, 1))  # BTN_A down
    assert st.turbo is True
    assert st.command().speed == SPEED_TURBO

    sess._on_event(_fake_event(ecodes.EV_KEY, 0x130, 0))  # release
    assert st.turbo is False
    assert st.speed == SPEED_NORMAL, "release latched full throttle"
    assert st.command().speed == SPEED_NORMAL
