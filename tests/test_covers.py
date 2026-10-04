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
