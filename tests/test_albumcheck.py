"""Which tracks of an album the library does not have (app/albumcheck.py).

The album is real Navidrome-shaped rows; MusicBrainz and Spotify are
replaced by what they answer, in the shapes musicbrainz.py and
spotify.album_detail return.
"""

from __future__ import annotations

import sqlite3

import pytest

from app import albumcheck, musicbrainz, spotify
from conftest import add_track

FOLDER = "Radiohead/OK Computer"


@pytest.fixture
def db(navidrome_db, state_db, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "navidrome_db", navidrome_db)
    connection = sqlite3.connect(navidrome_db)
    with connection:
        connection.execute("alter table media_file add column mbz_album_id text")
        connection.execute("alter table media_file add column mbz_release_group_id text")
    connection.close()
    return navidrome_db


def held(db, n, title, recording="", release="rel-std", group="rg-1"):
    add_track(db, f"t{n}", path=f"{FOLDER}/{n:02d} - {title}.mp3", title=title,
              album="OK Computer", artist="Radiohead", album_artist="Radiohead",
              track_number=n, mbz_recording_id=recording)
    connection = sqlite3.connect(db)
    with connection:
        connection.execute("update media_file set mbz_album_id = ?, mbz_release_group_id = ?"
                           " where id = ?", (release, group, f"t{n}"))
    connection.close()


def ref(n, title, recording=None, disc=1):
    return {"disc": disc, "number": n, "title": title, "artist": "Radiohead",
            "length_ms": 200000, "recording_id": recording}


STANDARD = [ref(1, "Airbag", "r1"), ref(2, "Paranoid Android", "r2"),
            ref(3, "Subterranean Homesick Alien", "r3"), ref(4, "Exit Music (For a Film)", "r4")]


def mb_release(release_id, tracks, title="OK Computer"):
    return {"id": release_id, "title": title, "artist": "Radiohead", "date": "1997-05-21",
            "country": "GB", "disambiguation": "", "formats": ["CD"],
            "release_group": "rg-1", "tracks": tracks}


@pytest.fixture
def mb(monkeypatch):
    releases = {"rel-std": mb_release("rel-std", STANDARD),
                "rel-deluxe": mb_release("rel-deluxe", STANDARD + [ref(5, "Lull", "r5")],
                                         title="OK Computer OKNOTOK")}
    monkeypatch.setattr(musicbrainz, "release", lambda rid: releases[rid])
    monkeypatch.setattr(musicbrainz, "editions", lambda group: [
        {"id": "rel-std", "title": "OK Computer", "date": "1997", "country": "GB",
         "disambiguation": "", "formats": ["CD"], "track_count": 4},
        # Another pressing of the same tracklist: one choice, not two.
        {"id": "rel-std-us", "title": "OK Computer", "date": "1997", "country": "US",
         "disambiguation": "", "formats": ["CD"], "track_count": 4},
        {"id": "rel-deluxe", "title": "OK Computer OKNOTOK", "date": "2017", "country": "XW",
         "disambiguation": "", "formats": ["CD"], "track_count": 5}])
    return releases


# --- held or missing ------------------------------------------------------------

def test_a_track_is_held_by_its_recording_or_by_its_title():
    files = [{"title": "Airbag", "mbz_recording_id": ""},
             {"title": "Some other name", "mbz_recording_id": "r2"},
             {"title": "Exit Music (For a Film) - Remastered", "mbz_recording_id": ""}]

    marked, extra = albumcheck.compare(STANDARD, files)

    assert [t["held"] for t in marked] == [True, True, False, True]
    assert marked[1]["held_as"] == "Some other name"
    assert extra == 0


def test_one_file_answers_for_one_track_only():
    """Two versions of one title on an edition need two files."""
    reference = [ref(1, "Intro"), ref(9, "Intro")]
    marked, _ = albumcheck.compare(reference, [{"title": "Intro"}])

    assert [t["held"] for t in marked] == [True, False]


def test_files_not_on_the_edition_are_counted():
    _, extra = albumcheck.compare(STANDARD[:1], [{"title": "Airbag"}, {"title": "Lull"}])

    assert extra == 1


