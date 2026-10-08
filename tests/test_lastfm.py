"""Matching Last.fm scrobbles to tracks in the library.

A backfill that quietly matched sixty per cent would poison every statistic
built on it afterwards and never say so, which is why the reporting is as
much the feature as the writing.
"""

from __future__ import annotations

import json
import sys

import pytest

from app import lastfm, store


@pytest.mark.parametrize("a, b", [
    ("Radiohead", "radiohead"),
    ("Sigur Rós", "sigur rós"),
    ("Godspeed You! Black Emperor", "Godspeed You Black Emperor"),
    ("Anohni and the Johnsons", "Anohni & the Johnsons"),
])
def test_typography_does_not_stop_a_match(a, b):
    """A scrobble and a tag disagree about punctuation far more often than
    about the words."""
    assert lastfm.normalise(a) == lastfm.normalise(b)


def test_featured_artists_are_dropped():
    assert lastfm.normalise("Song (feat. Someone)") == lastfm.normalise("Song")
    assert lastfm.normalise("Song ft. Someone") == lastfm.normalise("Song")


def test_genuinely_different_titles_stay_different():
    assert lastfm.normalise("Let Down") != lastfm.normalise("Lucky")


def _index(pairs):
    return {(lastfm.normalise(a), lastfm.normalise(t)): u for a, t, u in pairs}


def test_a_clean_match_becomes_a_day_row():
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    # 2026-02-14 12:00 UTC
    planned = lastfm.plan("alex", "u-alex",
                          [("Radiohead", "Let Down", 1771070400)], index, None)
    assert planned["matched"] == 1
    assert planned["unmatched"] == 0
    assert len(planned["rows"]) == 1


def test_several_scrobbles_of_one_track_on_one_day_are_summed():
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    played = [("Radiohead", "Let Down", 1771070400 + n * 300) for n in range(3)]
    planned = lastfm.plan("alex", "u-alex", played, index, None)
    assert planned["matched"] == 3
    assert [row[2] for row in planned["rows"]] == [3]


def test_two_candidates_are_ambiguous_not_guessed():
    """A single and its album appearance are two files of one recording.
    Choosing by row order would be arbitrary and silent."""
    index = _index([("Radiohead", "Let Down", ["uuid-a", "uuid-b"])])
    planned = lastfm.plan("alex", "u-alex",
                          [("Radiohead", "Let Down", 1771070400)], index, None)
    assert planned["ambiguous"] == 1
    assert planned["matched"] == 0
    assert planned["rows"] == []
    assert planned["ambiguous_examples"]


def test_a_track_not_in_the_library_is_reported():
    planned = lastfm.plan("alex", "u-alex",
                          [("Some Band", "Some Song", 1771070400)], {}, None)
    assert planned["unmatched"] == 1
    assert planned["unmatched_examples"][0][0] == "Some Band - Some Song"


def test_scrobbles_from_the_snapshot_era_are_left_alone():
    """Snapshots already count these. Importing over the top would double
    every play on the handover day."""
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    played = [
        ("Radiohead", "Let Down", 1771070400),   # 2026-02-14
        ("Radiohead", "Let Down", 1773748800),   # 2026-03-17
    ]
    planned = lastfm.plan("alex", "u-alex", played, index, before="2026-03-01")

    assert planned["after_snapshots_began"] == 1
    assert planned["matched"] == 1
    assert all(row[0] < "2026-03-01" for row in planned["rows"])


def test_the_report_accounts_for_every_scrobble():
    """Matched + ambiguous + unmatched + overlapping must equal the total, or
    something was dropped without saying so."""
    index = _index([("A", "One", ["u1"]), ("B", "Two", ["u2", "u3"])])
    played = [
        ("A", "One", 1771070400),
        ("B", "Two", 1771070400),
        ("C", "Three", 1771070400),
        ("A", "One", 1773748800),
    ]
    planned = lastfm.plan("alex", "u-alex", played, index, before="2026-03-01")
    total = (planned["matched"] + planned["ambiguous"]
             + planned["unmatched"] + planned["after_snapshots_began"])
    assert total == planned["scrobbles"] == 4


@pytest.mark.parametrize("a, b", [
    ("Simon & Garfunkel", "Simon and Garfunkel"),
    ("Florence + the Machine", "Florence and the Machine"),
    ("Belle & Sebastian", "Belle and Sebastian"),
])
def test_ampersand_and_the_word_and_are_the_same_band(a, b):
    """One of the commonest differences between what a scrobbler sent and
    what the tag says. Stripping & as punctuation would leave the two
    spellings permanently unmatchable."""
    assert lastfm.normalise(a) == lastfm.normalise(b)


