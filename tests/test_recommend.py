"""The Download tab's recommendations (app/recommend.py).

Spotify and Last.fm are replaced by plain functions returning what each
would have said; everything about the library, the plays and the stars is
real SQLite, as everywhere else in this suite. What is being tested is the
judgement - who counts as a favourite, what counts as already owned, what
a dismissal hides - not the two services.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app import recommend, store
from app.api import browse
from conftest import add_track, annotate

ALEX = "u-alex"


@pytest.fixture
def library(state_db, navidrome_db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "navidrome_db", navidrome_db)
    monkeypatch.setattr(recommend, "LASTFM_PAUSE", 0)
    monkeypatch.setattr(recommend, "SPOTIFY_PAUSE", 0)
    recommend._failed.clear()
    return navidrome_db


def tagged(uuid: str) -> str:
    return json.dumps({"navidrome_uuid": [{"value": uuid}]})


def played(track_uuid: str, plays: int, days_ago: int = 1) -> None:
    when = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    store.connection().execute(
        "insert or replace into play_imported"
        " (played_at, track_uuid, user_id, username, source, plays)"
        " values (?, ?, ?, 'alex', 'lastfm', ?)", (when, track_uuid, ALEX, plays))
    store.connection().commit()


def album(name, artist="Radiohead", kind="album", released="2001-06-05",
          total=10, id_=None):
    return {"id": id_ or f"{artist}-{name}", "name": name, "artist": artist,
            "primary_artist": artist, "type": kind, "released": released,
            "year": released[:4], "total": total, "cover": None, "url": "u"}


def track(name, artist, id_=None):
    return {"id": id_ or f"{artist}-{name}", "name": name, "artist": artist,
            "primary_artist": artist, "url": "u", "cover": None}


# --- names ------------------------------------------------------------------

@pytest.mark.parametrize("title, base", [
    ("OK Computer (Collector's Edition)", "ok computer"),
    ("Abbey Road - Remastered 2009", "abbey road"),
    ("Kid A [Deluxe] (Bonus Tracks)", "kid a"),
    ("Hail to the Thief", "hail to the thief"),
    # A title that is all brackets is kept, not reduced to nothing.
    ("(What's the Story) Morning Glory?", "whats the story morning glory"),
    ("+", "+"),
])
def test_an_edition_is_the_album_you_already_have(title, base):
    assert recommend.base_title(title) == base


def test_any_edition_in_the_library_counts_as_owned(library):
    add_track(library, "t1", album="OK Computer", artist="Radiohead",
              album_artist="Radiohead")
    names = recommend.library_names(1)

    assert recommend._album_owned(album("OK Computer OKNOTOK 1997 2017"), names) is False
    assert recommend._album_owned(album("OK Computer (Collector's Edition)"), names)
    # Somebody else's library is not yours.
    add_track(library, "k1", album="Kid A", artist="Radiohead", library_id=2)
    assert not recommend._album_owned(album("Kid A"), recommend.library_names(1))


# --- who is a favourite -------------------------------------------------------

def test_favourites_are_a_year_of_plays_plus_stars(library):
    add_track(library, "a", artist="Radiohead", tags=tagged("ua"))
    add_track(library, "b", artist="Björk", tags=tagged("ub"))
    add_track(library, "c", artist="Various Artists", tags=tagged("uc"))
    add_track(library, "d", artist="Portishead", tags=tagged("ud"))
    played("ua", 30)
    played("ub", 20)
    played("uc", 100)
    # Two years ago: somebody liked in another life.
    played("ud", 500, days_ago=800)
    # A starred track is worth a few plays, enough to place Portishead.
    annotate(library, ALEX, "d", starred=1)

    favourites = recommend.favourite_artists(ALEX, 1)

    assert [name for name, _ in favourites] == ["Radiohead", "Björk", "Portishead"]


def test_somebody_else_s_stars_are_not_yours(library):
    add_track(library, "a", artist="Radiohead", tags=tagged("ua"))
    annotate(library, "u-kelly", "a", starred=1)

    assert recommend.favourite_artists(ALEX, 1) == []


# --- discographies -------------------------------------------------------------

def test_missing_albums_take_turns_and_leave_out_singles_and_owned(library):
    add_track(library, "t1", album="Kid A", artist="Radiohead", album_artist="Radiohead")
    names = recommend.library_names(1)
    discographies = [
        ("Radiohead", [album("Kid A (Deluxe)"), album("Amnesiac"), album("Creep", kind="single"),
                       album("Hail to the Thief"), album("In Rainbows"), album("The Bends")]),
        ("Björk", [album("Homogenic", artist="Björk"), album("Vespertine", artist="Björk")]),
    ]

    shelf = [a["name"] for a in recommend.missing_albums(discographies, names)]

    # One each in turn, three at most an artist, no Kid A in any edition.
    assert shelf == ["Amnesiac", "Homogenic", "Hail to the Thief", "Vespertine", "In Rainbows"]


def test_new_releases_are_recent_and_not_owned(library):
    names = recommend.library_names(1)
    recent = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%d")
    older = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%d")
    discographies = [("Radiohead", [
        album("Fresh", released=recent), album("Single", kind="single", released=recent),
        album("Old", released=older),
        # Dated only to the year: never "new", whatever the year.
        album("Vague", released=recent[:4]),
    ])]

    shelf = [a["name"] for a in recommend.new_releases(discographies, names)]

    assert sorted(shelf) == ["Fresh", "Single"]


# --- Last.fm's half -----------------------------------------------------------

def test_similar_artists_rank_by_how_many_favourites_agree(library, monkeypatch):
    add_track(library, "t1", artist="Massive Attack", album_artist="Massive Attack")
    similar = {
        "Radiohead": [("Muse", "0.9"), ("Thom Yorke", "0.8"), ("Massive Attack", "0.7")],
        "Björk": [("Thom Yorke", "0.3"), ("FKA twigs", "0.95")],
        "Portishead": [("Thom Yorke", "0.2"), ("FKA twigs", "0.1")],
    }

    def fake_lastfm(method, artist, **_):
        assert method == "artist.getSimilar"
        return {"similarartists": {"artist": [{"name": n, "match": m}
                                              for n, m in similar[artist]]}}

    monkeypatch.setattr(recommend, "_lastfm", fake_lastfm)
    monkeypatch.setattr(recommend, "find_artist",
                        lambda name: {"id": name, "name": name, "cover": None})
    favourites = [("Radiohead", 30), ("Björk", 20), ("Portishead", 5)]

    cards = recommend.similar_artists(favourites, recommend.library_names(1), [])

    # Massive Attack is in the library already.
    assert [c["name"] for c in cards] == ["Thom Yorke", "FKA twigs", "Muse"]
    assert cards[0]["because"] == ["Radiohead", "Björk", "Portishead"]


def test_because_you_played_skips_the_seed_artist_and_what_is_held(library, monkeypatch):
    similar = [("Paranoid Android", "Radiohead"), ("Teardrop", "Massive Attack"),
               ("Angel", "Massive Attack"), ("Unfinished Sympathy", "Massive Attack"),
               ("Glory Box", "Portishead"), ("Roads", "Portishead")]
    monkeypatch.setattr(recommend, "_lastfm", lambda method, **_: {
        "similartracks": {"track": [{"name": n, "artist": {"name": a}} for n, a in similar]}})
    monkeypatch.setattr(recommend, "find_track", lambda name, artist: track(name, artist))
    held = {recommend.registry.recording_key("Portishead", "Glory Box")}

    shelves = recommend.because_you_played(
        [{"title": "Karma Police", "artist": "Radiohead"}], held, [])

    assert [t["name"] for t in shelves[0]["tracks"]] == ["Teardrop", "Angel", "Roads"]


def test_no_last_fm_key_leaves_its_shelves_out_and_says_so(library, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "lastfm_api_key", "")
    monkeypatch.setattr(recommend, "_discographies", lambda favourites, problems: [])

    payload = recommend.compute(ALEX, 1)

    assert payload["lastfm"] is False
    assert payload["shelves"]["similar"] == []


# --- not interested -------------------------------------------------------------

def payload_with(**shelves):
    base = {"new": [], "missing": [], "similar": [], "because": [], "genres": []}
    base.update(shelves)
    return {"computed_at": datetime.now(UTC).isoformat(), "library_id": 1,
            "lastfm": True, "problems": [], "shelves": base}


def test_a_dismissed_album_and_artist_stay_hidden_until_undone(library):
    payload = payload_with(
        missing=[album("Amnesiac"), album("Homogenic", artist="Björk")],
        similar=[{"id": "x", "name": "Björk", "because": ["Radiohead"]}],
        because=[{"seed": {"title": "s", "artist": "a"},
                  "tracks": [track("Jóga", "Björk"), track("Teardrop", "Massive Attack")]}])
    names = recommend.library_names(1)

    recommend.dismiss(ALEX, "album", recommend.album_dismiss_key(album("Amnesiac")), "Amnesiac")
    recommend.dismiss(ALEX, "artist", "björk", "Björk")
    shown = recommend.present(payload, ALEX, names)["shelves"]

    assert shown["missing"] == []
    assert shown["similar"] == []
    assert [t["name"] for t in shown["because"][0]["tracks"]] == ["Teardrop"]
    # Somebody else's dismissals are theirs.
    assert len(recommend.present(payload, "u-kelly", names)["shelves"]["missing"]) == 2

    recommend.undismiss(ALEX, "artist", "björk")
    shown = recommend.present(payload, ALEX, names)["shelves"]
    assert [a["name"] for a in shown["missing"]] == ["Homogenic"]


def test_every_card_carries_the_keys_its_dismissal_sends(library):
    shown = recommend.present(payload_with(missing=[album("Amnesiac")]), ALEX,
                              recommend.library_names(1))
    card = shown["shelves"]["missing"][0]

    assert card["dismiss_key"] == recommend.registry.album_key("Radiohead", "Amnesiac")
    assert card["artist_key"] == "radiohead"


def test_a_fully_held_album_is_dropped_by_the_route(library):
    add_track(library, "t1", album="Amnesiac", title="One", artist="Radiohead",
              album_artist="")
    add_track(library, "t2", album="Amnesiac", title="Two", artist="Radiohead",
              album_artist="")
    data = {"shelves": {"new": [], "missing": [album("Amnesiac", total=2), album("Kid A", total=2)],
                        "similar": [], "genres": [],
                        "because": [{"seed": {}, "tracks": [track("One", "Radiohead"),
                                                            track("Three", "Radiohead")]}]}}

    browse._shelves_held(data, 1)

    assert [a["name"] for a in data["shelves"]["missing"]] == ["Kid A"]
    assert [t["name"] for t in data["shelves"]["because"][0]["tracks"]] == ["Three"]


# --- once a day, in the background ------------------------------------------------

def test_the_first_visit_starts_a_pass_and_the_next_is_answered_from_it(library):
    calls = []

    def work(user_id, library_id):
        calls.append(user_id)
        return payload_with(missing=[album("Amnesiac")])

    recommend.start(ALEX, 1, work)
    recommend.wait(ALEX, 10)
    shown = recommend.view(ALEX, 1)

    assert calls == [ALEX]
    assert shown["status"] == "ready" and shown["refreshing"] is False
    assert [a["name"] for a in shown["shelves"]["missing"]] == ["Amnesiac"]


def test_a_stale_pass_is_served_while_the_next_is_worked_out(library, monkeypatch):
    old = payload_with(missing=[album("Amnesiac")])
    old["computed_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    recommend._save(ALEX, old)
    started = []
    monkeypatch.setattr(recommend, "start",
                        lambda user_id, library_id, work=None: started.append(user_id))

    shown = recommend.view(ALEX, 1)

    assert started == [ALEX]
    assert [a["name"] for a in shown["shelves"]["missing"]] == ["Amnesiac"]


def test_a_failed_first_pass_says_why_and_is_not_retried_on_every_visit(library, monkeypatch):
    def broken(user_id, library_id):
        raise RuntimeError("Spotify said no")

    recommend.start(ALEX, 1, broken)
    recommend.wait(ALEX, 10)
    started = []
    monkeypatch.setattr(recommend, "start",
                        lambda user_id, library_id, work=None: started.append(user_id))

    shown = recommend.view(ALEX, 1)

    assert shown == {"status": "failed", "reason": "Spotify said no"}
    assert started == []


def test_the_pass_survives_a_restart(library):
    recommend._save(ALEX, payload_with(missing=[album("Amnesiac")]))
    recommend._schema_on = None

    assert recommend._stored(ALEX)["shelves"]["missing"][0]["name"] == "Amnesiac"
