"""SoundCloud and Bandcamp as fallbacks, and choosing between copies by sound.

The services are replaced by what they were seen to answer on 2026-10-09:
SoundCloud's flat search gives a title, an uploader and a length; Radiohead's
own uploads there are 30-second previews; Bandcamp's search gives a name, an
artist and an album but no length.
"""

from __future__ import annotations

import pytest

from app import matcher, sources

KARMA = {"title": "Karma Police", "artist": "Radiohead", "album": "OK Computer",
         "duration_ms": 264066}


def sc(title, uploader, duration, n=1):
    return {"title": title, "uploader": uploader, "duration": duration,
            "url": f"https://api.soundcloud.com/tracks/{n}",
            "webpage_url": f"https://soundcloud.com/x/{n}"}


# --- SoundCloud ---------------------------------------------------------------

def test_an_official_preview_is_never_the_song(monkeypatch):
    monkeypatch.setattr(sources, "_soundcloud_entries", lambda q: [
        sc("Karma Police", "Radiohead", 30.0)])

    with pytest.raises(matcher.MatchError, match="right title, artist and length"):
        sources.find("soundcloud", KARMA)


def test_a_re_upload_naming_the_artist_in_its_title_is_found(monkeypatch):
    monkeypatch.setattr(sources, "_soundcloud_entries", lambda q: [
        sc("Karma Police", "Radiohead", 30.0, 1),
        sc("Radiohead - Karma Police", "skintowear", 264.5, 2),
        sc("Radiohead - Karma Police (sped up)", "edits", 263.0, 3)])

    url, score, _ = sources.find("soundcloud", KARMA)

    assert url == "https://soundcloud.com/x/2"
    assert score >= matcher.SCORE_FLOOR


def test_a_title_first_re_upload_is_read_the_other_way_round(monkeypatch):
    teardrop = {"title": "Teardrop", "artist": "Massive Attack",
                "album": "Mezzanine", "duration_ms": 330000}
    monkeypatch.setattr(sources, "_soundcloud_entries", lambda q: [
        sc("Teardrop ● Massive Attack", "someone", 331.0)])

    url, _score, _ = sources.find("soundcloud", teardrop)

    assert url == "https://soundcloud.com/x/1"


def test_soundcloud_could_not_be_asked_is_not_nothing_there(monkeypatch):
    class Broken:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, *a, **k):
            raise RuntimeError("HTTP Error 429")

    monkeypatch.setattr(sources, "_ydl", lambda **k: Broken())

    with pytest.raises(matcher.SearchUnavailable, match="SoundCloud"):
        sources.find("soundcloud", KARMA)


# --- Bandcamp -------------------------------------------------------------------

def bc(name, band, n, album="Kairoclerosis"):
    return {"type": "t", "name": name, "band_name": band, "album_name": album,
            "item_url_path": f"https://{band}.bandcamp.com/track/{n}"}


SEED = {"title": "The Bloat", "artist": "kuatari", "album": "Kairoclerosis",
        "duration_ms": 357500}


def test_bandcamp_opens_only_the_best_names_for_their_length(monkeypatch):
    found = [bc("Over Nets", "kuatari", "over"), bc("The Bloat", "kuatari", "bloat"),
             bc("The Bloat (live)", "kuatari", "live"), bc("Bloat", "someone", "other"),
             bc("Draft Guides", "kuatari", "draft")]
    monkeypatch.setattr(sources, "_bandcamp_search", lambda text: found)
    opened = []

    def duration(url):
        opened.append(url)
        return 357.5 if url.endswith("/bloat") else 200.0

    monkeypatch.setattr(sources, "_bandcamp_duration", duration)

    url, _score, _ = sources.find("bandcamp", SEED)

    assert url == "https://kuatari.bandcamp.com/track/bloat"
    assert len(opened) <= sources.BANDCAMP_OPENED
    # A name that fails the title or artist gate costs no request.
    assert "https://someone.bandcamp.com/track/other" not in opened


def test_bandcamp_tries_the_title_alone_when_both_find_nothing(monkeypatch):
    asked = []

    def search(text):
        asked.append(text)
        return [] if len(asked) == 1 else [bc("The Bloat", "kuatari", "bloat")]

    monkeypatch.setattr(sources, "_bandcamp_search", search)
    monkeypatch.setattr(sources, "_bandcamp_duration", lambda url: 357.0)

    sources.find("bandcamp", SEED)

    assert asked == ["kuatari The Bloat", "The Bloat"]


def test_a_challenge_page_is_could_not_ask(monkeypatch):
    class Page:
        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError("Expecting value")

    monkeypatch.setattr(sources.requests, "post", lambda *a, **k: Page())

    with pytest.raises(matcher.SearchUnavailable, match="challenge"):
        sources._bandcamp_search("kuatari")


# --- which copy sounds better -----------------------------------------------------

def test_opus_outranks_an_mp3_of_the_same_bitrate_and_lossless_outranks_both():
    youtube = sources.worth({"acodec": "opus", "abr": 130})
    soundcloud = sources.worth({"acodec": "mp3", "abr": 128})
    original = sources.worth({"ext": "wav", "acodec": None})

    assert original > youtube > soundcloud
    assert sources.worth({"acodec": "none", "vcodec": "h264", "tbr": 2000}) == 0


def test_only_a_near_certain_match_is_confirmed():
    assert sources.confirmed(0.95, {"duration": 0.9})
    assert not sources.confirmed(0.95, {"duration": 0.5})   # 7s off
    assert not sources.confirmed(0.80, {"duration": 1.0})
