"""Cover art: fetching it, squaring it, and putting it on the files.

Two jobs share this module. A download embeds whatever cover its source
offers, and for YouTube that is the video's thumbnail - a 16:9 frame with the
album's square cover in the middle and bars either side, or, for the smaller
4:3 thumbnails, bars on all four. `square` cuts the cover back out, so the
bars never reach the library.

The other is replacing art on an album that is already filed, without
touching anything else on it: the "Fetch cover" button. Retagging from
MusicBrainz brings art with it, but it also rewrites every tag and can move
the files, which is a lot to accept for a picture.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageStat

from . import uuidtags

log = logging.getLogger("navidrome_companion.covers")


# urllib honours file:// and ftp:// as happily as http. The URL comes from
# Spotify or from whatever yt-dlp scraped, so it is not ours to trust with a
# scheme that can read the filesystem.
ALLOWED_SCHEMES = ("http", "https")

# Where a cover picked in the browser may be fetched from. The URL arrives
# from the browser, and the server fetching any address it is handed would
# let a page probe the network the server sits on.
CHOOSABLE_HOSTS = ("i.scdn.co", "coverartarchive.org")

JPEG = "image/jpeg"


def fetch(url: str) -> bytes | None:
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        log.debug("refusing to fetch cover over %r", scheme)
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "navidrome-companion"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.read()
    except Exception as exc:
        log.debug("cover fetch failed for %s: %s", url, exc)
        return None


def choosable(url: str) -> bool:
    parsed = urllib.parse.urlparse(url or "")
    return parsed.scheme == "https" and parsed.hostname in CHOOSABLE_HOSTS


# --- squaring ---------------------------------------------------------------

# A band is a bar when it is this dark all the way through. Measured as a
# mean *and* a peak: a mean alone passes a dark cover with a bright logo.
_BAR_MEAN = 20
_BAR_PEAK = 60


def _is_bar(image: Image.Image, box: tuple[int, int, int, int]) -> bool:
    if box[2] <= box[0] or box[3] <= box[1]:
        return False
    stat = ImageStat.Stat(image.crop(box).convert("L"))
    return stat.mean[0] < _BAR_MEAN and stat.extrema[0][1] < _BAR_PEAK


def _crop_letterbox(image: Image.Image) -> Image.Image:
    """Take a 16:9 frame back out of a 4:3 thumbnail that letterboxed it.

    hqdefault and sddefault - what a playlist entry often carries - pad the
    widescreen frame with black above and below. Cut only to exactly 16:9
    and only when both bands really are black, so a 4:3 picture that is
    genuinely that shape is left alone.
    """
    width, height = image.size
    if abs(width / height - 4 / 3) > 0.02:
        return image
    band = (height - round(width * 9 / 16)) // 2
    if band <= 0:
        return image
    if (_is_bar(image, (0, 0, width, band))
            and _is_bar(image, (0, height - band, width, height))):
        return image.crop((0, band, width, height - band))
    return image


def square(data: bytes) -> tuple[bytes, str]:
    """The cover as a square JPEG, cut from the middle of whatever arrived.

    A square JPEG comes back untouched, byte for byte - which is every
    Spotify cover. Anything that cannot be read as an image also comes back
    untouched, with a JPEG label as before: a cover with bars is better than
    none, and none is what an exception here would cost.
    """
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        log.debug("cover is not a readable image: %s", exc)
        return data, JPEG

    if image.width == image.height and image.format == "JPEG":
        return data, JPEG

    image = _crop_letterbox(image.convert("RGB"))
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side))

    out = io.BytesIO()
    image.save(out, "JPEG", quality=92)
    return out.getvalue(), JPEG


def is_square(data: bytes) -> bool:
    """Square to within 2% - plenty of real covers are 1000x1002.

    Unreadable counts as square: this decides whether to offer a fix, and
    there is nothing to fix in an image that cannot be opened.
    """
    try:
        width, height = Image.open(io.BytesIO(data)).size
    except Exception:
        return True
    return abs(width - height) <= 0.02 * max(width, height)


# folder -> (newest mtime of its first track, barred). Reading a cover means
# reading a tag and an image header off the Pi's disk for every album, which
# is seconds over a whole library - so it is done once per file change.
_barred_cache: dict[str, tuple[int, bool]] = {}


def _look(folder: Path) -> tuple[list[os.DirEntry], list[os.DirEntry]]:
    """(the first audio file, the folder covers) from one directory listing.

    The survey used to walk each album recursively and then test twenty
    cover names twice - about 28 filesystem calls per album even when the
    answer was already known, some 70,000 for a library, on a Pi.
    """
    from .filer import COVER_NAMES, COVER_SUFFIXES

    try:
        with os.scandir(folder) as listing:
            entries = {entry.name: entry for entry in listing}
    except OSError:
        return [], []
    covers = [entries[name + suffix] for name in COVER_NAMES
              for suffix in COVER_SUFFIXES
              if name + suffix in entries
              and entries[name + suffix].is_file()]
    tracks = sorted((e for e in entries.values()
                     if e.is_file() and uuidtags.is_audio(Path(e.name))),
                    key=lambda e: e.name)
    return tracks[:1], covers


def barred(folder: Path) -> bool | None:
    """Whether the album's cover is the wrong shape. None if it has none.

    Asked of the first track only, plus any folder cover: the filer puts one
    album in one folder, and a download writes the same cover on every track.
    """
    from .filer import audio_in

    first, found = _look(folder)
    if first:
        tracks = [Path(first[0].path)]
    else:
        # Nothing at the top: a multi-disc album keeps its tracks below.
        tracks = audio_in(folder)[:1]
        if not tracks:
            return None
    try:
        stamp = max([first[0].stat().st_mtime_ns if first
                     else tracks[0].stat().st_mtime_ns]
                    + [entry.stat().st_mtime_ns for entry in found])
    except OSError:
        return None
    hit = _barred_cache.get(str(folder))
    if hit and hit[0] == stamp:
        return hit[1]
    data = current(folder, tracks, [Path(entry.path) for entry in found])
    result = None if data is None else not is_square(data)
    if result is not None:
        _barred_cache[str(folder)] = (stamp, result)
    return result


def barred_known(folder: Path) -> bool:
    """What the survey last found, without reading anything. For the list,
    which must not open a file per row to draw a flag."""
    hit = _barred_cache.get(str(folder))
    return bool(hit and hit[1])


def preview(data: bytes, size: int = 300) -> str:
    """A small data: URL of an image, for showing before it is applied."""
    image = Image.open(io.BytesIO(data)).convert("RGB")
    image.thumbnail((size, size))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


# --- the files --------------------------------------------------------------

class NotEmbeddable(Exception):
    """The file is in a format this cannot put a cover on."""


def embedded(path: Path) -> bytes | None:
    """The front cover a file carries, or the first picture if none is marked."""
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4

    try:
        audio = MutagenFile(path)
    except Exception:
        return None
    if audio is None or audio.tags is None and not isinstance(audio, FLAC):
        return None

    if isinstance(audio.tags, ID3):
        pictures = [(p.type, p.data) for p in audio.tags.getall("APIC")]
    elif isinstance(audio, FLAC):
        pictures = [(p.type, p.data) for p in audio.pictures]
    elif isinstance(audio, MP4):
        pictures = [(3, bytes(p)) for p in audio.tags.get("covr") or []]
    else:
        return None
    if not pictures:
        return None
    pictures.sort(key=lambda p: p[0] != 3)
    return pictures[0][1]


def embed(path: Path, data: bytes, mime: str = JPEG) -> None:
    """Replace every picture on the file with this one, as its front cover.

    Leaves every other tag alone - including the track and album UUIDs the
    filer keyed stars and play counts to.
    """
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, ID3
    from mutagen.mp4 import MP4, MP4Cover

    try:
        audio = MutagenFile(path)
    except Exception as exc:
        raise NotEmbeddable(f"{type(exc).__name__}: {exc}") from exc
    if audio is None:
        raise NotEmbeddable("not an audio format this can read")

    if isinstance(audio, FLAC):
        picture = Picture()
        picture.type, picture.mime, picture.desc, picture.data = 3, mime, "Cover", data
        audio.clear_pictures()
        audio.add_picture(picture)
    elif isinstance(audio, MP4):
        if audio.tags is None:
            audio.add_tags()
        kind = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio.tags["covr"] = [MP4Cover(data, imageformat=kind)]
    elif audio.tags is None or isinstance(audio.tags, ID3):
        if audio.tags is None:
            audio.add_tags()
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    else:
        raise NotEmbeddable(f"cannot embed a cover in {type(audio).__name__}")

    try:
        audio.save()
    except Exception as exc:
        raise NotEmbeddable(f"{type(exc).__name__}: {exc}") from exc


def folder_covers(folder: Path) -> list[Path]:
    from .filer import COVER_NAMES, COVER_SUFFIXES

    return [folder / f"{name}{suffix}"
            for name in COVER_NAMES for suffix in COVER_SUFFIXES
            if (folder / f"{name}{suffix}").is_file()]


def current(folder: Path, tracks: list[Path],
            found_covers: list[Path] | None = None) -> bytes | None:
    """The cover Navidrome shows for this album.

    A folder cover first, because that is what Navidrome prefers by default:
    replacing only the embedded art under a cover.jpg changes nothing anybody
    can see. `found_covers` is the folder covers when the caller has
    already listed them.
    """
    for found in (folder_covers(folder) if found_covers is None
                  else found_covers):
        try:
            return found.read_bytes()
        except OSError:
            continue
    for track in tracks:
        data = embedded(track)
        if data:
            return data
    return None


def apply(folder: Path, tracks: list[Path], data: bytes) -> dict[str, Any]:
    """Put one cover on every track of an album, and over its folder cover."""
    data, mime = square(data)
    written, failed = 0, []
    for track in tracks:
        try:
            embed(track, data, mime)
            written += 1
        except NotEmbeddable as exc:
            failed.append(f"{track.name}: {exc}")

    # Replaced rather than left: Navidrome reads a folder cover ahead of the
    # embedded one, so a stale cover.jpg would hide everything written above.
    covers = folder_covers(folder)
    for old in covers:
        try:
            old.unlink()
        except OSError as exc:
            failed.append(f"{old.name}: {exc}")
    if covers:
        (folder / "cover.jpg").write_bytes(data)

    return {"written": written, "failed": failed}


# --- choosing one ------------------------------------------------------------

def _musicbrainz_album_id(tracks: list[Path]) -> str | None:
    from mutagen import File as MutagenFile

    for track in tracks:
        try:
            audio = MutagenFile(track, easy=True)
        except Exception:
            continue
        if audio is not None and audio.tags is not None:
            found = (audio.tags.get("musicbrainz_albumid") or [""])[0]
            if found:
                return found
    return None


def candidates(folder: Path, tracks: list[Path], artist: str,
               album: str) -> list[dict[str, Any]]:
    """Covers this album could have, best guess first.

    The current cover squared, when it has bars to lose - that alone is the
    fix for anything YouTube already delivered. Then the Cover Art Archive's
    front for the release MusicBrainz matched, and Spotify's search.
    """
    from . import spotify

    found: list[dict[str, Any]] = []

    now = current(folder, tracks)
    if now and not is_square(now):
        try:
            found.append({"source": "current", "url": None,
                          "label": "The current cover, squared",
                          "detail": "Bars trimmed off the sides",
                          "preview": preview(square(now)[0])})
        except Exception as exc:
            log.debug("could not preview the current cover: %s", exc)

    release = _musicbrainz_album_id(tracks)
    if release:
        url = f"https://coverartarchive.org/release/{release}/front-500"
        found.append({"source": "musicbrainz", "url": url,
                      "label": "MusicBrainz", "detail": "The matched release",
                      "preview": url})

    query = " ".join(part for part in (artist, album) if part)
    if query:
        try:
            albums = spotify.search(query, "album", limit=8)
        except Exception as exc:
            # Not configured, or not answering: the other sources still help.
            log.debug("spotify cover search failed: %s", exc)
            albums = []
        seen = set()
        for item in albums:
            card = spotify.album_card(item)
            if not card["cover"] or card["cover"] in seen:
                continue
            seen.add(card["cover"])
            found.append({
                "source": "spotify", "url": card["cover"],
                "label": card["name"],
                "detail": " · ".join(p for p in (card["artist"], card["year"]) if p),
                "preview": card["cover"]})
    return found
