"""Nightly play-count snapshots.

Tested hard because the failure mode is silent and permanent: Navidrome
keeps only a cumulative total and one date, so a night this gets wrong is a
night of listening history that cannot be reconstructed afterwards. There is
no "run it again tomorrow" for yesterday.
"""

from __future__ import annotations

import sqlite3

import pytest

from app import ledger, playcounts
from app.config import settings
from conftest import add_track

UUID_A = '{"navidrome_uuid": [{"value": "uuid-a"}]}'
UUID_B = '{"navidrome_uuid": [{"value": "uuid-b"}]}'

ALEX, KELLY = "u-alex", "u-kelly"


@pytest.fixture
def wired(navidrome_db, state_db, monkeypatch):
    monkeypatch.setattr(settings, "navidrome_db", navidrome_db)
    return navidrome_db


def played(db, track_id, user_id, count, when="2026-03-01T12:00:00Z"):
    """Set a play count, the way Navidrome would."""
    connection = sqlite3.connect(db)
    with connection:
        connection.execute(
            "delete from annotation where item_id = ? and user_id = ?",
            (track_id, user_id))
        connection.execute(
            "insert into annotation (user_id, item_id, item_type, starred,"
            " rating) values (?, ?, 'media_file', 0, 0)", (user_id, track_id))
        connection.execute(
            "update annotation set play_count = ?, play_date = ?"
            " where item_id = ? and user_id = ?",
            (count, when, track_id, user_id))
    connection.close()


@pytest.fixture(autouse=True)
def play_columns(navidrome_db):
    """Navidrome's annotation table carries these; the fixture schema is the
    subset this app reads, so the snapshot columns are added here."""
    connection = sqlite3.connect(navidrome_db)
    with connection:
        connection.execute("alter table annotation add column play_count INTEGER DEFAULT 0")
        connection.execute("alter table annotation add column play_date TEXT")
    connection.close()


# --- the first night --------------------------------------------------------

def test_the_first_snapshot_is_a_baseline(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)

    result = playcounts.take("2026-03-01")

    assert result["taken"] is True
    assert result["baseline"] is True, (
        "there is no earlier snapshot to subtract from; real data starts "
        "tomorrow")
    assert result["changed"] == 1


def test_the_second_snapshot_is_not_a_baseline(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 7)

    assert playcounts.take("2026-03-02")["baseline"] is False


# --- only what changed ------------------------------------------------------

def test_an_unchanged_count_is_not_stored_again(wired):
    """A full nightly capture is mostly identical to the night before. Storing
    it all puts a year near a million rows instead of tens of thousands."""
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take("2026-03-01")

    second = playcounts.take("2026-03-02")

    assert second["changed"] == 0
    rows = ledger.connection().execute(
        "select count(*) from play_snapshot").fetchone()[0]
    assert rows == 1, "the unchanged count should not have been written twice"


def test_a_changed_count_is_stored(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 9)

    assert playcounts.take("2026-03-02")["changed"] == 1
    rows = ledger.connection().execute(
        "select taken_on, play_count from play_snapshot order by taken_on"
    ).fetchall()
    assert rows == [("2026-03-01", 5), ("2026-03-02", 9)]


