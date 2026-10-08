"""Cover art: squaring what YouTube sends, and replacing art on its own.

A YouTube download's cover is the video thumbnail - a 16:9 frame with the
album's square cover in the middle and bars either side. Every one of them
arrived in the library that way until `covers.square` cut the cover back out.
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path

from mutagen.id3 import ID3
from PIL import Image

from app import covers, tagger

SILENCE = Path(__file__).parent / "fixtures" / "silence.mp3"

RED = (200, 30, 30)


def _jpeg(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "JPEG", quality=95)
    return out.getvalue()


def _pillarboxed(width=1280, height=720, bar=(0, 0, 0)) -> bytes:
    """A maxresdefault thumbnail: a square red cover between two bars."""
    frame = Image.new("RGB", (width, height), bar)
    frame.paste(Image.new("RGB", (height, height), RED), ((width - height) // 2, 0))
    return _jpeg(frame)


def _letterboxed() -> bytes:
    """An hqdefault thumbnail: the 16:9 frame above, padded to 4:3 in black."""
    frame = Image.new("RGB", (480, 360), (0, 0, 0))
    inner = Image.open(io.BytesIO(_pillarboxed(480, 270)))
    frame.paste(inner, (0, 45))
    return _jpeg(frame)


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def _all_cover(image: Image.Image) -> bool:
    """No bar survived: every corner is the cover's own colour."""
    w, h = image.size
    for x, y in ((2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3)):
        r, g, b = image.getpixel((x, y))
        if not (r > 150 and g < 80 and b < 80):
            return False
    return True


# --- squaring -----------------------------------------------------------------

def test_a_pillarboxed_thumbnail_loses_its_side_bars():
    data, mime = covers.square(_pillarboxed())
    image = _open(data)
    assert mime == "image/jpeg"
    assert image.size == (720, 720)
    assert _all_cover(image)


def test_blurred_bars_go_too():
    """Some uploads pad with a blur of the cover rather than black. The
    cover is still the middle square, so the bars' colour does not matter."""
    image = _open(covers.square(_pillarboxed(bar=(90, 120, 200)))[0])
    assert image.size == (720, 720)
    assert _all_cover(image)


def test_a_letterboxed_thumbnail_loses_bars_on_all_four_sides():
    image = _open(covers.square(_letterboxed())[0])
    assert image.size == (270, 270)
    assert _all_cover(image)


def test_a_genuinely_4_3_picture_is_not_cut_to_16_9():
    """Only black bands are bars. A bright 4:3 image is just squared."""
    image = _open(covers.square(_jpeg(Image.new("RGB", (480, 360), RED)))[0])
    assert image.size == (360, 360)


def test_a_square_jpeg_passes_through_untouched():
    """Every Spotify cover. Re-encoding it would only lose quality."""
    original = _jpeg(Image.new("RGB", (640, 640), RED))
    assert covers.square(original) == (original, "image/jpeg")


def test_a_webp_thumbnail_comes_back_as_a_jpeg():
    """yt-dlp's best thumbnail is often WebP, which used to be embedded with
    a JPEG label it did not deserve."""
    out = io.BytesIO()
    Image.new("RGB", (1280, 720), RED).save(out, "WEBP")
    data, mime = covers.square(out.getvalue())
    assert mime == "image/jpeg"
    assert _open(data).size == (720, 720)


def test_something_that_is_not_an_image_is_left_alone():
    assert covers.square(b"not an image") == (b"not an image", "image/jpeg")


# --- downloads ----------------------------------------------------------------

def test_a_youtube_download_is_tagged_with_a_square_cover(tmp_path, monkeypatch):
    path = tmp_path / "song.mp3"
    shutil.copy(SILENCE, path)
    monkeypatch.setattr(covers, "fetch", lambda url: _pillarboxed())

    tagger.tag(path, {"title": "Song", "artist": "Artist",
                      "album_artist": "Artist", "album": "Song",
                      "cover_url": "https://i.ytimg.com/vi/x/maxresdefault.jpg"})

    picture = ID3(path).getall("APIC")[0]
    assert _open(picture.data).size == (720, 720)


def test_a_flat_playlist_entry_still_gets_a_cover():
    """Flat entries carry `thumbnails` and no `thumbnail`, so every track
    from a playlist used to arrive with no cover at all."""
    from app import generic

    item = generic._to_item({
        "id": "abc", "url": "https://www.youtube.com/watch?v=abc",
        "title": "Artist - Song",
        "thumbnails": [
            {"url": "https://i.ytimg.com/vi/abc/hqdefault.jpg?a", "width": 168, "height": 94},
            {"url": "https://i.ytimg.com/vi/abc/hqdefault.jpg?b", "width": 336, "height": 188},
        ]})
    assert item["cover_url"].endswith("?b")


# --- replacing a cover on a filed album -----------------------------------------

