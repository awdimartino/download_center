"""Listening over time, which is the one thing Navidrome cannot answer.

It keeps a cumulative counter and one date. Plays are the *increase* between
readings, and every test here is about getting that arithmetic right - a
baseline counted as listening would put somebody's whole history into
whichever month the snapshots began.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app import navidrome, overview, store
from conftest import add_track

ALEX = "u-alex"


@pytest.fixture
def identity_with_db(navidrome_db, monkeypatch, identity):
    from app.config import settings
    monkeypatch.setattr(settings, "navidrome_db", navidrome_db)
    return identity


def snapshot(day, track_uuid, count, user_id=ALEX, username="alex",
             played_at=None):
    """One reading of the counter.

    `played_at` is Navidrome's own record of when the track last played,
    which is a different thing from when we looked - and the tests about
    the arithmetic leave it out, so that a delta is attributed to the
    reading that caught it and nothing else is in the way.
    """
    store.connection().execute(
        "insert or replace into play_snapshot"
        " (taken_on, track_uuid, user_id, username, play_count, play_date)"
        " values (?, ?, ?, ?, ?, ?)",
        (day, track_uuid, user_id, username, count, played_at))
    store.connection().commit()


def imported(when, track_uuid, plays, user_id=ALEX):
    """`when` is a bare date for history whose time was never recovered, or
    a full timestamp for a scrobble that has one."""
    store.connection().execute(
        "insert or replace into play_imported"
        " (played_at, track_uuid, user_id, username, source, plays)"
        " values (?, ?, ?, 'alex', 'lastfm', ?)",
        (when, track_uuid, user_id, plays))
    store.connection().commit()


def tagged(uuid: str) -> str:
    """The identity tag as Navidrome stores it - a JSON blob per file."""
    return json.dumps({"navidrome_uuid": [{"value": uuid}]})


def this_month():
    now = datetime.now(UTC)
    return f"{now.year:04d}-{now.month:02d}"


# --- the arithmetic ---------------------------------------------------------

def test_plays_are_the_increase_between_readings(state_db):
    snapshot("2026-09-01", "t1", 10)
    snapshot("2026-09-02", "t1", 13)

    plays = overview._increments(ALEX)

    assert plays == [("2026-09-02", "t1", 3)]


def test_the_first_reading_is_a_baseline_not_listening(state_db):
    """It is whatever the counter already said the day this started
    watching. Counting it would put a lifetime into one month."""
    snapshot("2026-09-01", "t1", 500)

    assert overview._increments(ALEX) == []


def test_a_counter_that_fell_is_not_negative_listening(state_db):
    """A re-import or a reset lowers it. That is not minus four plays."""
    snapshot("2026-09-01", "t1", 10)
    snapshot("2026-09-02", "t1", 4)
    snapshot("2026-09-03", "t1", 6)

    assert overview._increments(ALEX) == [("2026-09-03", "t1", 2)]


def test_tracks_do_not_bleed_into_each_other(state_db):
    """Each track has its own baseline; a shared running total would read
    the second track's first count as a jump from the first track's."""
    snapshot("2026-09-01", "t1", 100)
    snapshot("2026-09-02", "t1", 101)
    snapshot("2026-09-01", "t2", 700)
    snapshot("2026-09-02", "t2", 702)

    plays = sorted(overview._increments(ALEX))

    assert plays == [("2026-09-02", "t1", 1), ("2026-09-02", "t2", 2)]


def test_imported_history_is_already_plays_not_a_total(state_db):
    imported("2024-03-04", "t1", 7)
    assert overview._increments(ALEX) == [("2024-03-04", "t1", 7)]


def test_another_persons_listening_is_not_counted(state_db):
    snapshot("2026-09-01", "t1", 0, user_id="u-kelly", username="kelly")
    snapshot("2026-09-02", "t1", 9, user_id="u-kelly", username="kelly")

    assert overview._increments(ALEX) == []


# --- the months -------------------------------------------------------------

def test_every_month_is_present_even_the_silent_ones(state_db,
                                                      identity_with_db):
    """Built from the calendar, not the data - or a month nobody listened
    in vanishes and makes the months either side look adjacent."""
    heard = overview.listening(identity_with_db)

    assert len(heard["months"]) == overview.MONTHS
    assert [m["month"] for m in heard["months"]] == sorted(
        m["month"] for m in heard["months"]), "oldest first"
    assert heard["months"][-1]["month"] == this_month()


def test_a_months_listening_is_the_sum_of_its_increases(state_db,
                                                         identity_with_db):
    month = this_month()
    snapshot(f"{month}-01", "t1", 0)
    snapshot(f"{month}-02", "t1", 4)
    snapshot(f"{month}-03", "t1", 9)

    heard = overview.listening(identity_with_db)

    assert heard["this_month"] == 9
    assert heard["months"][-1]["plays"] == 9


def test_imported_and_snapshot_history_both_count(state_db,
                                                   identity_with_db):
    month = this_month()
    snapshot(f"{month}-01", "t1", 0)
    snapshot(f"{month}-02", "t1", 3)
    imported(f"{month}-05", "t2", 4)

    assert overview.listening(identity_with_db)["this_month"] == 7


def test_nothing_recorded_is_zeroes_not_an_error(state_db, identity_with_db):
    heard = overview.listening(identity_with_db)

    assert heard["total_plays"] == 0
    assert heard["top_artists"] == []
    assert all(m["plays"] == 0 for m in heard["months"])


# --- the artists ------------------------------------------------------------

def test_artists_are_summed_across_their_tracks(state_db, identity_with_db,
                                                 navidrome_db):
    month = this_month()
    for n, (uuid_, artist) in enumerate(
            [("t1", "Aphex Twin"), ("t2", "Aphex Twin"), ("t3", "Burial")]):
        add_track(navidrome_db, f"m{n}", artist=artist,
                  tags=tagged(uuid_))
        snapshot(f"{month}-01", uuid_, 0)
        snapshot(f"{month}-02", uuid_, 2 if artist == "Aphex Twin" else 1)

    heard = overview.listening(identity_with_db)

    assert heard["top_artists"][0] == {"artist": "Aphex Twin", "plays": 4}
    assert heard["artists_heard"] == 2


def test_a_track_that_has_left_the_library_has_no_artist_to_credit(
        state_db, identity_with_db):
    """Its plays still happened and still count towards the month; there is
    simply nobody to attribute them to."""
    month = this_month()
    snapshot(f"{month}-01", "gone", 0)
    snapshot(f"{month}-02", "gone", 5)

    heard = overview.listening(identity_with_db)

    assert heard["this_month"] == 5
    assert heard["top_artists"] == []


def test_the_artist_list_is_capped(state_db, identity_with_db, navidrome_db):
    month = this_month()
    for n in range(overview.TOP_ARTISTS + 3):
        add_track(navidrome_db, f"m{n}", artist=f"Artist {n}",
                  tags=tagged(f"t{n}"))
        snapshot(f"{month}-01", f"t{n}", 0)
        snapshot(f"{month}-02", f"t{n}", n + 1)

    heard = overview.listening(identity_with_db)

    assert len(heard["top_artists"]) == overview.TOP_ARTISTS
    plays = [a["plays"] for a in heard["top_artists"]]
    assert plays == sorted(plays, reverse=True), "most played first"


# --- the collection ---------------------------------------------------------

def test_the_collection_is_counted_not_walked(state_db, identity_with_db,
                                               navidrome_db):
    for n in range(3):
        add_track(navidrome_db, f"a{n}", album="Abbey Road",
                  album_artist="The Beatles", path=f"b/a/{n}.mp3")
    add_track(navidrome_db, "r1", album="Revolver",
              album_artist="The Beatles", path="b/r/1.mp3")

    counted = overview.collection(identity_with_db)

    assert counted == {"tracks": 4, "albums": 2, "available": True}


def test_a_library_that_cannot_be_read_says_so(identity, monkeypatch,
                                                tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "navidrome_db", tmp_path / "gone.db")

    assert overview.collection(identity)["available"] is False


def test_somebody_with_no_library_counts_nothing(state_db):
    nobody = navidrome.Identity(user_id="u-x", username="x", is_admin=False,
                                token="t", subsonic_token="", subsonic_salt="")
    assert overview.collection(nobody)["tracks"] == 0


# --- the whole payload ------------------------------------------------------

def test_the_overview_survives_an_unreadable_navidrome(identity, monkeypatch,
                                                        tmp_path, state_db):
    """The landing page is the first thing anybody sees. It does not get to
    be a 500 because a mount is missing."""
    from app.config import settings
    monkeypatch.setattr(settings, "navidrome_db", tmp_path / "gone.db")

    page = overview.overview(identity)

    assert page["username"] == "alex"
    assert page["collection"]["available"] is False
    assert page["listening"]["months"] == [] or page["listening"]["total_plays"] == 0


# --- the year ---------------------------------------------------------------

def test_the_year_counts_distinct_tracks_not_plays(state_db, identity_with_db,
                                                    navidrome_db):
    year = datetime.now(UTC).year
    for n, uuid_ in enumerate(["t1", "t2"]):
        add_track(navidrome_db, f"m{n}", duration=300.0,
                  tags=tagged(uuid_))
        snapshot(f"{year}-01-01", uuid_, 0)
    snapshot(f"{year}-01-02", "t1", 5)
    snapshot(f"{year}-01-02", "t2", 1)

    counted = overview.listening(identity_with_db)["year"]

    assert counted["plays"] == 6
    assert counted["tracks"] == 2, "two tracks, six plays"
    assert counted["seconds"] == 6 * 300


def test_last_year_is_not_this_year(state_db, identity_with_db):
    year = datetime.now(UTC).year
    snapshot(f"{year - 1}-06-01", "t1", 0)
    snapshot(f"{year - 1}-06-02", "t1", 9)

    assert overview.listening(identity_with_db)["year"]["plays"] == 0


def test_time_listened_needs_the_track_to_still_exist(state_db,
                                                       identity_with_db):
    """A track that has left has no duration to multiply by. The play still
    counts; the hours cannot."""
    year = datetime.now(UTC).year
    snapshot(f"{year}-02-01", "gone", 0)
    snapshot(f"{year}-02-02", "gone", 3)

    counted = overview.listening(identity_with_db)["year"]

    assert counted["plays"] == 3
    assert counted["seconds"] == 0


def test_the_busiest_month_is_the_biggest_one(state_db, identity_with_db):
    months = overview._months_back(overview.MONTHS)
    snapshot(f"{months[-1]}-01", "t1", 0)
    snapshot(f"{months[-1]}-02", "t1", 2)
    snapshot(f"{months[-3]}-01", "t2", 0)
    snapshot(f"{months[-3]}-02", "t2", 40)

    busiest = overview.listening(identity_with_db)["busiest_month"]

    assert busiest == {"month": months[-3], "plays": 40}


# --- the headline -----------------------------------------------------------

def test_every_highlight_has_all_three_parts(state_db, identity_with_db,
                                              navidrome_db):
    year = datetime.now(UTC).year
    add_track(navidrome_db, "m1", artist="Burial", duration=400.0,
              tags=tagged("t1"))
    add_track(navidrome_db, "m2", artist="Aphex Twin", duration=400.0,
              tags=tagged("t2"))
    snapshot(f"{year}-01-01", "t1", 0)
    snapshot(f"{year}-01-02", "t1", 30)
    snapshot(f"{year}-01-01", "t2", 0)
    snapshot(f"{year}-01-02", "t2", 5)

    page = overview.overview(identity_with_db)

    assert len(page["highlights"]) >= 4
    for fact in page["highlights"]:
        assert set(fact) == {"lead", "value", "tail"}
        assert all(str(part).strip() for part in fact.values())


def test_a_silent_account_is_not_told_it_played_nothing(state_db,
                                                         identity_with_db,
                                                         navidrome_db):
    """Facts about zero are worse than no fact. With music on disk and no
    listening yet, only the collection has anything to say."""
    add_track(navidrome_db, "m1")

    page = overview.overview(identity_with_db)

    assert [f["lead"] for f in page["highlights"]] == ["Your library holds"]


def test_a_brand_new_install_offers_no_headline_at_all(state_db,
                                                        identity_with_db):
    assert overview.overview(identity_with_db)["highlights"] == []


def test_the_headline_never_reads_one_tracks(state_db, identity_with_db,
                                              navidrome_db):
    year = datetime.now(UTC).year
    add_track(navidrome_db, "m1", tags=tagged("t1"))
    snapshot(f"{year}-03-01", "t1", 0)
    snapshot(f"{year}-03-02", "t1", 1)

    said = " ".join(f"{f['lead']} {f['value']} {f['tail']}"
                    for f in overview.overview(identity_with_db)["highlights"])

    assert "1 different tracks" not in said
    assert "1 different track" in said


@pytest.mark.parametrize("seconds, expected", [
    (0, "1 minute"), (90, "2 minutes"), (3600, "1 hour"),
    (7200, "2 hours"), (360_000, "100 hours"),
])
def test_time_reads_in_the_coarsest_useful_unit(seconds, expected):
    assert overview._hours(seconds) == expected


def test_a_month_is_named_not_numbered():
    assert overview._month_name("2025-09") == "September 2025"
    assert overview._month_name("") == ""


# --- when a play actually happened ------------------------------------------
# The reason the counts are read every few minutes rather than nightly:
# Navidrome stores the moment of a track's most recent play beside its
# running total, so a reading that catches the count rising by one has that
# play's exact time.

def test_a_play_is_dated_by_when_it_played_not_when_we_looked(state_db):
    snapshot("2026-09-25T17:05:00+00:00", "t1", 4)
    snapshot("2026-09-25T17:10:00+00:00", "t1", 5,
             played_at="2026-09-25 17:09:46.018+00:00")

    assert overview._increments(ALEX) == [
        ("2026-09-25T17:09:46+00:00", "t1", 1)]


def test_the_reading_time_stands_in_when_the_play_has_no_time(state_db):
    snapshot("2026-09-25T17:05:00+00:00", "t1", 4)
    snapshot("2026-09-25T17:10:00+00:00", "t1", 5)

    assert overview._increments(ALEX) == [
        ("2026-09-25T17:10:00+00:00", "t1", 1)]


def test_junk_in_the_play_date_does_not_lose_the_play(state_db):
    snapshot("2026-09-25T17:05:00+00:00", "t1", 4)
    snapshot("2026-09-25T17:10:00+00:00", "t1", 5, played_at="not a date")

    assert overview._increments(ALEX) == [
        ("2026-09-25T17:10:00+00:00", "t1", 1)]


def test_two_plays_in_one_interval_share_the_one_recorded_time(state_db):
    """Only the last of them has a timestamp. They are smeared onto it
    rather than dropped - the plays are real, and at a five-minute cadence
    the error is bounded by five minutes."""
    snapshot("2026-09-25T17:05:00+00:00", "t1", 4)
    snapshot("2026-09-25T17:10:00+00:00", "t1", 6,
             played_at="2026-09-25 17:09:46+00:00")

    assert overview._increments(ALEX) == [
        ("2026-09-25T17:09:46+00:00", "t1", 2)]


def test_rows_from_when_this_ran_nightly_still_read(state_db):
    """A bare date, a timestamp and a converted play time all sort and
    bucket alike, so the two years already on disk need no migration."""
    snapshot("2026-09-01", "t1", 10)
    snapshot("2026-09-02", "t1", 12)
    snapshot("2026-09-25T17:10:00+00:00", "t1", 13,
             played_at="2026-09-25 17:09:46+00:00")

    assert overview._increments(ALEX) == [
        ("2026-09-02", "t1", 2),
        ("2026-09-25T17:09:46+00:00", "t1", 1)]


def test_an_evening_play_counts_in_the_local_month_not_the_utc_one(
        state_db, monkeypatch):
    """22:00 on 30 September in New York is 02:00 on 1 October in UTC.
    Bucketing the raw value would move the last hours of every month into
    the month after it."""
    from app.config import settings
    monkeypatch.setattr(settings, "play_day_timezone", "America/New_York")
    snapshot("2026-10-01T01:00:00+00:00", "t1", 4)
    snapshot("2026-10-01T03:00:00+00:00", "t1", 5,
             played_at="2026-10-01 02:00:00+00:00")

    when, _track, _n = overview._increments(ALEX)[0]

    assert when.startswith("2026-09-30T22:00:00"), when
    assert when[:7] == "2026-09"


# --- hourly distribution ------------------------------------------------

def test_plays_are_bucketed_by_the_hour_they_happened(state_db):
    snapshot("2026-09-25T09:05:00+00:00", "t1", 1)
    snapshot("2026-09-25T09:10:00+00:00", "t1", 2,
             played_at="2026-09-25 09:07:00+00:00")
    snapshot("2026-09-25T21:05:00+00:00", "t2", 4)
    snapshot("2026-09-25T21:10:00+00:00", "t2", 5,
             played_at="2026-09-25 21:08:00+00:00")

    hourly = overview.hourly_distribution(ALEX, "2026-09-25", "2026-09-25")

    assert len(hourly) == 24
    assert [h["hour"] for h in hourly] == list(range(24))
    assert hourly[9]["plays"] == 1
    assert hourly[21]["plays"] == 1
    assert sum(h["plays"] for h in hourly) == 2


def test_a_play_with_no_known_time_is_not_bucketed(state_db):
    """A bare-date row has a day but no hour - it cannot be placed on this
    chart, so it is left out rather than guessed at."""
    snapshot("2026-09-24", "t1", 3)
    snapshot("2026-09-25", "t1", 6)

    hourly = overview.hourly_distribution(ALEX, "2026-09-25", "2026-09-25")

    assert sum(h["plays"] for h in hourly) == 0


def test_hourly_distribution_is_scoped_to_the_range(state_db):
    snapshot("2026-08-01T10:00:00+00:00", "t1", 1)
    snapshot("2026-08-02T10:00:00+00:00", "t1", 2,
             played_at="2026-08-02 10:00:00+00:00")

    hourly = overview.hourly_distribution(ALEX, "2026-09-01", "2026-09-30")

    assert sum(h["plays"] for h in hourly) == 0


# --- sessions -------------------------------------------------------------

def test_consecutive_plays_form_one_session(state_db, identity_with_db,
                                            navidrome_db):
    add_track(navidrome_db, "m1", duration=180.0, tags=tagged("t1"))
    snapshot("2026-09-25T20:00:00+00:00", "t1", 1)
    snapshot("2026-09-25T20:05:00+00:00", "t1", 2,
             played_at="2026-09-25 20:04:00+00:00")
    snapshot("2026-09-25T20:10:00+00:00", "t1", 3,
             played_at="2026-09-25 20:07:00+00:00")

    session = overview.longest_session(ALEX, "2026-09-25", "2026-09-25")

    assert session["count"] == 1
    assert session["tracks"] == 1, "one song, played twice"
    assert session["plays"] == 2
    # 20:04 to 20:07 is 180 seconds, plus the last track's own 180 seconds.
    assert session["seconds"] == 360


def test_a_long_gap_starts_a_new_session(state_db):
    snapshot("2026-09-25T09:00:00+00:00", "t1", 1)
    snapshot("2026-09-25T09:05:00+00:00", "t1", 2,
             played_at="2026-09-25 09:01:00+00:00")
    snapshot("2026-09-25T21:00:00+00:00", "t1", 3,
             played_at="2026-09-25 21:00:00+00:00")

    session = overview.longest_session(ALEX, "2026-09-25", "2026-09-25")

    assert session["count"] == 2


def test_the_longest_session_is_reported(state_db):
    # A short session at 09:00, two minutes end to end...
    snapshot("2026-09-25T09:00:00+00:00", "t1", 1)
    snapshot("2026-09-25T09:05:00+00:00", "t1", 2,
             played_at="2026-09-25 09:01:00+00:00")
    snapshot("2026-09-25T09:10:00+00:00", "t1", 3,
             played_at="2026-09-25 09:03:00+00:00")
    # ...and a longer one at 21:00, twenty minutes end to end.
    snapshot("2026-09-25T21:00:00+00:00", "t2", 1)
    snapshot("2026-09-25T21:15:00+00:00", "t2", 2,
             played_at="2026-09-25 21:05:00+00:00")
    snapshot("2026-09-25T21:30:00+00:00", "t2", 3,
             played_at="2026-09-25 21:25:00+00:00")

    session = overview.longest_session(ALEX, "2026-09-25", "2026-09-25")

    assert session["count"] == 2
    assert session["start"].startswith("2026-09-25T21:05:00")


def test_sessions_with_no_plays_report_nothing(state_db):
    assert overview.longest_session(ALEX, "2026-09-25", "2026-09-25") == {
        "seconds": 0, "tracks": 0, "plays": 0,
        "start": "", "end": "", "count": 0}


def test_sessions_are_scoped_to_where_they_started(state_db):
    """Built once over the whole history and filtered afterwards - cutting
    the timeline to the range first would split a session that straddles a
    boundary and report a shorter one on each side."""
    snapshot("2026-08-31T23:50:00+00:00", "t1", 1)
    snapshot("2026-09-01T00:05:00+00:00", "t1", 2,
             played_at="2026-08-31 23:55:00+00:00")

    august = overview.longest_session(ALEX, "2026-08-01", "2026-08-31")
    september = overview.longest_session(ALEX, "2026-09-01", "2026-09-30")

    assert august["count"] == 1
    assert september["count"] == 0


def test_top_album_is_the_album_with_most_plays_and_its_loudest_track():
    # Two albums under the same artist name but different album artists must
    # not merge, and the cover comes from the album's own most played track.
    recent = {"a1": 5, "a2": 4, "a3": 3, "b1": 6}
    named = {
        "a1": {"album": "Kid A", "album_artist": "Radiohead", "id": "mf-1"},
        "a2": {"album": "Kid A", "album_artist": "Radiohead", "id": "mf-2"},
        "a3": {"album": "Kid A", "album_artist": "Radiohead", "id": "mf-3"},
        "b1": {"album": "Kid A", "album_artist": "Someone Else", "id": "mf-9"},
    }
    top = overview._top_album(recent, named)
    assert top == {"artist": "Radiohead", "album": "Kid A", "plays": 12,
                   "cover_track_id": "mf-1"}


def test_top_album_is_none_when_nothing_has_an_album():
    named = {"x": {"album": "", "album_artist": "", "id": "mf-x"}}
    assert overview._top_album({"x": 3}, named) is None
    assert overview._top_album({}, {}) is None