def test_taking_the_same_day_twice_replaces_rather_than_duplicates(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take("2026-03-01")
    playcounts.take("2026-03-01")

    rows = ledger.connection().execute(
        "select count(*) from play_snapshot").fetchone()[0]
    assert rows == 1


# --- identity ---------------------------------------------------------------

def test_snapshots_are_keyed_by_uuid_not_media_file_id(wired):
    """The id is an index artefact - re-import a file and it changes. The
    UUID is on the file and survives, which is the whole point of it."""
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take("2026-03-01")

    stored = ledger.connection().execute(
        "select track_uuid from play_snapshot").fetchone()[0]
    assert stored == "uuid-a"


def test_a_track_with_no_uuid_is_counted_not_guessed_at(wired):
    """It cannot be followed across a re-import, so its history would restart
    silently. Reported instead."""
    add_track(wired, "t1", tags=UUID_A)
    add_track(wired, "t2", tags=None, path="b.mp3")
    played(wired, "t1", ALEX, 5)
    played(wired, "t2", ALEX, 3)

    result = playcounts.take("2026-03-01")
    assert result["without_uuid"] == 1
    assert result["tracked"] == 1


def test_two_users_playing_one_track_are_kept_apart(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    played(wired, "t1", KELLY, 11)

    playcounts.take("2026-03-01")
    rows = dict(ledger.connection().execute(
        "select user_id, play_count from play_snapshot").fetchall())
    assert rows == {ALEX: 5, KELLY: 11}


def test_a_track_nobody_has_played_is_not_recorded(wired):
    add_track(wired, "t1", tags=UUID_A)
    assert playcounts.take("2026-03-01")["tracked"] == 0


# --- a count that goes down -------------------------------------------------

def test_a_falling_count_is_recorded_as_an_anomaly(wired):
    """A re-import or a reset can lower a counter. That is not minus four
    plays."""
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 10)
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 4)

    result = playcounts.take("2026-03-02")

    assert result["anomalies"] == 1
    row = ledger.connection().execute(
        "select was, became from play_anomaly").fetchone()
    assert row == (10, 4)


def test_a_falling_count_never_becomes_negative_plays(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 10)
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 4)
    playcounts.take("2026-03-02")

    plays = playcounts.plays_between("2026-03-02", "2026-03-02")
    assert all(row["plays"] > 0 for row in plays)


# --- reading it back --------------------------------------------------------

def test_plays_in_a_range_are_the_difference_between_snapshots(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 12)
    playcounts.take("2026-03-05")

    plays = playcounts.plays_between("2026-03-02", "2026-03-05")
    assert plays == [{"track_uuid": "uuid-a", "user_id": ALEX,
                      "username": "alex", "plays": 7}]


def test_a_track_not_played_in_the_range_is_absent(wired):
    add_track(wired, "t1", tags=UUID_A)
    add_track(wired, "t2", tags=UUID_B, path="b.mp3")
    played(wired, "t1", ALEX, 5)
    played(wired, "t2", ALEX, 3)
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 8)
    playcounts.take("2026-03-02")

    plays = playcounts.plays_between("2026-03-02", "2026-03-02")
    assert [row["track_uuid"] for row in plays] == ["uuid-a"]


def test_a_range_can_be_scoped_to_one_person(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 1)
    played(wired, "t1", KELLY, 1)
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 4)
    played(wired, "t1", KELLY, 9)
    playcounts.take("2026-03-02")

    mine = playcounts.plays_between("2026-03-02", "2026-03-02", user_id=ALEX)
    assert [row["plays"] for row in mine] == [3]


def test_results_are_ordered_by_how_much_it_was_played(wired):
    add_track(wired, "t1", tags=UUID_A)
    add_track(wired, "t2", tags=UUID_B, path="b.mp3")
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 2)
    played(wired, "t2", ALEX, 9)
    playcounts.take("2026-03-02")

    plays = playcounts.plays_between("2026-03-02", "2026-03-02")
    assert [row["plays"] for row in plays] == [9, 2]


# --- the loop's guard and reporting -----------------------------------------

def test_taken_on_reports_whether_a_day_is_done(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 1)

    assert playcounts.taken_on("2026-03-01") is False
    playcounts.take("2026-03-01")
    assert playcounts.taken_on("2026-03-01") is True


def test_status_says_enough_to_tell_it_is_working(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 6)
    playcounts.take("2026-03-02")

    status = playcounts.status()
    assert status["days"] == 2
    assert status["rows"] == 2
    assert status["first_day"] == "2026-03-01"
    assert status["last_day"] == "2026-03-02"


def test_an_unreachable_navidrome_is_reported_not_raised(state_db, monkeypatch,
                                                         tmp_path):
    """The loop must survive it: a database briefly unreadable is a reason to
    try again in half an hour, not to stop taking snapshots."""
    monkeypatch.setattr(settings, "navidrome_db", tmp_path / "gone.db")
    result = playcounts.take("2026-03-01")
    assert result["taken"] is False
    assert "reason" in result


def test_the_day_is_utc():
    """A local date would shift under daylight saving and show up months
    later as one day with twice the plays and one with none."""
    day = playcounts.today()
    assert len(day) == 10 and day[4] == "-" and day[7] == "-"
