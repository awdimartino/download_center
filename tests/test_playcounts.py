"""Nightly play-count snapshots.

Tested hard because the failure mode is silent and permanent: Navidrome
keeps only a cumulative total and one date, so a night this gets wrong is a
night of listening history that cannot be reconstructed afterwards. There is
no "run it again tomorrow" for yesterday.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app import store, playcounts
from app.config import settings
from conftest import add_track
from app.api import listening as listening_routes

UUID_A = '{"navidrome_uuid": [{"value": "uuid-a"}]}'
UUID_B = '{"navidrome_uuid": [{"value": "uuid-b"}]}'

ALEX, KELLY = "u-alex", "u-kelly"


@pytest.fixture
def wired(navidrome_db, state_db, monkeypatch):
    monkeypatch.setattr(settings, "navidrome_db", navidrome_db)
    return navidrome_db


def played(db, track_id, user_id, count, when=None):
    """Set a play count, the way Navidrome would. `when` is its play_date;
    left out, the reading that catches the rise dates the play."""
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
    rows = store.connection().execute(
        "select count(*) from play_snapshot").fetchone()[0]
    assert rows == 1, "the unchanged count should not have been written twice"


def test_a_changed_count_is_stored(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 9)

    assert playcounts.take("2026-03-02")["changed"] == 1
    rows = store.connection().execute(
        "select taken_on, play_count from play_snapshot order by taken_on"
    ).fetchall()
    assert rows == [("2026-03-01", 5), ("2026-03-02", 9)]


def test_taking_the_same_day_twice_replaces_rather_than_duplicates(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take("2026-03-01")
    playcounts.take("2026-03-01")

    rows = store.connection().execute(
        "select count(*) from play_snapshot").fetchone()[0]
    assert rows == 1


# --- identity ---------------------------------------------------------------

def test_snapshots_are_keyed_by_uuid_not_media_file_id(wired):
    """The id is an index artefact - re-import a file and it changes. The
    UUID is on the file and survives, which is the whole point of it."""
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take("2026-03-01")

    stored = store.connection().execute(
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
    rows = dict(store.connection().execute(
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
    row = store.connection().execute(
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

def _run_logged(day: str) -> bool:
    """Whether the run log says a reading was taken that day."""
    return store.connection().execute(
        "SELECT 1 FROM play_snapshot_run WHERE day = ? LIMIT 1",
        (day,)).fetchone() is not None


def test_a_reading_is_logged_as_run(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 1)

    assert not _run_logged("2026-03-01")
    playcounts.take("2026-03-01")
    assert _run_logged("2026-03-01")


def test_status_says_enough_to_tell_it_is_working(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 6)
    playcounts.take("2026-03-02")

    snapshots = playcounts.status()["snapshots"]
    assert snapshots["days"] == 2
    assert snapshots["rows"] == 2
    assert snapshots["first_day"] == "2026-03-01"
    assert snapshots["last_day"] == "2026-03-02"


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


# --- which midnight closes a day -------------------------------------------

def test_the_zone_is_configurable(monkeypatch):
    monkeypatch.setattr(settings, "play_day_timezone", "America/New_York")
    assert "New_York" in str(playcounts.zone())


def test_utc_never_needs_a_timezone_database(monkeypatch):
    """The fallback must not be able to raise the error it is catching.
    ZoneInfo("UTC") needs tzdata like any other name; datetime.UTC does not."""
    monkeypatch.setattr(settings, "play_day_timezone", "UTC")
    assert playcounts.zone() is not None
    monkeypatch.setattr(settings, "play_day_timezone", "Not/AZone")
    assert playcounts.zone() is not None



# --- imported history sits beside the snapshots ----------------------------

def _import(when, track_uuid, user_id, plays, username="alex"):
    """`when` is a bare date for history whose time was never recovered, or
    a full timestamp for a scrobble that has one."""
    store.connection().execute(
        "INSERT OR REPLACE INTO play_imported"
        " (played_at, track_uuid, user_id, username, plays, source)"
        " VALUES (?, ?, ?, ?, ?, 'lastfm')",
        (when, track_uuid, user_id, username, plays))
    store.connection().commit()


def test_imported_history_is_readable_alongside_snapshots(wired):
    _import("2026-02-14", "uuid-a", ALEX, 4)
    plays = playcounts.plays_between("2026-02-01", "2026-02-28")
    assert plays == [{"track_uuid": "uuid-a", "user_id": ALEX,
                      "username": "alex", "plays": 4}]


def test_imported_history_outside_the_range_is_ignored(wired):
    _import("2026-01-01", "uuid-a", ALEX, 4)
    assert playcounts.plays_between("2026-02-01", "2026-02-28") == []


def test_importing_twice_does_not_double_a_history(wired):
    """The key includes the source, so a re-run replaces its own rows."""
    _import("2026-02-14", "uuid-a", ALEX, 4)
    _import("2026-02-14", "uuid-a", ALEX, 4)
    plays = playcounts.plays_between("2026-02-01", "2026-02-28")
    assert plays[0]["plays"] == 4


def test_imported_history_can_be_scoped_to_one_person(wired):
    _import("2026-02-14", "uuid-a", ALEX, 4)
    _import("2026-02-14", "uuid-a", KELLY, 9, username="kelly")
    mine = playcounts.plays_between("2026-02-01", "2026-02-28", user_id=ALEX)
    assert [row["plays"] for row in mine] == [4]


# --- tracks whose files have gone -------------------------------------------

def test_a_track_in_a_vanished_directory_is_not_counted(wired):
    """Navidrome marks a disappeared directory missing on the *folder* row
    and leaves the rows beneath it untouched. Trusting media_file.missing
    alone counts tracks whose files were deleted months ago - 49 of them on
    the real library, left behind by the migration."""
    add_track(wired, "here", tags=UUID_A, folder_id="f1")
    add_track(wired, "gone", tags=UUID_B, folder_id="gone", path="b.mp3")
    played(wired, "here", ALEX, 5)
    played(wired, "gone", ALEX, 40)

    result = playcounts.take("2026-03-01")

    assert result["tracked"] == 1
    stored = store.connection().execute(
        "select track_uuid, play_count from play_snapshot").fetchall()
    assert stored == [("uuid-a", 5)]


def test_a_file_flagged_missing_is_not_counted(wired):
    add_track(wired, "here", tags=UUID_A)
    add_track(wired, "gone", tags=UUID_B, path="b.mp3", missing=1)
    played(wired, "here", ALEX, 5)
    played(wired, "gone", ALEX, 40)

    assert playcounts.take("2026-03-01")["tracked"] == 1


# --- status reports both halves of the record -------------------------------

def test_status_reports_imported_history_too(wired):
    """Leaving it out made 41,000 imported plays look like nothing was
    there: the record is snapshots *and* the history from before them."""
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take("2026-03-01")
    _import("2026-02-14", "uuid-a", ALEX, 40)

    status = playcounts.status()
    assert status["snapshots"]["days"] == 1
    assert len(status["imported"]) == 1
    assert status["imported"][0]["source"] == "lastfm"
    assert status["imported"][0]["plays"] == 40
    assert status["imported"][0]["first_day"] == "2026-02-14"


def test_status_says_whether_the_collector_is_alive(wired):
    """Not "has yesterday been captured" any more. Readings are taken every
    few minutes, so the only way to be behind is to have stopped, and the
    answer is how long ago the last one was."""
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)

    assert playcounts.status()["up_to_date"] is False
    playcounts.take()
    status = playcounts.status()
    assert status["up_to_date"] is True
    assert status["awaiting"] == playcounts.last_reading()


def test_a_collector_that_stopped_this_morning_is_not_healthy(wired):
    """The failure this is here to catch: the loop dies, nobody listens for
    a while anyway, and nothing distinguishes that from a quiet evening."""
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take()
    assert playcounts.read_recently() is True

    stale = (datetime.now(UTC)
             - timedelta(minutes=playcounts.STALE_AFTER_MINUTES + 1))
    store.connection().execute(
        "update play_snapshot_run set taken_at = ?",
        (stale.isoformat(timespec="seconds"),))
    store.connection().commit()

    assert playcounts.read_recently() is False
    assert playcounts.status()["up_to_date"] is False


def test_a_quiet_hour_is_not_a_dead_collector(wired):
    """Only changed counts are stored, so an hour when nobody listened
    writes no snapshot rows. Health is read from the run log for exactly
    this reason."""
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take()
    before = store.connection().execute(
        "select count(*) from play_snapshot").fetchone()[0]

    playcounts.take()

    assert store.connection().execute(
        "select count(*) from play_snapshot").fetchone()[0] == before
    assert playcounts.read_recently() is True


def test_status_with_nothing_recorded_yet(wired):
    status = playcounts.status()
    assert status["snapshots"]["days"] == 0
    assert status["imported"] == []
    assert status["up_to_date"] is False


# --- a day when nobody listened ---------------------------------------------

def test_a_day_with_no_changes_still_counts_as_taken(wired):
    """Only changed counts are stored, so a quiet day writes no rows at all.
    Asking play_snapshot whether the day is done then answers no for ever:
    the nightly job repeated it every half hour and the status never caught
    up. Found in production the day after this shipped."""
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take("2026-03-01")

    result = playcounts.take("2026-03-02")
    assert result["changed"] == 0

    assert _run_logged("2026-03-02"), (
        "a quiet day is a real answer, not an incomplete one")
    rows = store.connection().execute(
        "select count(*) from play_snapshot where taken_on = '2026-03-02'"
    ).fetchone()[0]
    assert rows == 0, "and it should still not have written a snapshot row"


def test_the_run_log_records_what_happened(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 7)
    playcounts.take("2026-03-01")

    row = store.connection().execute(
        "select day, tracked, changed, anomalies from play_snapshot_run"
    ).fetchone()
    assert row == ("2026-03-01", 1, 1, 0)


def test_status_is_up_to_date_after_a_quiet_day(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)
    playcounts.take()
    playcounts.take()          # nothing changed since

    status = playcounts.status()
    assert status["up_to_date"] is True
    assert status["snapshots"]["days_run"] == 1


# --- reading it back in the browser -----------------------------------------
# The snapshots ran for weeks with nothing able to display them, which from
# the outside was indistinguishable from nothing being collected at all.

def test_top_tracks_are_named_from_navidrome(wired):
    """Snapshots are keyed by UUID and nothing else - that is what makes a
    count survive retagging - which also makes the stored rows unreadable
    without asking Navidrome what each one is called."""
    add_track(wired, "t1", tags=UUID_A, title="Let Down", artist="Radiohead")
    played(wired, "t1", ALEX, 1)
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 9)
    playcounts.take("2026-03-02")

    top = playcounts.top_tracks("2026-03-02", "2026-03-02", ALEX)
    assert [(t["title"], t["artist"], t["plays"]) for t in top] == [
        ("Let Down", "Radiohead", 8)]
    assert top[0]["known"] is True


def test_a_track_that_has_left_the_library_still_counts(wired):
    """The plays happened. Dropping the row because the file is gone would
    quietly rewrite history, which is the one thing this module exists to
    prevent."""
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 1)
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 4)
    playcounts.take("2026-03-02")
    sqlite3.connect(wired).execute("delete from media_file").connection.commit()

    top = playcounts.top_tracks("2026-03-02", "2026-03-02", ALEX)
    assert top[0]["plays"] == 3
    assert top[0]["known"] is False
    assert "no longer in the library" in top[0]["title"]


def test_the_limit_is_honoured(wired):
    add_track(wired, "t1", tags=UUID_A)
    add_track(wired, "t2", tags=UUID_B, path="b.mp3")
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 5)
    played(wired, "t2", ALEX, 9)
    playcounts.take("2026-03-02")

    assert len(playcounts.top_tracks("2026-03-02", "2026-03-02", ALEX, 1)) == 1


# --- top albums --------------------------------------------------------------

def test_album_plays_are_summed_across_their_tracks(wired):
    add_track(wired, "t1", tags=UUID_A, album="Kid A", album_artist="Radiohead")
    add_track(wired, "t2", tags=UUID_B, path="b.mp3",
              album="Kid A", album_artist="Radiohead")
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 3)
    played(wired, "t2", ALEX, 2)
    playcounts.take("2026-03-02")

    albums = playcounts.top_albums("2026-03-02", "2026-03-02", ALEX)
    assert albums == [{"artist": "Radiohead", "album": "Kid A", "plays": 5}]


def test_same_named_albums_by_different_artists_are_kept_apart(wired):
    add_track(wired, "t1", tags=UUID_A, album="Greatest Hits",
              album_artist="Queen")
    add_track(wired, "t2", tags=UUID_B, path="b.mp3",
              album="Greatest Hits", album_artist="Abba")
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 4)
    played(wired, "t2", ALEX, 1)
    playcounts.take("2026-03-02")

    albums = playcounts.top_albums("2026-03-02", "2026-03-02", ALEX)
    assert {(a["artist"], a["plays"]) for a in albums} == {
        ("Queen", 4), ("Abba", 1)}


def test_a_track_with_no_album_is_not_grouped_into_one(wired):
    add_track(wired, "t1", tags=UUID_A, album="")
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 5)
    playcounts.take("2026-03-02")

    assert playcounts.top_albums("2026-03-02", "2026-03-02", ALEX) == []


def test_the_album_list_is_capped(wired):
    for n in range(3):
        add_track(wired, f"t{n}", tags=json.dumps(
            {"navidrome_uuid": [{"value": f"uuid-{n}"}]}),
            path=f"{n}.mp3", album=f"Album {n}", album_artist="Artist")
    playcounts.take("2026-03-01")
    for n in range(3):
        played(wired, f"t{n}", ALEX, n + 1)
    playcounts.take("2026-03-02")

    assert len(playcounts.top_albums(
        "2026-03-02", "2026-03-02", ALEX, limit=2)) == 2


# --- top genres ----------------------------------------------------------

def test_genre_plays_are_summed_across_their_tracks(wired):
    add_track(wired, "t1", tags=json.dumps(
        {"navidrome_uuid": [{"value": "uuid-a"}],
         "genre": [{"value": "Ambient"}]}))
    add_track(wired, "t2", path="b.mp3", tags=json.dumps(
        {"navidrome_uuid": [{"value": "uuid-b"}],
         "genre": [{"value": "Ambient"}]}))
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 3)
    played(wired, "t2", ALEX, 2)
    playcounts.take("2026-03-02")

    genres = playcounts.top_genres("2026-03-02", "2026-03-02", ALEX)
    assert genres == [{"genre": "Ambient", "plays": 5}]


def test_an_untagged_genre_is_left_out(wired):
    add_track(wired, "t1", tags=UUID_A)
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 5)
    playcounts.take("2026-03-02")

    assert playcounts.top_genres("2026-03-02", "2026-03-02", ALEX) == []


def test_coverage_is_one_persons_own_history(wired):
    """status() answers for the installation, which is right for a health
    check and wrong for a panel: one account's imported Last.fm history is
    not another account's to read."""
    store.connection().execute(
        "insert into play_imported"
        " (track_uuid, user_id, username, played_at, plays, source)"
        " values ('uuid-a', ?, 'alex', '2024-01-01', 40, 'lastfm')",
        (ALEX,))
    store.connection().commit()

    mine = playcounts.coverage(ALEX)
    theirs = playcounts.coverage(KELLY)

    assert mine["imported_plays"] == 40
    assert mine["imported_sources"] == ["lastfm"]
    assert theirs["imported_plays"] == 0
    assert theirs["imported_sources"] == []