# --- surviving a flaky API --------------------------------------------------

def test_a_server_error_is_retried(monkeypatch):
    """One 500 part way through a quarter of an hour of requests threw away
    every page already fetched. Same mistake as treating a rate-limited
    search as "no results", and more expensive."""
    import urllib.error
    calls = {"n": 0}

    def flaky(request, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(
                "u", 500, "Internal Server Error", {}, None)
        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"ok": true}'
        return Response()

    monkeypatch.setattr(lastfm.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(lastfm.time, "sleep", lambda s: None)
    monkeypatch.setattr(lastfm.json, "load", lambda r: {"ok": True})

    assert lastfm._call("user.getRecentTracks", api_key="k") == {"ok": True}
    assert calls["n"] == 3


def test_a_client_error_is_not_retried(monkeypatch):
    """A 4xx means the request was wrong. Repeating it cannot help."""
    import urllib.error
    calls = {"n": 0}

    def refused(request, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    monkeypatch.setattr(lastfm.urllib.request, "urlopen", refused)
    monkeypatch.setattr(lastfm.time, "sleep", lambda s: None)

    with pytest.raises(lastfm.LastfmError, match="403"):
        lastfm._call("user.getRecentTracks", api_key="k")
    assert calls["n"] == 1


def test_an_error_in_the_body_is_not_retried(monkeypatch):
    """Last.fm answering "invalid parameters" is not a transient failure."""
    calls = {"n": 0}

    def answered(request, timeout=None):
        calls["n"] += 1
        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return Response()

    monkeypatch.setattr(lastfm.urllib.request, "urlopen", answered)
    monkeypatch.setattr(lastfm.time, "sleep", lambda s: None)
    monkeypatch.setattr(lastfm.json, "load",
                        lambda r: {"error": 6, "message": "No user"})

    with pytest.raises(lastfm.LastfmError, match="No user"):
        lastfm._call("user.getInfo", api_key="k")
    assert calls["n"] == 1


def test_it_gives_up_eventually(monkeypatch):
    import urllib.error

    def always_500(request, timeout=None):
        raise urllib.error.HTTPError("u", 503, "Unavailable", {}, None)

    monkeypatch.setattr(lastfm.urllib.request, "urlopen", always_500)
    monkeypatch.setattr(lastfm.time, "sleep", lambda s: None)

    with pytest.raises(lastfm.LastfmError, match="attempts"):
        lastfm._call("user.getRecentTracks", api_key="k")


def test_rows_are_labelled_with_the_navidrome_name(monkeypatch):
    """A snapshot row saying "alex" beside an imported row saying
    "argyle_nz" for the same user_id reads like two different listeners."""
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    planned = lastfm.plan("alex", "u-alex",
                          [("Radiohead", "Let Down", 1771070400)], index, None)
    assert planned["username"] == "alex"


# --- auditing the misses ----------------------------------------------------

def test_a_near_miss_is_surfaced_with_its_score():
    """"Not in the library" is a claim that has to be checkable. A high score
    means the matcher missed something it should have caught; a low one means
    the track really is absent."""
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    planned = lastfm.plan("alex", "u-alex",
                          [("Radiohead", "Let Down (Remastered)", 1771070400)],
                          index, None)
    assert planned["unmatched"] == 1

    audit = lastfm.audit_unmatched(planned, index)
    assert len(audit) == 1
    assert audit[0]["scrobbled"] == "Radiohead - Let Down (Remastered)"
    # Shown normalised: that is what the matcher actually compared, which is
    # the useful thing to see when judging why a match was missed.
    assert "let down" in audit[0]["nearest"]
    assert audit[0]["score"] >= 85, "a remaster suffix should read as close"


def test_a_genuine_absence_scores_low():
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    planned = lastfm.plan("alex", "u-alex",
                          [("Some Other Band", "Unrelated Song", 1771070400)],
                          index, None)
    audit = lastfm.audit_unmatched(planned, index)
    assert audit[0]["score"] < 85


def test_the_audit_is_ordered_by_how_often_it_was_scrobbled():
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    played = ([("Band A", "Rare", 1771070400)]
              + [("Band B", "Often", 1771070400 + n) for n in range(5)])
    planned = lastfm.plan("alex", "u-alex", played, index, None)
    audit = lastfm.audit_unmatched(planned, index)
    assert audit[0]["scrobbled"] == "Band B - Often"
    assert audit[0]["scrobbles"] == 5


def test_the_audit_flag_is_actually_wired_up(tmp_path, monkeypatch, capsys):
    """The flag existed and did nothing for one deploy: argparse knew it,
    main() never called audit_unmatched, and the tests above passed because
    they exercise the function rather than the command. A flag that is
    silently ignored is the exact failure this codebase keeps producing."""
    import app.lastfm as lf

    target = tmp_path / "unmatched.txt"
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    monkeypatch.setattr(lf, "_credentials", lambda user: ("k", "s", "sk"))
    monkeypatch.setattr(lf, "username_for", lambda *a: "argyle_nz")
    monkeypatch.setattr(lf, "library_index", lambda c: index)
    monkeypatch.setattr(lf, "scrobbles",
                        lambda *a, **k: [("Nobody", "Nothing", 1771070400)])
    monkeypatch.setattr(lf.store, "connect", lambda p: None)

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args=()):
            class R:
                def fetchone(self_inner): return ("u-alex",)
            return R()
    monkeypatch.setattr(lf.navidrome, "open_db", lambda: FakeConn())
    monkeypatch.setattr(lf.store, "connection", lambda: FakeConn())
    monkeypatch.setattr(lf.playcounts, "baseline_stamp", lambda: None)
    monkeypatch.setattr(sys, "argv",
                        ["lastfm", "alex", "--audit", str(target)])

    assert lf.main() == 0
    assert target.exists(), "--audit was accepted and did nothing"
    body = target.read_text(encoding="utf-8")
    assert "Nobody - Nothing" in body
    assert "wrote 1 unmatched titles" in capsys.readouterr().out


# --- giving the imported history its clock back -----------------------------
# The rows in play_imported record decisions, some made by hand. The backfill
# joins fresh scrobbles onto those decisions rather than making them again,
# so what these check is that it never changes *which* plays are recorded -
# only when.

ALEX = "u-alex"
NOON = 1771070400          # 2026-02-14 12:00:00 UTC


def _row(when, track_uuid, plays, user_id=ALEX, source="lastfm"):
    store.connection().execute(
        "insert or replace into play_imported"
        " (played_at, track_uuid, user_id, username, plays, source)"
        " values (?, ?, ?, 'alex', ?, ?)",
        (when, track_uuid, user_id, plays, source))
    store.connection().commit()


def _plays():
    return store.connection().execute(
        "select played_at, track_uuid, plays from play_imported"
        " order by played_at, track_uuid").fetchall()


def _total():
    return store.connection().execute(
        "select coalesce(sum(plays), 0) from play_imported").fetchone()[0]


def test_a_day_row_becomes_the_times_it_actually_happened(state_db):
    _row("2026-02-14", "uuid-a", 2)
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    played = [("Radiohead", "Let Down", NOON),
              ("Radiohead", "Let Down", NOON + 3600)]

    planned = lastfm.plan_times(ALEX, played, index)
    assert planned["resolved_rows"] == 1
    assert planned["unresolved_rows"] == 0

    lastfm.write_times(planned, "alex")

    assert _plays() == [("2026-02-14T12:00:00+00:00", "uuid-a", 1),
                        ("2026-02-14T13:00:00+00:00", "uuid-a", 1)]
    assert _total() == 2


def test_the_number_of_plays_never_changes(state_db):
    _row("2026-02-14", "uuid-a", 3)
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    played = [("Radiohead", "Let Down", NOON + n * 60) for n in range(3)]

    before = _total()
    lastfm.write_times(lastfm.plan_times(ALEX, played, index), "alex")

    assert _total() == before


def test_a_row_whose_scrobbles_are_missing_is_left_alone(state_db):
    """Nothing is inferred. A day that cannot be resolved keeps its date."""
    _row("2026-02-14", "uuid-a", 4)
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    played = [("Radiohead", "Let Down", NOON)]          # one, not four

    planned = lastfm.plan_times(ALEX, played, index)
    assert planned["resolved_rows"] == 0
    assert planned["unresolved_rows"] == 1

    lastfm.write_times(planned, "alex")

    assert _plays() == [("2026-02-14", "uuid-a", 4)]


def test_scrobbles_are_not_handed_to_two_tracks_at_once(state_db):
    """A normalised artist and title can name several tracks - a single and
    its album appearance. Without a claim, both rows would take the same
    scrobbles and the day would report twice the listening."""
    _row("2026-02-14", "uuid-a", 1)
    _row("2026-02-14", "uuid-b", 1)
    index = _index([("Radiohead", "Let Down", ["uuid-a", "uuid-b"])])
    played = [("Radiohead", "Let Down", NOON),
              ("Radiohead", "Let Down", NOON + 600)]

    planned = lastfm.plan_times(ALEX, played, index)

    assert planned["resolved_rows"] == 2
    lastfm.write_times(planned, "alex")
    assert _total() == 2
    assert {row[0] for row in _plays()} == {"2026-02-14T12:00:00+00:00",
                                            "2026-02-14T12:10:00+00:00"}


def test_two_scrobbles_in_the_same_second_do_not_collide(state_db):
    """The primary key is (played_at, track, user, source). Inserted one by
    one, the second would replace the first and a play would vanish."""
    _row("2026-02-14", "uuid-a", 2)
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    played = [("Radiohead", "Let Down", NOON),
              ("Radiohead", "Let Down", NOON)]

    lastfm.write_times(lastfm.plan_times(ALEX, played, index), "alex")

    assert _plays() == [("2026-02-14T12:00:00+00:00", "uuid-a", 2)]
    assert _total() == 2


def test_running_it_twice_changes_nothing_the_second_time(state_db):
    _row("2026-02-14", "uuid-a", 1)
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    played = [("Radiohead", "Let Down", NOON)]

    lastfm.write_times(lastfm.plan_times(ALEX, played, index), "alex")
    after_once = _plays()

    again = lastfm.plan_times(ALEX, played, index)
    assert again["day_rows"] == 0, "a timestamped row is not a day row"
    lastfm.write_times(again, "alex")

    assert _plays() == after_once


def test_another_persons_history_is_not_touched(state_db):
    _row("2026-02-14", "uuid-a", 1)
    _row("2026-02-14", "uuid-a", 1, user_id="u-kelly")
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])

    lastfm.write_times(
        lastfm.plan_times(ALEX, [("Radiohead", "Let Down", NOON)], index),
        "alex")

    kelly = store.connection().execute(
        "select played_at, plays from play_imported where user_id = 'u-kelly'"
    ).fetchall()
    assert kelly == [("2026-02-14", 1)]


