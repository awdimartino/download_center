"""Resolves any URL yt-dlp understands into downloadable items.

This is the counterpart to the Spotify resolver. The difference is that there
is nothing to search for: the URL already names the exact audio, so items
produced here carry a direct_url and skip the matching stage entirely.

Metadata is whatever the site provides. YouTube Music and Bandcamp expose real
track, artist and album fields; a plain YouTube upload gives little more than a
video title and a channel name. Tags are written from what is available.
"""

from __future__ import annotations

import collections
import concurrent.futures
import logging
import re
from typing import Any

import yt_dlp

from . import downloader
from .config import settings

log = logging.getLogger("navidrome_companion.generic")


class ResolveError(Exception):
    """Raised when a URL cannot be turned into a track list."""


_URL = re.compile(r"^https?://", re.I)

# Titles routinely arrive as "Artist - Title (Official Video)". Splitting on a
# dash recovers the artist far more often than it gets it wrong, and the
# trailing noise is worth removing either way.
_NOISE = re.compile(
    r"\s*[\(\[]\s*(official\s*(music\s*)?(video|audio)|official|lyrics?|"
    r"lyric video|visualizer|audio|hd|hq|4k|mv|m/?v)\s*[\)\]]",
    re.I,
)
_SPLIT = re.compile(r"\s+[-\u2013\u2014]\s+")


def looks_like_url(text: str) -> bool:
    return bool(_URL.match(text.strip()))


def _clean_title(title: str) -> str:
    return _NOISE.sub("", title or "").strip()


def _split_artist_title(title: str, uploader: str | None) -> tuple[str, str]:
    """Best-effort split of "Artist - Title" into its parts."""
    cleaned = _clean_title(title)
    parts = _SPLIT.split(cleaned, maxsplit=1)
    if len(parts) == 2 and all(p.strip() for p in parts):
        return parts[0].strip(), parts[1].strip()
    # Channel names commonly end in " - Topic" on auto-generated YT Music
    # channels, which is a reliable artist name once the suffix is dropped.
    artist = re.sub(r"\s*-\s*Topic$", "", uploader or "").strip()
    return (artist or "Unknown Artist"), cleaned


def _strip_artist_prefix(title: str, artist: str) -> str:
    lowered, prefix = title.lower(), f"{artist.lower()} - "
    return title[len(prefix):].strip() if lowered.startswith(prefix) else title


