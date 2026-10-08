"""Direct links: what a playlist's tracks are tagged with (CODE_REVIEW M22).

A playlist is read flat, and a flat entry carries no album or artist - so a
YouTube Music album became N one-track albums, often under Unknown Artist.
The comment saying full metadata was fetched at download time was untrue.
yt-dlp is faked here; nothing reaches the network.
"""

from __future__ import annotations

import pytest

from app import generic

PLAYLIST = "https://music.youtube.com/playlist?list=OLAK5uy_album"

FLAT = {
    "_type": "playlist", "title": "Album - Record",
    "entries": [
        {"id": "v1", "url": "https://www.youtube.com/watch?v=v1",
         "title": "One", "ie_key": "Youtube"},
        {"id": "v2", "url": "https://www.youtube.com/watch?v=v2",
         "title": "Two", "ie_key": "Youtube"},
    ],
}

FULL = {
    "https://www.youtube.com/watch?v=v1": {
        "id": "v1", "webpage_url": "https://www.youtube.com/watch?v=v1",
        "title": "One", "track": "One", "artist": "Artist", "album": "Record",
        "duration": 200, "extractor_key": "Youtube"},
    "https://www.youtube.com/watch?v=v2": {
        "id": "v2", "webpage_url": "https://www.youtube.com/watch?v=v2",
        "title": "Two", "track": "Two", "artist": "Artist", "album": "Record",
        "duration": 210, "extractor_key": "Youtube"},
}


class FakeYDL:
    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        if url == PLAYLIST:
            return FLAT
        if url not in FULL:
            raise generic.yt_dlp.utils.DownloadError("gone")
        return FULL[url]


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    monkeypatch.setattr(generic.yt_dlp, "YoutubeDL", FakeYDL)


def test_a_playlists_tracks_carry_their_album_and_artist():
    title, items = generic.resolve(PLAYLIST)

    assert [(i["title"], i["artist"], i["album"]) for i in items] == [
        ("One", "Artist", "Record"), ("Two", "Artist", "Record")]


def test_an_entry_that_cannot_be_read_in_full_keeps_what_the_list_said(
        monkeypatch):
    monkeypatch.setitem(FLAT["entries"][1], "url",
                        "https://www.youtube.com/watch?v=missing")

    title, items = generic.resolve(PLAYLIST)

    assert len(items) == 2
    assert items[0]["album"] == "Record"
    assert items[1]["title"] == "Two"


# --- cookies.txt (L2) ---------------------------------------------------------
# The download used cookies.txt and resolving did not, so an age-gated video
# failed at resolve though it would have downloaded.

def test_resolving_reads_with_the_cookies_the_download_uses(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(type(generic.settings), "cookies_file",
                        property(lambda self: cookies))
    seen = []

    class Recording(FakeYDL):
        def __init__(self, options):
            seen.append(options)
            super().__init__(options)

    monkeypatch.setattr(generic.yt_dlp, "YoutubeDL", Recording)
    generic.resolve(PLAYLIST)
    assert len(seen) == 3  # the list, then each entry in full
    assert all(o.get("cookiefile") == str(cookies) for o in seen)


def test_no_cookies_file_means_no_cookie_option(monkeypatch):
    monkeypatch.setattr(type(generic.settings), "cookies_file",
                        property(lambda self: None))
    assert "cookiefile" not in generic._options()


# --- an oversized playlist is refused before each entry is read (L15) ----------

def test_a_playlist_over_the_limit_is_refused_from_the_flat_list(monkeypatch):
    seen = []

    class Recording(FakeYDL):
        def extract_info(self, url, download=False):
            seen.append(url)
            return super().extract_info(url, download)

    monkeypatch.setattr(generic.yt_dlp, "YoutubeDL", Recording)
    with pytest.raises(generic.ResolveError, match="has 2 tracks, more than the 1"):
        generic.resolve(PLAYLIST, limit=1)

    assert seen == [PLAYLIST]


# --- one album, one artist, its own order (2L8) -----------------------------------

def test_a_guest_track_stays_on_the_album_and_the_order_is_kept(monkeypatch):
    """album_artist was each track's own artist and track_no always None: a
    guest track was filed under "Artist, Guest" as an album of its own, and
    the rest played alphabetically."""
    guest = dict(FULL["https://www.youtube.com/watch?v=v2"],
                 artist="Artist, Guest", artists=["Artist", "Guest"])
    monkeypatch.setitem(FULL, "https://www.youtube.com/watch?v=v2", guest)

    _, items = generic.resolve(PLAYLIST)

    assert [i["album_artist"] for i in items] == ["Artist", "Artist"]
    assert [i["artist"] for i in items] == ["Artist", "Artist, Guest"]
    assert [i["track_no"] for i in items] == [1, 2]
    assert {i["album_total"] for i in items} == {2}


def test_a_list_of_unrelated_videos_stays_singles(monkeypatch):
    other = dict(FULL["https://www.youtube.com/watch?v=v2"], album="Elsewhere")
    monkeypatch.setitem(FULL, "https://www.youtube.com/watch?v=v2", other)

    _, items = generic.resolve(PLAYLIST)

    assert [i["track_no"] for i in items] == [None, None]
