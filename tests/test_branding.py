"""Branding guard: our own name on every surface that could imply endorsement.

Names are not copyrightable - the exposure is trademark, specifically whether a
reasonable person thinks Brandbase or Shell published or authorised this. The
surfaces that decide that are few and specific: the package name, the window
title, the web page title, the CLI banner. Third-party marks belong in prose
that describes compatibility (README, PROTOCOL.md, docstrings), never in
branding, and never compounded with "open"/"free" into something that reads like
a community edition of someone else's product.

These tests exist so the decision cannot rot back in.
"""

import re
from pathlib import Path

import pytest

from qcar import APP_NAME, CONFIG_DIR
from qcar import profiles as profiles_mod

ROOT = Path(__file__).resolve().parent.parent
MARKS = ("Shell", "Brandbase")


def read(rel: str) -> str:
    return (ROOT / rel).read_text()


def test_our_name_is_ours():
    assert APP_NAME == "revlane"
    assert not any(m.lower() in APP_NAME.lower() for m in MARKS)


def test_package_is_not_named_after_someone_elses_mark():
    """`open` + someone's mark reads as the community edition of their product."""
    pyproject = read("pyproject.toml")
    name = re.search(r'^name = "(.+)"', pyproject, re.M).group(1)
    assert name == "revlane"
    assert not any(m.lower() in name.lower() for m in MARKS)


def test_window_title_carries_our_name_and_no_theirs():
    """A title bar is the most endorsement-shaped surface an app has."""
    src = read("qcar/gui/app.py")
    caption = re.search(r"set_caption\((.+)\)", src).group(1)
    assert "APP_NAME" in caption
    assert not any(m in caption for m in MARKS), f"{caption!r} must not brand us as theirs"


def test_web_page_title_is_ours():
    html = read("qcar/web/index.html")
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    assert title == "revlane"


def test_cli_banner_is_ours():
    src = read("qcar/cli.py")
    assert '{APP_NAME} web controller' in src
    assert "openbrandbase web controller" not in src


def test_readme_disclaims_affiliation():
    """Doesn't create rights, but it removes the 'confusion' ingredient that a
    takedown reviewer looks for, so it must not be dropped in a tidy-up."""
    readme = read("README.md")
    assert "not affiliated with" in readme.lower()
    assert "trademarks of their" in readme.lower()


def test_description_states_compatibility_not_branding():
    """Naming what it interoperates with is nominative use and is fine - but the
    project is not called after it."""
    desc = re.search(r'^description = "(.+)"', read("pyproject.toml"), re.M).group(1)
    assert not any(m.lower() in desc.lower() for m in MARKS)


# ------------------------------------------------------- config path & rename


def test_garage_lives_under_our_own_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert profiles_mod.default_path() == tmp_path / CONFIG_DIR / "profiles.json"


def test_garage_roundtrips_at_the_default_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    garage = profiles_mod.Garage()
    garage.add("Marci's BMW M3", "00:3C:DE:07:01:A4")
    garage.save()
    assert (tmp_path / CONFIG_DIR / "profiles.json").exists()
    # note Garage() constructs empty by design; loading is the explicit ctor
    assert len(profiles_mod.Garage.load()) == 1


def test_the_old_name_is_gone_from_the_package():
    """One constant so a rename is one edit, and no leftovers to rot back."""
    assert CONFIG_DIR == APP_NAME == "revlane"
    hits = [
        str(p.relative_to(ROOT))
        for p in ROOT.joinpath("qcar").rglob("*.py")
        if "openbrandbase" in p.read_text()
    ]
    assert hits == [], f"unexpected hard-coded old name in {hits}; use qcar.APP_NAME"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
