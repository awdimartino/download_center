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

def test_singles_naming_one_record_are_offered_together(db, identity):
    add_track(db, "a", path="Radiohead/OK Computer/05 - Let Down.mp3",
              title="Let Down", album="OK Computer", artist="Radiohead",
              album_artist="Radiohead", album_id="al-a")
    add_track(db, "b", path="Non-Album/Radiohead/No Surprises.mp3",
              title="No Surprises", album="OK Computer (Single)",
              artist="Radiohead", album_artist="Radiohead", album_id="al-b")
    single(db, "Radiohead", "Creep")

    together = library.attention(identity)["together"]

    assert len(together) == 1
    assert sorted(a["album"] for a in together[0]["albums"]) == [
        "OK Computer", "OK Computer (Single)"]


def test_an_artists_separate_singles_are_not_one_album(db, identity):
    """Every one of an artist's singles was once offered as one album."""
    for title in ("Motion Sickness", "Scott Street", "Funeral"):
        single(db, "Phoebe Bridgers", title)
    for title in ("ワーストリグレット", "マージナルソウル"):
        single(db, "youまん", title)
    for n in (1, 2):
        add_track(db, f"u{n}", path=f"Mitski/Unknown Album/{n}/x.mp3",
                  title=f"Untitled {n}", album="[Unknown Album]",
                  artist="Mitski", album_artist="Mitski", album_id=f"al-u{n}")

    assert library.attention(identity)["together"] == []


def test_a_record_key_drops_the_release_kind_and_keeps_any_script():
    assert library.record_key("The Afterparty (Single)") == library.record_key(
        "the afterparty")
    assert library.record_key("Splice - EP") == library.record_key("splice")
    assert library.record_key("ワーストリグレット") != library.record_key("マージナルソウル")
    assert library.record_key("Deep") != library.record_key("De")
    assert library.record_key("[Unknown Album]") is None


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
