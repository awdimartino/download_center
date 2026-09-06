"""The seed tags beets matches against.

These exist because of one bug, worth stating in full. beets decides an
album is a Various Artists release when its tracks do not agree on the
`artist` tag, and then searches MusicBrainz for a compilation. Spotify
credits a featured artist on the track, so one guest appearance among nine
tracks was enough: staged Thriller offered five Various Artists candidates
with a best distance of 0.41 and was refused, while the same album with that
one tag normalised matched Michael Jackson - Thriller at 0.01 and filed.

Every album with a guest on one track failed this way, which is most of the
staging backlog.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from mutagen.easyid3 import EasyID3

from app import spotify, tagger

SILENCE = Path(__file__).parent / "fixtures" / "silence.mp3"


def _spotify_track(names, album_names=("Michael Jackson",)):
    """A Spotify API track object, trimmed to what to_track reads."""
    return {
        "id": "t1",
        "name": "The Girl Is Mine",
        "external_ids": {"isrc": "USSM18300012"},
        "artists": [{"name": name} for name in names],
        "album": {
            "id": "a1", "name": "Thriller", "total_tracks": 9,
            "release_date": "1982-11-30", "images": [],
            "artists": [{"name": name} for name in album_names],
        },
        "track_number": 3, "disc_number": 1, "duration_ms": 222000,
    }


# --- what Spotify gives us --------------------------------------------------

def test_the_full_credit_is_kept():
    """The browser shows it, and the YouTube Music search uses it - naming
    the guest is how the right recording gets found."""
    track = spotify.to_track(_spotify_track(["Michael Jackson", "Paul McCartney"]))
    assert track["artist"] == "Michael Jackson, Paul McCartney"


def test_the_primary_artist_is_carried_separately():
    track = spotify.to_track(_spotify_track(["Michael Jackson", "Paul McCartney"]))
    assert track["primary_artist"] == "Michael Jackson"


def test_a_track_with_one_artist_agrees_with_itself():
    track = spotify.to_track(_spotify_track(["Michael Jackson"]))
    assert track["primary_artist"] == track["artist"] == "Michael Jackson"


def test_the_primary_artist_is_not_split_off_the_joined_string():
    """"Tyler, The Creator" is one artist with a comma in it. Splitting the
    display string would have filed him as "Tyler"."""
    track = spotify.to_track(_spotify_track(["Tyler, The Creator", "Kali Uchis"]))
    assert track["primary_artist"] == "Tyler, The Creator"


def test_an_artistless_track_does_not_invent_one():
    track = spotify.to_track(_spotify_track([]))
    assert track["primary_artist"] == ""


# --- what reaches the file --------------------------------------------------

def _tag(tmp_path, item):
    path = tmp_path / "track.mp3"
    shutil.copy(SILENCE, path)
    tagger.tag(path, item, embed_cover=False)
    return EasyID3(path)


def _item(**overrides):
    item = spotify.to_track(_spotify_track(["Michael Jackson", "Paul McCartney"]))
    item.update(overrides)
    return item


def test_the_artist_tag_is_the_primary_artist(tmp_path):
    """The one that decides whether beets reads the album as a compilation."""
    tags = _tag(tmp_path, _item())
    assert tags["artist"] == ["Michael Jackson"]


def test_the_album_artist_tag_is_unchanged(tmp_path):
    tags = _tag(tmp_path, _item())
    assert tags["albumartist"] == ["Michael Jackson"]


def test_every_track_of_an_album_agrees(tmp_path):
    """The actual property beets tests. A guest on track three used to break
    it, and one disagreement is all it takes."""
    album = [
        _item(primary_artist="Michael Jackson", artist="Michael Jackson"),
        _item(primary_artist="Michael Jackson",
              artist="Michael Jackson, Paul McCartney"),
    ]
    written = set()
    for index, item in enumerate(album):
        path = tmp_path / f"{index}.mp3"
        shutil.copy(SILENCE, path)
        tagger.tag(path, item, embed_cover=False)
        written.add(EasyID3(path)["artist"][0])

    assert written == {"Michael Jackson"}, "beets would read this as VA"


def test_an_item_without_a_primary_artist_falls_back(tmp_path):
    """generic.py builds items from yt-dlp and has no Spotify artist list.
    Falling back to the full credit is right - it is what we have - and a
    single loose track has no siblings to disagree with."""
    item = _item()
    del item["primary_artist"]
    tags = _tag(tmp_path, item)
    assert tags["artist"] == ["Michael Jackson, Paul McCartney"]