def test_a_play_is_dated_in_the_listeners_zone_not_utc(state_db, monkeypatch):
    """23:00 on 14 February in New York is 04:00 on the 15th in UTC. The
    row it belongs to is the 14th's, and bucketing the raw value would fail
    to find it at all."""
    from app.config import settings
    monkeypatch.setattr(settings, "play_day_timezone", "America/New_York")
    _row("2026-02-14", "uuid-a", 1)
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    late = NOON + 16 * 3600       # 2026-02-15 04:00 UTC = 14th 23:00 EST

    planned = lastfm.plan_times(ALEX, [("Radiohead", "Let Down", late)], index)

    assert planned["resolved_rows"] == 1
    lastfm.write_times(planned, "alex")
    assert _plays() == [("2026-02-14T23:00:00-05:00", "uuid-a", 1)]


def test_a_write_that_would_change_the_total_is_refused(state_db):
    """The safety property, checked directly: this may change when a play
    is recorded, never whether it is."""
    _row("2026-02-14", "uuid-a", 1)
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    planned = lastfm.plan_times(ALEX, [("Radiohead", "Let Down", NOON)], index)
    # A plan that claims one play but carries two timestamps.
    planned["resolved"] = [("2026-02-14", "uuid-a", 1,
                            ["2026-02-14T12:00:00+00:00",
                             "2026-02-14T13:00:00+00:00"])]

    with pytest.raises(RuntimeError, match="refusing to commit"):
        lastfm.write_times(planned, "alex")

    assert _plays() == [("2026-02-14", "uuid-a", 1)], "rolled back"