def test_coverage_still_reports_the_job_globally(wired):
    """Whether the nightly run happened is a fact about the collector, not
    about the person reading it."""
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 3)
    playcounts.take("2026-03-01")

    for who in (ALEX, KELLY):
        assert playcounts.coverage(who)["days_run"] == 1


# --- a range that opens before collection began -----------------------------
# With no reading before the range there was nothing to subtract, and the
# opening balance fell back to zero: each track's lifetime count at the
# first reading was counted as plays in range, on top of the imported plays
# that lifetime already includes. All time showed 192 for 92 (CODE_REVIEW H4).

def test_all_time_does_not_count_the_first_reading_as_plays(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 100)
    playcounts.take("2026-03-01")
    played(wired, "t1", ALEX, 102)
    playcounts.take("2026-03-05")
    _import("2024-06-01", "uuid-a", ALEX, 90)

    plays = playcounts.plays_between("2000-01-01", "2026-12-31", user_id=ALEX)

    assert [row["plays"] for row in plays] == [92]


def test_a_track_first_played_after_collection_began_counts_from_zero(wired):
    add_track(wired, "t1", tags=UUID_A)
    add_track(wired, "t2", tags=UUID_B, path="b.mp3")
    played(wired, "t1", ALEX, 100)
    playcounts.take("2026-03-01")
    played(wired, "t2", ALEX, 2)
    playcounts.take("2026-03-05")

    plays = playcounts.plays_between("2000-01-01", "2026-12-31", user_id=ALEX)

    assert {row["track_uuid"]: row["plays"] for row in plays} == {"uuid-b": 2}


