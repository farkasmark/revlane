"""Garage persistence: a broken store must never be silently destroyed.

Regression: a malformed profiles.json loaded as an empty garage, and the
first later save() replaced the original file - the user's cars were gone
with no trace. Loading now quarantines an unreadable store (and a valid-JSON
wrong-shape one) so the next save works from a clean slate while the
original stays recoverable.
"""

import json

from qcar.profiles import Garage, Profile


def test_load_missing_file_returns_empty_garage(tmp_path):
    g = Garage.load(tmp_path / "profiles.json")
    assert len(g) == 0
    assert g.dirty is False


def test_load_valid_file_round_trips(tmp_path):
    p = tmp_path / "profiles.json"
    Garage(path=p, profiles=[Profile(name="A", address="AA:11")]).save()
    g = Garage.load(p)
    assert len(g) == 1
    assert g.profiles[0].name == "A"
    assert g.dirty is False


def test_malformed_json_is_backed_up_not_destroyed(tmp_path):
    p = tmp_path / "profiles.json"
    p.write_text("{not json at all")
    g = Garage.load(p)
    assert len(g) == 0
    assert g.dirty is False  # loading alone never schedules a write
    backups = list(tmp_path.glob("profiles.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "{not json at all"
    assert p.read_text() == "{not json at all"  # original untouched


def test_wrong_top_level_shape_is_backed_up(tmp_path):
    """Valid JSON that is not an object would crash .get() - same hazard."""
    p = tmp_path / "profiles.json"
    p.write_text('["a bare list"]')
    g = Garage.load(p)
    assert len(g) == 0
    assert list(tmp_path.glob("profiles.json.corrupt-*"))


def test_cars_not_a_list_is_backed_up(tmp_path):
    p = tmp_path / "profiles.json"
    p.write_text('{"cars": "oops"}')
    g = Garage.load(p)
    assert len(g) == 0
    assert list(tmp_path.glob("profiles.json.corrupt-*"))


def test_non_dict_entries_are_skipped(tmp_path):
    p = tmp_path / "profiles.json"
    p.write_text(json.dumps({"cars": [{"name": "A", "address": "AA"}, "junk", 7]}))
    g = Garage.load(p)
    assert len(g) == 1
    assert g.profiles[0].name == "A"


def test_later_save_leaves_the_backup_intact(tmp_path):
    p = tmp_path / "profiles.json"
    p.write_text("garbage")
    g = Garage.load(p)
    g.add("Fresh", "BB:22")
    g.save()
    backups = list(tmp_path.glob("profiles.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "garbage"
    assert json.loads(p.read_text())["cars"][0]["name"] == "Fresh"
