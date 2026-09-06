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


def test_a_snapshot_describes_yesterday_not_today(monkeypatch):
    """A snapshot is a total at the moment it runs, so the only day it can
    describe in full is the one before it. Labelling it today shifted every
    delta a day late."""
    import app.playcounts as pc

    class FakeDatetime(pc.datetime):
        @classmethod
        def now(cls, tz=None):
            return pc.datetime(2026, 3, 10, 0, 5, tzinfo=tz)

    monkeypatch.setattr(pc, "datetime", FakeDatetime)
    assert pc.last_complete_day() == "2026-03-09"
    assert pc.today() == "2026-03-10"


def test_the_target_day_does_not_move_during_a_day(monkeypatch):
    """The earlier version aimed at "yesterday if it is still early". A
    restart in the afternoon then wrote a partial reading of today under
    today's label, marked the day done, and the run after midnight skipped
    it - leaving the day permanently half-closed with nothing to say so."""
    import app.playcounts as pc

    seen = set()
    for hour in (0, 3, 4, 12, 23):
        class FakeDatetime(pc.datetime):
            @classmethod
            def now(cls, tz=None, _h=hour):
                return pc.datetime(2026, 3, 10, _h, 30, tzinfo=tz)

        monkeypatch.setattr(pc, "datetime", FakeDatetime)
        seen.add(pc.last_complete_day())

    assert seen == {"2026-03-09"}, (
        f"the target must be stable across the day, got {seen}")


# --- imported history sits beside the snapshots ----------------------------

def _import(day, track_uuid, user_id, plays, username="alex"):
    ledger.connection().execute(
        "INSERT OR REPLACE INTO play_imported"
        " (day, track_uuid, user_id, username, plays, source)"
        " VALUES (?, ?, ?, ?, ?, 'lastfm')",
        (day, track_uuid, user_id, username, plays))
    ledger.connection().commit()


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
    stored = ledger.connection().execute(
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


def test_status_says_whether_the_nightly_job_is_up_to_date(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 5)

    assert playcounts.status()["up_to_date"] is False
    playcounts.take()          # defaults to the last complete day
    status = playcounts.status()
    assert status["up_to_date"] is True
    assert status["awaiting"] == playcounts.last_complete_day()


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

    assert playcounts.taken_on("2026-03-02") is True, (
        "a quiet day is a real answer, not an incomplete one")
    rows = ledger.connection().execute(
        "select count(*) from play_snapshot where taken_on = '2026-03-02'"
    ).fetchone()[0]
    assert rows == 0, "and it should still not have written a snapshot row"


def test_the_run_log_records_what_happened(wired):
    add_track(wired, "t1", tags=UUID_A)
    played(wired, "t1", ALEX, 7)
    playcounts.take("2026-03-01")

    row = ledger.connection().execute(
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


def test_coverage_is_one_persons_own_history(wired):
    """status() answers for the installation, which is right for a health
    check and wrong for a panel: one account's imported Last.fm history is
    not another account's to read."""
    ledger.connection().execute(
        "insert into play_imported (track_uuid, user_id, username, day, plays,"
        " source) values ('uuid-a', ?, 'alex', '2024-01-01', 40, 'lastfm')",
        (ALEX,))
    ledger.connection().commit()

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