def test_collection_began_at_the_first_reading_even_if_it_stored_nothing(wired):
    playcounts.take("2026-03-01")
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 3)
    playcounts.take("2026-03-02")

    assert playcounts.baseline_stamp() == "2026-03-01"


def test_an_existing_history_learns_when_collection_began(tmp_path):
    path = tmp_path / "state.db"
    store.connect(path)
    store.connection().execute("DELETE FROM play_collection")
    store.connection().execute(
        "INSERT INTO play_snapshot (taken_on, track_uuid, user_id, username,"
        " play_count) VALUES ('2026-09-06', 'u', 'a', 'alex', 4)")
    store.connection().commit()

    store.connect(path)

    assert store.connection().execute(
        "SELECT began FROM play_collection").fetchone() == ("2026-09-06",)


# Two files carrying one UUID (a duplicate not yet resolved) each have their
# own Navidrome count. Keeping whichever row came last, in no fixed order,
# let 5 and 2 alternate, and every rise back to 5 counted three phantom
# plays (CODE_REVIEW M3).

@pytest.mark.parametrize("order", [(5, 2), (2, 5)])
def test_two_files_with_one_uuid_read_as_the_higher_count(wired, order):
    add_track(wired, "t1", tags=UUID_A)
    add_track(wired, "t2", tags=UUID_A, path="copy.mp3")
    played(wired, "t1", ALEX, order[0])
    played(wired, "t2", ALEX, order[1])

    connection = sqlite3.connect(wired)
    with connection:
        current, _ = playcounts._current(connection)
    connection.close()

    assert current[("uuid-a", ALEX)]["play_count"] == 5


