"""A song's old listening, credited to its current identity (app/relink.py).

The shape found on the Pi: a missing Navidrome row under an old track UUID,
holding the plays, and a live row for the same song under a new one.
"""

from __future__ import annotations

import json

import pytest

from app import playcounts, relink, store
from conftest import add_track

ALEX = "u-alex"


@pytest.fixture
def db(navidrome_db, state_db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "navidrome_db", navidrome_db)
    return navidrome_db


def tagged(uuid: str) -> str:
    return json.dumps({"navidrome_uuid": [{"value": uuid}]})


def played(track_uuid: str, plays: int, when: str = "2026-09-01") -> None:
    store.connection().execute(
        "insert or replace into play_imported"
        " (played_at, track_uuid, user_id, username, source, plays)"
        " values (?, ?, ?, 'alex', 'lastfm', ?)", (when, track_uuid, ALEX, plays))
    store.connection().commit()


def song(db, row_id, uuid, missing=0, **fields):
    fields.setdefault("artist", "Radiohead")
    fields.setdefault("title", "Pyramid Song")
    add_track(db, row_id, tags=tagged(uuid), missing=missing,
              path=f"{row_id}.mp3", **fields)


def by_track(user_id=ALEX):
    totals: dict[str, int] = {}
    for _when, track_uuid, n in playcounts.increments(user_id):
        totals[track_uuid] = totals.get(track_uuid, 0) + n
    return totals


def test_an_old_identitys_plays_are_credited_to_the_live_song(db):
    song(db, "old", "u-old", missing=1)
    song(db, "new", "u-new")
    played("u-old", 4)
    played("u-new", 1, "2026-10-01")

    plan = relink.build()

    assert [(p.old_uuid, p.new_uuid, p.plays) for p in plan.pairs] == [
        ("u-old", "u-new", {"alex": 4})]
    assert by_track() == {"u-old": 4, "u-new": 1}

    relink.apply_plan(plan)

    # The readings themselves are untouched; only the credit moves.
    assert store.connection().execute(
        "select count(*) from play_imported where track_uuid = 'u-old'").fetchone()[0] == 1
    assert by_track() == {"u-new": 5}
    assert relink.build().pairs == []


def test_undo_gives_the_old_identity_its_plays_back(db):
    song(db, "old", "u-old", missing=1)
    song(db, "new", "u-new")
    played("u-old", 4)
    relink.apply_plan(relink.build())

    assert relink.undo() == 1
    assert by_track() == {"u-old": 4}


def test_a_song_with_two_live_copies_is_left_alone(db):
    song(db, "old", "u-old", missing=1)
    song(db, "a", "u-a")
    song(db, "b", "u-b")
    played("u-old", 4)

    plan = relink.build()

    assert plan.pairs == []
    assert plan.ambiguous == ["Radiohead - Pyramid Song: 2 live copies"]


def test_a_different_length_is_a_different_recording(db):
    song(db, "old", "u-old", missing=1, duration=290.0)
    song(db, "live", "u-live", duration=400.0)

    assert relink.build().pairs == []


def test_another_librarys_song_is_not_a_match(db):
    song(db, "old", "u-old", missing=1)
    song(db, "new", "u-new", library_id=2)

    assert relink.build().pairs == []


def test_a_uuid_still_live_somewhere_is_not_an_old_identity(db):
    """A missing row whose UUID is also on a live file is a moved copy, and
    its plays are already that live file's."""
    song(db, "old", "u-same", missing=1)
    song(db, "new", "u-same")

    assert relink.build().pairs == []


def test_aliases_follow_a_song_re_identified_twice(db):
    store.connection().executemany(
        "insert into play_alias (old_uuid, new_uuid, reason, created_at)"
        " values (?, ?, 'relink', '2026-10-09')",
        [("u-1", "u-2"), ("u-2", "u-3"), ("loop-a", "loop-b"), ("loop-b", "loop-a")])
    store.connection().commit()

    alias = playcounts.aliases()

    assert alias["u-1"] == "u-3" and alias["u-2"] == "u-3"
    assert alias["loop-a"] in ("loop-a", "loop-b")