# --- the handover, and running it twice -------------------------------------
# The cutoff compared a scrobble's day with the first reading as text, so a
# timestamped first reading let the whole handover day in - plays after the
# reading included, which the snapshots count too. And a plain import after
# --times wrote day rows beside the timestamped ones (CODE_REVIEW M2).

def test_the_handover_day_is_split_at_the_moment_collection_began(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "play_day_timezone", "UTC")
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    played = [
        ("Radiohead", "Let Down", 1790323200),   # 2026-09-25 08:00 UTC
        ("Radiohead", "Let Down", 1790359200),   # 2026-09-25 18:00 UTC
    ]
    planned = lastfm.plan("alex", "u-alex", played, index,
                          before="2026-09-25T17:00:00+00:00")

    assert planned["matched"] == 1
    assert planned["after_snapshots_began"] == 1


def test_a_nightly_first_reading_held_the_whole_of_its_day(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "play_day_timezone", "UTC")
    index = _index([("Radiohead", "Let Down", ["uuid-a"])])
    played = [("Radiohead", "Let Down", 1790359200)]   # 2026-09-25 18:00

    kept = lastfm.plan("alex", "u-alex", played, index, before="2026-09-25")
    dropped = lastfm.plan("alex", "u-alex", played, index, before="2026-09-24")

    assert kept["matched"] == 1
    assert dropped["after_snapshots_began"] == 1