# --- from MusicBrainz -------------------------------------------------------------

def test_the_files_own_release_is_the_edition(db, identity, mb):
    held(db, 1, "Airbag", "r1")
    held(db, 2, "Paranoid Android", "r2")

    answer = albumcheck.missing(identity, 1, FOLDER)

    assert answer["source"] == "musicbrainz"
    assert answer["edition"] == "mb:rel-std"
    assert answer["missing"] == 2
    assert [t["title"] for t in answer["tracks"] if not t["held"]] == [
        "Subterranean Homesick Alien", "Exit Music (For a Film)"]
    # Twenty pressings of one tracklist are one choice.
    assert [e["id"] for e in answer["editions"]] == ["mb:rel-std", "mb:rel-deluxe"]


def test_another_edition_can_be_asked_for(db, identity, mb):
    for n, (title, rec) in enumerate([("Airbag", "r1"), ("Paranoid Android", "r2"),
                                      ("Subterranean Homesick Alien", "r3"),
                                      ("Exit Music (For a Film)", "r4")], start=1):
        held(db, n, title, rec)

    standard = albumcheck.missing(identity, 1, FOLDER)
    deluxe = albumcheck.missing(identity, 1, FOLDER, "mb:rel-deluxe")

    assert standard["missing"] == 0
    assert deluxe["missing"] == 1
    assert [t["title"] for t in deluxe["tracks"] if not t["held"]] == ["Lull"]


def test_musicbrainz_down_falls_back_to_spotify_and_says_so(db, identity, monkeypatch):
    held(db, 1, "Airbag", "r1")

    def down(rid):
        raise musicbrainz.Unavailable("MusicBrainz answered 503.")

    monkeypatch.setattr(musicbrainz, "release", down)
    monkeypatch.setattr(spotify, "search", lambda q, kind, limit: [
        {"id": "sp1", "name": "OK Computer", "artists": [{"name": "Radiohead"}]}])
    monkeypatch.setattr(spotify, "album_detail", lambda aid: {
        "id": aid, "name": "OK Computer", "artist": "Radiohead", "year": "1997",
        "type": "album", "url": "https://open.spotify.com/album/sp1",
        "tracks": [{"id": "s1", "name": "Airbag", "track_no": 1, "disc_no": 1},
                   {"id": "s2", "name": "Paranoid Android", "track_no": 2, "disc_no": 1}]})

    answer = albumcheck.missing(identity, 1, FOLDER)

    assert answer["source"] == "spotify"
    assert answer["problems"] == ["MusicBrainz answered 503."]
    assert [t["title"] for t in answer["tracks"] if not t["held"]] == ["Paranoid Android"]
    assert answer["tracks"][1]["spotify_id"] == "s2"


def test_a_track_filed_under_another_album_is_not_missing(db, identity, mb):
    """kuatari's "Little Seed, Arrival" was in the library as its own single:
    downloading it again would make a second copy."""
    held(db, 1, "Airbag", "r1")
    add_track(db, "single", path="Radiohead/Paranoid Android/01.mp3",
              title="Paranoid Android", album="Paranoid Android",
              artist="Radiohead", album_artist="Radiohead")

    answer = albumcheck.missing(identity, 1, FOLDER)
    paranoid = next(t for t in answer["tracks"] if t["title"] == "Paranoid Android")

    assert paranoid["held"] is False and paranoid["elsewhere"] is True
    assert answer["missing"] == 2 and answer["elsewhere"] == 1
    [on] = paranoid["elsewhere_on"]
    assert on["album"]["folder"] == "Radiohead/Paranoid Android"
    assert on["album"]["tracks"] == 1
    assert [t["title"] for t in on["tracks"]] == ["Paranoid Android"]


def test_only_the_copy_is_offered_from_a_bigger_album(db, identity, mb):
    """Merging a compilation whole would bring all of it along."""
    held(db, 1, "Airbag", "r1")
    for n, title in enumerate(("Paranoid Android", "Karma Police", "Creep"), 1):
        add_track(db, f"best-{n}", path=f"Radiohead/The Best Of/0{n}.mp3",
                  title=title, album="The Best Of", artist="Radiohead",
                  album_artist="Radiohead", album_id="al-best")

    answer = albumcheck.missing(identity, 1, FOLDER)
    paranoid = next(t for t in answer["tracks"] if t["title"] == "Paranoid Android")

    [on] = paranoid["elsewhere_on"]
    assert on["album"]["tracks"] == 3
    assert [t["path"] for t in on["tracks"]] == ["Radiohead/The Best Of/01.mp3"]