def _thumbnail(info: dict[str, Any]) -> str | None:
    """The best thumbnail on offer.

    A playlist is read flat, and a flat entry carries only the `thumbnails`
    list - no `thumbnail` - so every track from a playlist arrived with no
    cover at all. The largest of them is usually a letterboxed 4:3 frame,
    which `covers.square` cuts back down to the cover.
    """
    if info.get("thumbnail"):
        return info["thumbnail"]
    sized = [t for t in info.get("thumbnails") or [] if t.get("url")]
    if not sized:
        return None
    best = max(sized, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
    return best["url"]


def _to_item(info: dict[str, Any]) -> dict[str, Any] | None:
    video_id = info.get("id")
    if not video_id:
        return None

    url = info.get("webpage_url") or info.get("url")
    if not url:
        return None

    # yt-dlp fills these in for music sources; they are absent for plain video.
    track = info.get("track")
    artist = info.get("artist") or info.get("creator")
    if track and artist:
        # Some sites repeat the artist inside the track field, which would
        # otherwise produce "Artist - Artist - Title" filenames.
        title, artist_name = _strip_artist_prefix(track, artist), artist
    else:
        artist_name, title = _split_artist_title(
            info.get("title", ""), info.get("uploader") or info.get("channel")
        )

    duration = info.get("duration")
    year = info.get("release_year")
    extractor = (info.get("extractor_key") or info.get("ie_key") or "web").lower()
    # The album's artist where the site gives one, else the track's first
    # credited artist - not the whole credit. "A, B" for a guest track filed
    # that one track under its own folder and album UUID (see `_as_album`).
    named = info.get("album_artists") or (
        [info["album_artist"]] if info.get("album_artist") else [])
    artists = info.get("artists") or []
    album_artist = (named[0] if named else artists[0] if artists else artist_name)
    number = info.get("track_number")

    return {
        # Namespaced so it can never collide with a bare Spotify id.
        "spotify_id": f"{extractor}:{video_id}",
        "isrc": None,
        "title": title or "Unknown Title",
        "artist": artist_name,
        "album_artist": album_artist,
        "album": info.get("album") or title or "Unknown Title",
        "album_id": None,
        "album_total": None,
        "track_no": int(number) if isinstance(number, int | float) else None,
        "disc_no": 1,
        "release_date": str(year) if year else None,
        "duration_ms": int(duration * 1000) if duration else None,
        "cover_url": _thumbnail(info),
        # Present means "already located": the worker skips matching.
        "direct_url": url,
    }


# Entries read in full at once. A few, not many: this is one request per
# track to the same site, and a burst is how a rate limit starts.
ENTRY_WORKERS = 3


def _options(**extra: Any) -> dict[str, Any]:
    """yt-dlp's options for reading, never downloading.

    With cookies.txt, as the download itself has: an age-gated or
    members-only video failed here though it would have downloaded.
    """
    options: dict[str, Any] = {"quiet": True, "no_warnings": True,
                               "skip_download": True,
                               "cachedir": str(downloader.YTDLP_CACHE), **extra}
    cookies = settings.cookies_file
    if cookies:
        options["cookiefile"] = str(cookies)
    return options


def _in_full(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each playlist entry's full metadata, in the playlist's order.

    A flat entry carries a title and little else - no track, artist or
    album - so a YouTube Music album was tagged as N one-track albums, often
    under Unknown Artist. The tags are written from these items before the
    download, so this is the last chance to know better. An entry that
    cannot be read in full keeps what the list said rather than failing the
    job: the download step will report it if it is really gone.
    """
    options = _options()

    def one(entry: dict[str, Any]) -> dict[str, Any]:
        link = entry.get("url") or entry.get("webpage_url")
        if not link:
            return entry
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                return ydl.extract_info(link, download=False) or entry
        except Exception as exc:
            log.info("kept the playlist's own details for %s: %s", link, exc)
            return entry

    with concurrent.futures.ThreadPoolExecutor(ENTRY_WORKERS) as pool:
        return list(pool.map(one, entries))


def _as_album(items: list[dict[str, Any]]) -> None:
    """Give an album read from a site one album artist and a running order.

    A YouTube Music or Bandcamp album arrives as a playlist whose entries all
    name the same album. Each was filed under its own track's artist, so a
    guest track went to a folder of its own with a second album UUID; and
    with no track numbers the album played in alphabetical order. A list of
    unrelated videos is left alone: those really are singles.
    """
    if len(items) < 2 or len({item["album"] for item in items}) != 1:
        return
    counts = collections.Counter(item["album_artist"] for item in items)
    album_artist = counts.most_common(1)[0][0]
    numbered = all(item["track_no"] for item in items)
    for position, item in enumerate(items, start=1):
        item["album_artist"] = album_artist
        item["album_total"] = len(items)
        if not numbered:
            # The playlist's own order is the album's.
            item["track_no"] = position


def resolve(url: str, limit: int | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Return (job title, items) for any yt-dlp supported URL.

    A playlist over `limit` entries is refused from the flat list, before
    each entry is read in full - one request per track.
    """
    # The list is read flat, to learn what is in it quickly; each entry is
    # then read in full by `_in_full`. Nothing fetches more metadata at
    # download time - what is resolved here is what gets tagged.
    options = _options(extract_flat="in_playlist")

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.UnsupportedError as exc:
        raise ResolveError("No downloader available for that site.") from exc
    except yt_dlp.utils.DownloadError as exc:
        raise ResolveError(str(exc).replace("\n", " ")[:200]) from exc
    except Exception as exc:
        raise ResolveError(f"{type(exc).__name__}: {exc}"[:200]) from exc

    if not info:
        raise ResolveError("Nothing found at that URL.")

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if limit is not None and len(entries) > limit:
            raise ResolveError(
                f"{info.get('title') or 'That playlist'} has {len(entries)} "
                f"tracks, more than the {limit} this can queue at once. "
                "Queue it in parts - by album, say.")
        entries = _in_full(entries)
        items = [item for item in (_to_item(e) for e in entries) if item]
        _as_album(items)
        title = info.get("title") or "Playlist"
    else:
        item = _to_item(info)
        items = [item] if item else []
        title = f"{items[0]['artist']} - {items[0]['title']}" if items else url

    if not items:
        raise ResolveError("That URL resolved to nothing downloadable.")

    return title, items
