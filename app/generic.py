"""Resolves any URL yt-dlp understands into downloadable items.

This is the counterpart to the Spotify resolver. The difference is that there
is nothing to search for: the URL already names the exact audio, so items
produced here carry a direct_url and skip the matching stage entirely.

Metadata is whatever the site provides. YouTube Music and Bandcamp expose real
track, artist and album fields; a plain YouTube upload gives little more than a
video title and a channel name. Tags are written from what is available.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import yt_dlp

log = logging.getLogger("download_center.generic")


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

    return {
        # Namespaced so it can never collide with a bare Spotify id.
        "spotify_id": f"{extractor}:{video_id}",
        "isrc": None,
        "title": title or "Unknown Title",
        "artist": artist_name,
        "album_artist": artist_name,
        "album": info.get("album") or title or "Unknown Title",
        # No album grouping: without a reliable track count there is no way to
        # know a release is complete, so these are always staged as singles.
        "album_id": None,
        "album_total": None,
        "track_no": None,
        "disc_no": 1,
        "release_date": str(year) if year else None,
        "duration_ms": int(duration * 1000) if duration else None,
        "cover_url": info.get("thumbnail"),
        # Present means "already located": the worker skips matching.
        "direct_url": url,
    }


def resolve(url: str) -> tuple[str, list[dict[str, Any]]]:
    """Return (job title, items) for any yt-dlp supported URL."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # Playlist entries are read shallowly; full metadata is fetched at
        # download time anyway, and resolving a 200 item playlist eagerly
        # would take minutes.
        "extract_flat": "in_playlist",
    }

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
        items = [item for item in (_to_item(e) for e in entries) if item]
        title = info.get("title") or "Playlist"
    else:
        item = _to_item(info)
        items = [item] if item else []
        title = f"{items[0]['artist']} - {items[0]['title']}" if items else url

    if not items:
        raise ResolveError("That URL resolved to nothing downloadable.")

    return title, items