# A forced reading and the timer could run take() together: both compared
# against the same previous reading, and the run log counted the changes
# twice (CODE_REVIEW M29).

def test_two_readings_at_once_are_taken_one_after_the_other(wired, monkeypatch):
    import threading
    import time as clock

    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 3)
    real = playcounts._current
    active, most = [0], [0]
    guard = threading.Lock()

    def slow(connection):
        with guard:
            active[0] += 1
            most[0] = max(most[0], active[0])
        clock.sleep(0.3)
        try:
            return real(connection)
        finally:
            with guard:
                active[0] -= 1

    monkeypatch.setattr(playcounts, "_current", slow)
    threads = [threading.Thread(target=playcounts.take) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    changed = store.connection().execute(
        "SELECT SUM(changed) FROM play_snapshot_run").fetchone()[0]
    assert most[0] == 1, "two readings ran at once"
    assert changed == 1


# --- a counter that comes back into view (2M15) -----------------------------------

def _missing(db, track_id, missing):
    connection = sqlite3.connect(db)
    with connection:
        connection.execute("update media_file set missing = ? where id = ?",
                           (int(missing), track_id))
    connection.close()


def test_a_track_hidden_at_the_first_reading_does_not_count_its_lifetime(wired):
    """Only rows from the first reading were baselines, so a track missing,
    set aside or unstamped then had its whole history counted as new plays
    when it reappeared, dated months before collection began."""
    add_track(wired, "t1", tags=UUID_A)
    add_track(wired, "t2", tags=UUID_B, path="b.mp3")
    played(wired, "t1", ALEX, 100)
    played(wired, "t2", ALEX, 40, when="2026-01-03T09:00:00+00:00")
    _missing(wired, "t2", True)
    playcounts.take("2026-03-01T10:00:00+00:00")
    _missing(wired, "t2", False)
    playcounts.take("2026-03-05T10:00:00+00:00")

    assert playcounts.plays_between("2000-01-01", "2026-12-31", user_id=ALEX) == []

    played(wired, "t2", ALEX, 41, when="2026-03-06T20:00:00+00:00")
    playcounts.take("2026-03-06T20:05:00+00:00")

    plays = playcounts.plays_between("2000-01-01", "2026-12-31", user_id=ALEX)
    assert {row["track_uuid"]: row["plays"] for row in plays} == {"uuid-b": 1}


# --- a reading that fails part way (2M20) ----------------------------------------

def test_a_reading_that_fails_part_way_is_not_committed_by_the_next_write(wired):
    """The snapshot rows went in, the run-log insert failed, nothing rolled
    back - and the next unrelated commit wrote the half-reading to disk."""
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 3)
    store.connection().execute("DROP TABLE play_snapshot_run")

    with pytest.raises(sqlite3.Error):
        playcounts.take("2026-03-01T10:00:00+00:00")
    store.mark_reviewed(1, {"album"}, "by hand")

    path = store._conn.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(path)
    try:
        assert other.execute("SELECT COUNT(*) FROM play_snapshot").fetchone() == (0,)
    finally:
        other.close()