def test_timed_rows_are_counted_per_person(state_db):
    from app import store

    store.connection().executemany(
        "INSERT INTO play_imported (played_at, track_uuid, user_id, username,"
        " plays, source) VALUES (?, ?, ?, 'alex', 1, 'lastfm')",
        [("2026-01-01", "a", "u-alex"), ("2026-01-02T10:00:00+00:00", "a", "u-alex"),
         ("2026-01-02T10:00:00+00:00", "a", "u-kelly")])

    assert lastfm.timed_rows("u-alex") == 1


# --- fetching the history ----------------------------------------------------

def _page(tracks, pages):
    return {"recenttracks": {"@attr": {"totalPages": str(pages)},
                             "track": tracks}}


def _scrobble(title, uts):
    return {"artist": {"#text": "A"}, "name": title, "date": {"uts": str(uts)}}


def _fetch(monkeypatch, answers):
    asked = []

    def call(method, **params):
        asked.append(params)
        return answers[int(params["page"]) - 1]

    monkeypatch.setattr(lastfm, "_call", call)
    monkeypatch.setattr(lastfm.time, "sleep", lambda seconds: None)
    return asked


def test_a_scrobble_pushed_onto_the_next_page_is_counted_once(monkeypatch):
    """Pages run newest first, so a play landing mid-fetch moves the last row
    of one page to the top of the next."""
    asked = _fetch(monkeypatch, [
        _page([_scrobble("T3", 3), _scrobble("T2", 2)], 2),
        _page([_scrobble("T2", 2), _scrobble("T1", 1)], 2),
    ])
    assert lastfm.scrobbles("u", "k") == [("A", "T3", 3), ("A", "T2", 2),
                                           ("A", "T1", 1)]
    assert len({params["to"] for params in asked}) == 1


def test_an_empty_page_part_way_through_is_an_error_not_the_end(monkeypatch):
    _fetch(monkeypatch, [
        _page([_scrobble("T3", 3)], 3),
        {"recenttracks": {}},
        _page([_scrobble("T1", 1)], 3),
    ])
    with pytest.raises(lastfm.LastfmError):
        lastfm.scrobbles("u", "k")


def test_a_page_holding_a_single_track_is_read(monkeypatch):
    _fetch(monkeypatch, [_page(_scrobble("T1", 1), 1)])
    assert lastfm.scrobbles("u", "k") == [("A", "T1", 1)]


# --- a copy set aside does not make a scrobble ambiguous (2M17) ----------------

def _index_db(tmp_path, rows):
    import sqlite3

    db = sqlite3.connect(tmp_path / "nd.db")
    db.executescript("""
        create table folder (id text primary key, missing integer default 0);
        create table media_file (id text, artist text, title text, tags text,
                                 missing integer default 0, folder_id text,
                                 library_id integer default 1);
        insert into folder values ('f', 0);
    """)
    for n, (track_uuid, missing) in enumerate(rows):
        db.execute("insert into media_file values (?, 'Queen', 'Jealousy', ?, ?, 'f', 1)",
                   (str(n), json.dumps({"navidrome_uuid": [{"value": track_uuid}]}),
                    missing))
    return db


def test_a_copy_set_aside_does_not_make_its_scrobbles_ambiguous(tmp_path):
    """Navidrome keeps the row of a resolved duplicate, marked missing. Both
    were candidates, so every scrobble of the track was dropped as ambiguous."""
    index = lastfm.library_index(_index_db(tmp_path, [("kept", 0), ("gone", 1)]))
    assert index[("queen", "jealousy")] == ["kept"]


def test_a_track_no_longer_in_the_library_still_matches(tmp_path):
    index = lastfm.library_index(_index_db(tmp_path, [("gone", 1)]))
    assert index[("queen", "jealousy")] == ["gone"]
