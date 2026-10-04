"""The library's three ways in: albums, artists, and what needs attention.

Against the same Navidrome-shaped database the listing tests use. The
attention list is the one with judgement in it - which singles are pieces
of an album and which are duplicates of one - so most of the cases are
there.
"""

from __future__ import annotations

from app import library
from conftest import add_track
from test_library import db, make_album  # noqa: F401


def single(db, artist, title, **fields):
    add_track(db, f"{artist}-{title}".replace(" ", "-"),
              path=f"{artist}/{title}/{title}.mp3", title=title, album=title,
              artist=artist, album_artist=artist,
              album_id=f"al-{artist}-{title}", **fields)


def names(listed):
    return [a["album"] for a in listed["albums"]]


# --- the album list ------------------------------------------------------------

def test_a_folder_of_one_track_is_a_single(db, identity):
    single(db, "Mitski", "My Love Mine All Mine")
    make_album(db, "Mitski/Be the Cowboy", 3, album="Be the Cowboy",
               album_artist="Mitski")

    kinds = {a["album"]: a["kind"] for a in library.listing(identity)["albums"]}
    assert kinds == {"My Love Mine All Mine": "single", "Be the Cowboy": "album"}


def test_the_list_narrows_to_one_kind(db, identity):
    single(db, "Mitski", "Washing Machine Heart")
    make_album(db, "Mitski/Be the Cowboy", 3, album="Be the Cowboy",
               album_artist="Mitski")

    assert names(library.listing(identity, kind="single")) == ["Washing Machine Heart"]
    assert names(library.listing(identity, kind="album")) == ["Be the Cowboy"]


def test_the_list_sorts_by_artist_and_by_year(db, identity):
    make_album(db, "B/Later", 2, album="Later", album_artist="B", year=2020)
    make_album(db, "A/Earlier", 2, album="Earlier", album_artist="A", year=1999)

    assert names(library.listing(identity, sort="artist")) == ["Earlier", "Later"]
    assert names(library.listing(identity, sort="year")) == ["Later", "Earlier"]


def test_an_unknown_sort_is_refused(db, identity):
    import pytest

    with pytest.raises(ValueError):
        library.listing(identity, sort="vibes")


def test_the_artist_page_holds_exactly_that_artist(db, identity):
    single(db, "Big Thief", "Vampire Empire")
    single(db, "Big Thief Tribute", "Not")

    assert names(library.listing(identity, artist="big thief")) == ["Vampire Empire"]


def test_an_album_reports_its_year_and_length(db, identity):
    make_album(db, "A/Record", 3, album="Record", album_artist="A",
               year=2021, duration=100.0)

    album = library.listing(identity)["albums"][0]
    assert (album["year"], album["duration"]) == (2021, 300)


def test_a_search_finds_songs_as_well_as_albums(db, identity):
    make_album(db, "Phoebe Bridgers/Punisher", 1, album="Punisher",
               album_artist="Phoebe Bridgers", title="Kyoto")

    listed = library.listing(identity, search="kyoto")

    assert listed["albums"] == []
    assert [(s["title"], s["folder"]) for s in listed["songs"]] == [
        ("Kyoto", "Phoebe Bridgers/Punisher")]


def test_a_search_for_a_wildcard_is_taken_literally(db, identity):
    make_album(db, "A/Record", 1, album="Record", album_artist="A",
               title="Everything")

    assert library.listing(identity, search="%")["songs"] == []


def test_songs_come_only_with_the_first_page(db, identity):
    make_album(db, "A/Record", 1, album="Record", album_artist="A", title="Kyoto")
    assert library.listing(identity, search="kyoto", offset=50)["songs"] == []


# --- artists --------------------------------------------------------------------

def test_artists_count_albums_and_singles_apart(db, identity):
    make_album(db, "Phoebe Bridgers/Punisher", 3, album="Punisher",
               album_artist="Phoebe Bridgers")
    single(db, "Phoebe Bridgers", "Motion Sickness")
    single(db, "Mitski", "Nobody")

    found = {a["artist"]: a for a in library.artists(identity)["artists"]}

    assert set(found) == {"Phoebe Bridgers", "Mitski"}
    assert (found["Phoebe Bridgers"]["albums"],
            found["Phoebe Bridgers"]["singles"],
            found["Phoebe Bridgers"]["tracks"]) == (1, 1, 4)
    assert len(found["Phoebe Bridgers"]["art_ids"]) == 2


def test_two_spellings_of_one_artist_are_one_artist(db, identity):
    single(db, "boygenius", "Not Strong Enough")
    single(db, "Boygenius", "Cool About It")

    assert len(library.artists(identity)["artists"]) == 1


# --- needs attention -------------------------------------------------------------

def test_singles_by_one_artist_are_offered_together(db, identity):
    for title in ("Motion Sickness", "Scott Street", "Funeral"):
        single(db, "Phoebe Bridgers", title)
    single(db, "Mitski", "Nobody")

    together = library.attention(identity)["together"]

    assert len(together) == 1
    assert together[0]["artist"] == "Phoebe Bridgers"
    assert sorted(a["album"] for a in together[0]["albums"]) == [
        "Funeral", "Motion Sickness", "Scott Street"]


def test_a_single_already_on_an_album_is_a_duplicate_not_a_piece(db, identity):
    """Combining it would put the song on the album twice."""
    make_album(db, "Phoebe Bridgers/Stranger in the Alps", 1,
               album="Stranger in the Alps", album_artist="Phoebe Bridgers",
               title="Motion Sickness")
    add_track(db, "pb-extra", path="Phoebe Bridgers/Stranger in the Alps/02 - Funeral.mp3",
              title="Funeral", album="Stranger in the Alps",
              album_artist="Phoebe Bridgers", artist="Phoebe Bridgers",
              album_id="al-Phoebe Bridgers/Stranger in the Alps")
    single(db, "Phoebe Bridgers", "Motion Sickness")
    single(db, "Phoebe Bridgers", "Scott Street")

    found = library.attention(identity)

    assert [(b["album"], b["on_album"]) for b in found["beside"]] == [
        ("Motion Sickness", "Stranger in the Alps")]
    assert found["together"] == [], "one stray single is not a group"


def test_review_and_replaygain_are_counted_in_full(db, identity):
    for n in range(library.SAMPLE + 3):
        make_album(db, f"A/Record {n}", 2, album=f"Record {n}", album_artist="A")

    found = library.attention(identity)

    assert found["review"]["count"] == library.SAMPLE + 3
    assert len(found["review"]["albums"]) == library.SAMPLE
    assert found["no_gain"]["count"] == library.SAMPLE + 3


def test_plays_are_this_persons_own(db, identity):
    import sqlite3

    make_album(db, "A/Loved", 1, album="Loved", album_artist="A")
    make_album(db, "B/Ignored", 1, album="Ignored", album_artist="B")
    connection = sqlite3.connect(db)
    with connection:
        # Navidrome has it; the shared fixture leaves it to the tests that read it.
        connection.execute("alter table annotation add column play_count INTEGER DEFAULT 0")
        connection.executemany(
            "insert into annotation (user_id, item_id, item_type, play_count)"
            " values (?, ?, 'media_file', ?)",
            [("u-alex", "A-Loved-1", 9), ("u-kelly", "B-Ignored-1", 50)])
    connection.close()

    listed = library.listing(identity, sort="plays")
    assert [(a["album"], a["plays"]) for a in listed["albums"]] == [
        ("Loved", 9), ("Ignored", 0)]
