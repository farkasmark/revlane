"""SDL2 (pygame-ce) front end: garage, profiles and a touch drive pad.

Imports lazily on purpose: ``qcar.gui.backend`` is a pure-BLE controller used by
the web server and the tests, and must stay importable on a machine with no
pygame (and without splashing pygame's banner over a terminal).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # let type checkers see the real symbols
    from .app import App

__all__ = ["App"]


def __getattr__(name: str) -> Any:
    if name == "App":
        from .app import App

        return App
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