def test_apply_replaces_the_embedded_cover_and_keeps_every_tag(tmp_path):
    path = tmp_path / "song.mp3"
    shutil.copy(SILENCE, path)
    tagger.tag(path, {"title": "Song", "artist": "Artist",
                      "album_artist": "Artist", "album": "Song",
                      "isrc": "USABC1234567"}, embed_cover=False)
    covers.embed(path, _pillarboxed())

    result = covers.apply(tmp_path, [path], _pillarboxed())

    assert result == {"written": 1, "failed": []}
    tags = ID3(path)
    assert len(tags.getall("APIC")) == 1
    assert _open(covers.embedded(path)).size == (720, 720)
    assert str(tags["TSRC"]) == "USABC1234567"


def test_apply_replaces_a_folder_cover_navidrome_would_show_instead(tmp_path):
    path = tmp_path / "song.mp3"
    shutil.copy(SILENCE, path)
    (tmp_path / "folder.png").write_bytes(b"old")

    covers.apply(tmp_path, [path], _pillarboxed())

    assert not (tmp_path / "folder.png").exists()
    assert _open((tmp_path / "cover.jpg").read_bytes()).size == (720, 720)


def test_no_folder_cover_is_invented_where_there_was_none(tmp_path):
    path = tmp_path / "song.mp3"
    shutil.copy(SILENCE, path)
    covers.apply(tmp_path, [path], _pillarboxed())
    assert covers.folder_covers(tmp_path) == []


def test_the_current_cover_is_offered_squared_only_when_it_has_bars(tmp_path, monkeypatch):
    from app import spotify

    monkeypatch.setattr(spotify, "search", lambda *a, **k: [])
    path = tmp_path / "song.mp3"
    shutil.copy(SILENCE, path)

    covers.embed(path, _pillarboxed())
    offered = covers.candidates(tmp_path, [path], "Artist", "Song")
    assert [c["source"] for c in offered] == ["current"]
    assert offered[0]["url"] is None

    covers.embed(path, _jpeg(Image.new("RGB", (500, 500), RED)))
    assert covers.candidates(tmp_path, [path], "Artist", "Song") == []


def test_only_offered_hosts_can_be_fetched_from_the_browser():
    assert covers.choosable("https://i.scdn.co/image/abc")
    assert covers.choosable("https://coverartarchive.org/release/x/front-500")
    assert not covers.choosable("http://i.scdn.co/image/abc")
    assert not covers.choosable("https://192.168.1.1/admin")
    assert not covers.choosable("file:///etc/passwd")


# --- the survey's cost (CODE_REVIEW M38) -------------------------------------
# Each album was walked recursively and twenty cover names tested twice:
# about 28 filesystem calls per album even when the answer was known.

def _album(root, cover: bytes | None = None, below: str = ""):
    folder = root / "A" / "B"
    (folder / below).mkdir(parents=True, exist_ok=True)
    for n in range(3):
        shutil.copy(SILENCE, folder / below / f"{n:02d}.mp3")
    if cover is not None:
        (folder / "cover.jpg").write_bytes(cover)
    return folder


def test_a_known_album_is_answered_from_one_listing(tmp_path, monkeypatch):
    import os

    folder = _album(tmp_path, cover=_letterboxed())
    assert covers.barred(folder) is True

    calls = []
    real_scandir = os.scandir
    monkeypatch.setattr(os, "scandir",
                        lambda *a: calls.append("scandir") or real_scandir(*a))
    monkeypatch.setattr(Path, "is_file",
                        lambda self: calls.append("is_file") or False)

    assert covers.barred(folder) is True
    assert calls == ["scandir"]


def test_a_multi_disc_album_with_no_top_level_tracks_is_still_checked(tmp_path):
    folder = _album(tmp_path, cover=_letterboxed(), below="CD1")

    assert covers.barred(folder) is True


# --- one fetch per cover, not per track (L16) ----------------------------------

import threading
from collections import OrderedDict

import pytest


@pytest.fixture
def fresh_cache(monkeypatch):
    monkeypatch.setattr(covers, "_squared", OrderedDict(), raising=False)
    monkeypatch.setattr(covers, "_fetching", {}, raising=False)