def test_an_untitled_track_is_not_offered_for_download(db, identity, monkeypatch):
    held(db, 1, "Airbag", "r1")
    monkeypatch.setattr(musicbrainz, "release", lambda rid: mb_release(
        rid, [ref(1, "Airbag", "r1"), ref(2, "[unknown]")]))
    monkeypatch.setattr(musicbrainz, "editions", lambda group: [])

    answer = albumcheck.missing(identity, 1, FOLDER)

    assert [t["downloadable"] for t in answer["tracks"]] == [True, False]


# --- from Spotify -----------------------------------------------------------------

def test_spotify_picks_the_album_sharing_most_titles(db, identity, monkeypatch):
    held(db, 1, "Airbag", release="", group="")
    held(db, 2, "Lull", release="", group="")
    albums = {
        "std": ["Airbag", "Paranoid Android"],
        "deluxe": ["Airbag", "Paranoid Android", "Lull"],
        "other": ["Creep"]}
    monkeypatch.setattr(spotify, "search", lambda q, kind, limit: [
        {"id": a, "name": "OK Computer", "artists": [{"name": "Radiohead"}]} for a in albums]
        + [{"id": "x", "name": "OK Computer", "artists": [{"name": "A Tribute Band"}]}])
    monkeypatch.setattr(spotify, "album_detail", lambda aid: {
        "id": aid, "name": "OK Computer", "artist": "Radiohead", "year": "1997",
        "type": "album", "url": None,
        "tracks": [{"id": f"{aid}{i}", "name": n, "track_no": i, "disc_no": 1}
                   for i, n in enumerate(albums[aid], start=1)]})

    answer = albumcheck.missing(identity, 1, FOLDER)

    assert answer["edition"] == "sp:deluxe"
    assert {e["id"] for e in answer["editions"]} == {"sp:std", "sp:deluxe", "sp:other"}
    assert answer["missing"] == 1


# --- downloading ------------------------------------------------------------------

ALBUM = {"artist": "Radiohead", "album": "OK Computer"}


def test_missing_tracks_are_tagged_as_this_album(monkeypatch):
    monkeypatch.setattr(albumcheck, "_spotify_match", lambda track, album: {
        "spotify_id": "s3", "isrc": "GBAYE9700003", "title": track["title"],
        "artist": "Radiohead", "primary_artist": "Radiohead",
        "album": "OK Computer OKNOTOK 1997 2017", "album_artist": "Radiohead",
        "track_no": 7, "disc_no": 2, "album_total": 23, "cover_url": "http://x/c.jpg",
        "duration_ms": 267000})

    kind, title, items = albumcheck.album_items(
        ALBUM, [{"title": "Subterranean Homesick Alien", "disc": 1, "number": 3}], 12)

    assert kind == "spotify" and "1 missing track" in title
    one = items[0]
    # The library's own names and the reference's numbering, Spotify's ISRC.
    assert (one["album"], one["album_artist"]) == ("OK Computer", "Radiohead")
    assert (one["track_no"], one["disc_no"], one["album_total"]) == (3, 1, 12)
    assert one["isrc"] == "GBAYE9700003"


def test_a_track_spotify_lacks_is_still_queued_from_the_reference(monkeypatch):
    monkeypatch.setattr(albumcheck, "_spotify_match", lambda track, album: None)

    _kind, _title, items = albumcheck.album_items(
        ALBUM, [{"title": "Lull", "artist": "Radiohead", "disc": 1, "number": 5,
                 "length_ms": 254000}], 5)

    one = items[0]
    assert one["title"] == "Lull" and one["spotify_id"] is None
    # What the source search scores on.
    assert one["duration_ms"] == 254000 and one["artist"] == "Radiohead"
    assert one["album"] == "OK Computer" and one["track_no"] == 5
