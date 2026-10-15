"""The garage: named, saved cars.

A *profile* is the thing you care about ("Marci's BMW M3"), the MAC address is
the thing the radio cares about.  Profiles survive the car being power cycled,
re-paired, or re-discovered under a different RSSI, and they hold the per-car
defaults you do not want to re-set every session (speed cap, turbo, lights).

Stored as plain JSON at ``$XDG_CONFIG_HOME/revlane/profiles.json`` so it is
trivially editable, versionable, and syncable.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
import uuid
from pathlib import Path

from . import CONFIG_DIR

ACCENTS = ("#ffb300", "#e53935", "#1e88e5", "#43a047", "#8e24aa", "#00acc1", "#f4511e", "#c0ca33")


def default_path() -> Path:
    if os.environ.get("ANDROID_ARGUMENT"):
        # inside the APK: HOME is unset and expanduser("~") is a literal
        # "~" with no passwd entry behind it - p4a points ANDROID_PRIVATE at
        # the app's writable files directory, so hang config off that
        base = Path(os.environ.get("ANDROID_PRIVATE") or os.environ["ANDROID_ARGUMENT"])
        return base / ".config" / CONFIG_DIR / "profiles.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"))
    return base / CONFIG_DIR / "profiles.json"


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "car"


@dataclasses.dataclass
class Profile:
    """One saved car."""

    name: str
    address: str
    id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex[:8])
    model: str = "QCAR"
    accent: str = "#ffb300"
    speed_cap: int = 80
    turbo_default: bool = False
    lights_default: bool = False
    notes: str = ""
    created: float = dataclasses.field(default_factory=time.time)
    last_seen: float | None = None
    last_battery: int | None = None
    sessions: int = 0
    drive_seconds: float = 0.0

    # ---- serialisation -------------------------------------------------

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> Profile:
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    # ---- helpers -------------------------------------------------------

    @property
    def speed_default(self) -> int:
        return 100 if self.turbo_default else min(80, self.speed_cap)

    def touch_seen(self, battery: int | None = None) -> None:
        self.last_seen = time.time()
        if battery is not None:
            self.last_battery = battery

    def start_session(self) -> None:
        self.sessions += 1
        self.last_seen = time.time()

    def end_session(self, seconds: float) -> None:
        self.drive_seconds = round(self.drive_seconds + max(0.0, seconds), 1)
        self.last_seen = time.time()

    @property
    def odometer(self) -> str:
        m = int(self.drive_seconds // 60)
        if m < 60:
            return f"{m} min"
        return f"{m // 60} h {m % 60:02d} min"

    @property
    def last_seen_text(self) -> str:
        if not self.last_seen:
            return "never"
        dt = time.time() - self.last_seen
        if dt < 90:
            return "just now"
        if dt < 3600:
            return f"{int(dt // 60)} min ago"
        if dt < 86400:
            return f"{int(dt // 3600)} h ago"
        return time.strftime("%Y-%m-%d", time.localtime(self.last_seen))

    def __str__(self) -> str:
        return f"{self.name}  {self.address}  cap {self.speed_cap}%  {self.odometer}"


class Garage:
    """Mutable collection of profiles, backed by a JSON file."""

    VERSION = 1

    def __init__(self, path: Path | None = None, profiles: list[Profile] | None = None):
        self.path = Path(path) if path else default_path()
        self.profiles: list[Profile] = profiles or []
        self.dirty = False

    # ---- persistence ---------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> Garage:
        g = cls(path)
        if not g.path.is_file():
            return g
        try:
            raw = json.loads(g.path.read_text())
        except (json.JSONDecodeError, OSError):
            return g
        for item in raw.get("cars", []):
            try:
                g.profiles.append(Profile.from_dict(item))
            except TypeError:
                continue
        return g

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.VERSION,
            "updated": time.time(),
            "cars": [p.to_dict() for p in self.profiles],
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(self.path)
        self.dirty = False
        return self.path

    # ---- queries -------------------------------------------------------

    def sorted(self) -> list[Profile]:
        return sorted(self.profiles, key=lambda p: (-(p.last_seen or 0), p.name.lower()))

    def find(self, address: str) -> Profile | None:
        address = address.upper()
        for p in self.profiles:
            if p.address.upper() == address:
                return p
        return None

    def by_id(self, pid: str) -> Profile | None:
        for p in self.profiles:
            if p.id == pid:
                return p
        return None

    def __len__(self) -> int:
        return len(self.profiles)

    def __iter__(self):
        return iter(self.profiles)

    # ---- mutations -----------------------------------------------------

    def add(self, name: str, address: str, **kw) -> Profile:
        existing = self.find(address)
        if existing:
            existing.name = name
            for k, v in kw.items():
                setattr(existing, k, v)
            self.dirty = True
            return existing
        p = Profile(name=name, address=address.upper(), **kw)
        if "accent" not in kw:
            p.accent = ACCENTS[len(self.profiles) % len(ACCENTS)]
        self.profiles.append(p)
        self.dirty = True
        return p

    def remove(self, pid: str) -> bool:
        before = len(self.profiles)
        self.profiles = [p for p in self.profiles if p.id != pid]
        if len(self.profiles) != before:
            self.dirty = True
            return True
        return False

    def suggest_name(self, address: str) -> str:
        """'QCAR-0701A4' -> 'Car 0701A4', skipping names we already use."""
        tail = address.replace(":", "")[-6:]
        base = f"Car {tail}"
        names = {p.name for p in self.profiles}
        if base not in names:
            return base
        n = 2
        while f"{base} {n}" in names:
            n += 1
        return f"{base} {n}"