def test_an_albums_tracks_fetch_and_square_its_cover_once(tmp_path, monkeypatch,
                                                         fresh_cache):
    fetched, squared = [], []
    real_square = covers.square

    def fetch(url):
        fetched.append(url)
        return _pillarboxed()

    monkeypatch.setattr(covers, "fetch", fetch)
    monkeypatch.setattr(covers, "square",
                        lambda data: squared.append(1) or real_square(data))
    url = "https://i.ytimg.com/vi/album/maxresdefault.jpg"

    def one(n):
        path = tmp_path / f"{n}.mp3"
        shutil.copy(SILENCE, path)
        tagger.tag(path, {"title": f"Song {n}", "artist": "Artist",
                          "album_artist": "Artist", "album": "Album",
                          "cover_url": url})

    threads = [threading.Thread(target=one, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert fetched == [url]
    assert len(squared) == 1
    for n in range(6):
        assert _open(ID3(tmp_path / f"{n}.mp3").getall("APIC")[0].data).size == (720, 720)


def test_a_failed_cover_fetch_is_tried_again(monkeypatch, fresh_cache):
    answers = [None, _pillarboxed()]
    monkeypatch.setattr(covers, "fetch", lambda url: answers.pop(0))

    assert covers.squared("https://x.test/c.jpg") is None
    assert covers.squared("https://x.test/c.jpg") is not None


# --- what the survey remembers (L21) ---------------------------------------------

@pytest.fixture
def survey_file(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(covers, "_barred_cache", {})
    monkeypatch.setattr(covers, "_barred_loaded", False, raising=False)
    monkeypatch.setattr(covers, "_barred_dirty", False, raising=False)
    return tmp_path / "config" / ".cover-survey.json"


def test_the_survey_survives_a_restart(tmp_path, monkeypatch, survey_file):
    folder = _album(tmp_path, cover=_letterboxed())
    assert covers.barred(folder) is True
    covers.save_barred()

    # A restart: the memory is empty, and nothing has surveyed yet.
    monkeypatch.setattr(covers, "_barred_cache", {})
    monkeypatch.setattr(covers, "_barred_loaded", False)

    assert covers.barred_known(folder) is True


def test_a_squared_cover_is_no_longer_flagged(tmp_path, survey_file):
    folder = _album(tmp_path, cover=_letterboxed())
    assert covers.barred(folder) is True

    covers.apply(folder, sorted(folder.glob("*.mp3")), _letterboxed())

    assert covers.barred_known(folder) is False


def test_the_survey_file_forgets_albums_no_longer_there(tmp_path, survey_file):
    import json

    folder = _album(tmp_path, cover=_letterboxed())
    covers.barred(folder)
    covers._barred_cache["/gone/Artist/Album"] = (1, True)

    covers.save_barred(keep={str(folder)})

    assert list(json.loads(survey_file.read_text())) == [str(folder)]


def test_a_card_flagged_for_review_still_gets_its_cover_flag():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
          / "library.js").read_text(encoding="utf-8")
    assert 'node.querySelector(".tone-warn")' not in js
    assert 'node.querySelector(".flag-cover")' in js


# --- what a fetch will accept as a cover (L22) -----------------------------------

class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]


def _serve(monkeypatch, body):
    """Answer every fetch with body, with no network and no DNS."""
    class Opener:
        def open(self, request, timeout):
            return _Response(body)

    monkeypatch.setattr(covers.urllib.request, "build_opener", lambda *h: Opener())
    monkeypatch.setattr(covers.netguard, "check", lambda url: None)


def test_an_error_page_is_not_a_cover(monkeypatch):
    _serve(monkeypatch, b"<html>Not found</html>")
    assert covers.fetch("https://i.scdn.co/image/x") is None


def test_an_oversized_download_is_not_a_cover(monkeypatch):
    monkeypatch.setattr(covers, "MAX_COVER_BYTES", 1000, raising=False)
    _serve(monkeypatch, _pillarboxed())
    assert covers.fetch("https://i.scdn.co/image/x") is None


def test_a_real_image_is_a_cover(monkeypatch):
    body = _pillarboxed()
    _serve(monkeypatch, body)
    assert covers.fetch("https://i.scdn.co/image/x") == body


# --- never this network's own machines (L42) -----------------------------------

def test_a_cover_on_a_private_address_is_not_fetched(monkeypatch):
    opened = []
    monkeypatch.setattr(covers.urllib.request, "build_opener",
                        lambda *h: opened.append(1))
    assert covers.fetch("http://192.168.1.1/admin.jpg") is None
    assert opened == []


def test_a_redirect_is_checked_at_every_hop():
    import urllib.request

    from app import netguard

    handler = covers._CheckedRedirects(covers.CHOOSABLE_HOSTS)
    request = urllib.request.Request("https://i.scdn.co/image/x")
    with pytest.raises(netguard.Refused):
        handler.redirect_request(request, None, 302, "Found", {},
                                 "http://127.0.0.1:4533/rest/ping")
    with pytest.raises(netguard.Refused):
        handler.redirect_request(request, None, 302, "Found", {},
                                 "https://elsewhere.example/x.jpg")


def test_an_offered_host_is_held_to_its_list(monkeypatch):
    monkeypatch.setattr(covers.netguard, "check", lambda url: None)
    opened = []
    monkeypatch.setattr(covers.urllib.request, "build_opener",
                        lambda *h: opened.append(1))
    assert covers.fetch("https://example.com/x.jpg", covers.CHOOSABLE_HOSTS) is None
    assert opened == []


# --- covers whatever their case (2M12) ------------------------------------------

def test_folder_covers_are_found_whatever_their_case(tmp_path):
    """A rip made on Windows says Folder.jpg or cover.JPG. On the Pi a
    lower-case lookup missed it: Fetch cover embedded new art and reported
    success while Navidrome kept showing the file it never saw."""
    (tmp_path / "Folder.JPG").write_bytes(b"art")
    (tmp_path / "Front.png").write_bytes(b"art")
    shutil.copy(SILENCE, tmp_path / "01.mp3")

    assert [p.name for p in covers.folder_covers(tmp_path)] == ["Folder.JPG",
                                                                "Front.png"]
    _, found = covers._look(tmp_path)
    assert [entry.name for entry in found] == ["Folder.JPG", "Front.png"]