# --- plays the collector has to drop (2L15) -----------------------------------------

def test_plays_on_tracks_without_a_uuid_are_counted_where_people_look(wired, caplog):
    """Dropped at every reading and said only in a forced reading's own
    answer: not in the log, the status or the Listening panel."""
    import logging

    add_track(wired, "t1", tags=None)
    played(wired, "t1", ALEX, 4)

    with caplog.at_level(logging.WARNING):
        playcounts.take("2026-03-01T10:00:00+00:00")

    assert playcounts.coverage(ALEX)["without_uuid"] == 1
    assert any("no UUID" in r.getMessage() for r in caplog.records)


# --- a range typed without leading zeros (2L16) -------------------------------------

@pytest.mark.asyncio
async def test_a_range_without_leading_zeros_finds_the_same_plays(wired):
    """strptime took "2026-3-1" and the raw text was compared against padded
    dates, so the range came back empty."""
    from types import SimpleNamespace


    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 1)
    playcounts.take("2026-03-01T10:00:00+00:00")
    played(wired, "t1", ALEX, 4, when="2026-03-05T20:00:00+00:00")
    playcounts.take("2026-03-05T20:05:00+00:00")
    session = SimpleNamespace(identity=SimpleNamespace(
        user_id=ALEX, username="alex", libraries=[], is_admin=False))

    padded = await listening_routes.playcount_top(start="2026-03-01", end="2026-03-31",
                                      session=session)
    unpadded = await listening_routes.playcount_top(start="2026-3-1", end="2026-3-31",
                                        session=session)

    assert padded["plays"] == 3
    assert unpadded["plays"] == padded["plays"]


# --- an edit made from outside the app (2D6) --------------------------------------

def test_an_in_place_edit_from_another_connection_moves_the_history_version(state_db):
    """Reassigning imported plays to another copy by hand is an UPDATE: the
    rowids and the count stay put, so every cached statistic stayed stale
    until the next play."""
    store.connection().execute(
        "INSERT INTO play_imported (played_at, track_uuid, user_id, username,"
        " plays, source) VALUES ('2025-01-01', 'old', 'u', 'alex', 2, 'lastfm')")
    store.connection().commit()
    before = playcounts.history_version()

    shell = sqlite3.connect(state_db)
    shell.execute("UPDATE play_imported SET track_uuid = 'new'")
    shell.commit()
    shell.close()

    assert playcounts.history_version() != before
