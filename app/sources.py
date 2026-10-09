"""Where a Spotify track's audio can come from, and which copy is best.

YouTube Music first: the biggest catalogue, with real album metadata. When
it has no recording good enough, or its download is refused (a 403 from
YouTube is the commonest failure), the track tries SoundCloud, then
Bandcamp. When YouTube Music and SoundCloud both have a *confirmed* match,
the one that sounds better is taken (`quality`) - usually YouTube's Opus,
but a SoundCloud artist who allows downloads offers the original file. Much of what YouTube Music lacks is music that lives on those two
- independent releases, SoundCloud-native producers - and both are free to
stream at about 128 kbps.

Every source is judged by `matcher.score`: title, artist and running time
against what Spotify says, with a penalty for "remix", "sped up" and the
rest. The fallbacks are held to one rule YouTube Music is not: the running
time must be known and within `matcher.DURATION_TOLERANCE`. Major-label
tracks on SoundCloud are 30-second previews with the right title and the
right artist, and nothing else would stop one being filed as the song.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
import yt_dlp

from . import downloader, generic, matcher

log = logging.getLogger("navidrome_companion.sources")

ORDER = ("youtube", "soundcloud", "bandcamp")
NAMES = {"youtube": "YouTube Music", "soundcloud": "SoundCloud",
         "bandcamp": "Bandcamp"}

# Searched for every track, so that when both have the song the better
# sounding copy can win (see `quality`). Bandcamp is not: its free stream is
# a 128 kbps MP3, which never beats YouTube Music's Opus, so it is only asked
# once these two have nothing.
COMPARED = ("youtube", "soundcloud")

# A match good enough that the choice between sources can be made on sound
# alone: title and artist agree, and the length is within a few seconds
# (the duration component is 1 - seconds off / tolerance, so 0.8 is 3s).
CONFIRMED_SCORE = 0.90
CONFIRMED_DURATION = 0.80

# What a kbps is worth in each codec, as MP3 kbps. Opus at 128 sounds about
# as good as MP3 at 192; ranking on raw bitrate would prefer SoundCloud's
# 128 kbps MP3 to YouTube's 130 kbps Opus, which is backwards.
CODEC_WORTH = {"opus": 1.5, "aac": 1.3, "mp4a": 1.3, "vorbis": 1.2, "mp3": 1.0}
LOSSLESS = {"flac", "wav", "alac", "aiff", "pcm"}
# A lossless original counts as CD quality.
LOSSLESS_KBPS = 1411.0

SOUNDCLOUD_RESULTS = 10
BANDCAMP_SEARCH = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"
# Bandcamp's search does not say how long a track is, and each page that does
# is a request, so only the best few names are opened.
BANDCAMP_OPENED = 3
TIMEOUT = 15
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) navidrome-companion"}

Found = tuple[str, float, dict[str, float]]


def find(source: str, track: dict[str, Any]) -> Found:
    """(url, score, components) for the best recording on one source.

    Raises `matcher.MatchError` when the source was asked and had nothing
    good enough, and `matcher.SearchUnavailable` when it could not be asked.
    """
    if source == "youtube":
        return matcher.find(track)
    if source == "soundcloud":
        return _soundcloud(track)
    if source == "bandcamp":
        return _bandcamp(track)
    raise ValueError(f"unknown source {source!r}")


def confirmed(score: float, parts: dict[str, float]) -> bool:
    """Whether a match is certain enough to choose between sources by sound."""
    return score >= CONFIRMED_SCORE and parts.get("duration", 0) >= CONFIRMED_DURATION


def worth(fmt: dict[str, Any]) -> float:
    """One format's sound, as MP3-equivalent kbps; 0 when it does not say."""
    codec = (fmt.get("acodec") or "").lower().split(".")[0]
    ext = (fmt.get("ext") or "").lower()
    if codec in LOSSLESS or ext in LOSSLESS:
        return LOSSLESS_KBPS
    if codec == "none":
        return 0.0
    kbps = fmt.get("abr") or fmt.get("tbr") or 0.0
    return float(kbps) * CODEC_WORTH.get(codec or ext, 1.0)


def quality(url: str) -> float:
    """The best audio a URL offers, as MP3-equivalent kbps.

    Read from the formats yt-dlp would choose between, without downloading.
    A SoundCloud upload whose artist allows downloads offers the original
    file, often lossless, which is the case this exists for. 0 when it
    cannot be read: unknown is ranked last, never refused.
    """
    try:
        with _ydl() as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception as exc:
        log.debug("cannot read the formats of %s: %s", url, exc)
        return 0.0
    formats = info.get("formats") or [info]
    return max((worth(fmt) for fmt in formats), default=0.0)


def _query(track: dict[str, Any]) -> str:
    return f"{track['artist']} {track['title']}"


def _best(results: list[dict[str, Any]], track: dict[str, Any], name: str) -> Found:
    """The highest scorer whose running time is close enough, or MatchError."""
    target = (track.get("duration_ms") or 0) / 1000.0
    scored = []
    for result in results:
        seconds = result.get("duration_seconds")
        if not seconds or (target and abs(seconds - target) > matcher.DURATION_TOLERANCE):
            continue
        total, parts = matcher.score(result, track)
        scored.append((total, parts, result))
    if not scored:
        raise matcher.MatchError(
            f"Nothing on {name} with the right title, artist and length.")
    best_score, parts, best = max(scored, key=lambda row: row[0])
    if best_score < matcher.SCORE_FLOOR:
        raise matcher.MatchError(
            f"Best candidate on {name} scored {best_score:.2f}, below the "
            f"{matcher.SCORE_FLOOR:.2f} floor (“{best.get('title', '?')}”).")
    return best["url"], best_score, parts


# --- SoundCloud ------------------------------------------------------------

def _ydl(**options: Any) -> yt_dlp.YoutubeDL:
    """yt-dlp for reading, with the cache and cookies.txt a download has."""
    return yt_dlp.YoutubeDL(generic._options(logger=downloader._QuietLogger(),
                                             **options))


def _soundcloud_entries(query: str) -> list[dict[str, Any]]:
    """yt-dlp's SoundCloud search, flat: title, uploader, length and URL per
    track, from one request."""
    try:
        with _ydl(extract_flat="in_playlist") as ydl:
            info = ydl.extract_info(f"scsearch{SOUNDCLOUD_RESULTS}:{query}",
                                    download=False)
    except Exception as exc:
        raise matcher.SearchUnavailable(f"SoundCloud: {exc}"[:200]) from exc
    return [entry for entry in (info or {}).get("entries") or [] if entry]


# How a re-upload joins artist and title, in its title.
SEPARATORS = (" - ", " – ", " — ", " | ", " ● ")


def soundcloud_readings(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """One SoundCloud upload, read as matcher results.

    The uploader is usually the artist. A re-upload says who it is by in
    the title instead - "Radiohead - Karma Police", or "Teardrop ● Massive
    Attack" the other way round, by somebody else - so both readings are
    offered too, and the scorer keeps whichever fits.
    """
    title = entry.get("title") or ""
    base = {"duration_seconds": entry.get("duration"),
            "url": entry.get("webpage_url") or entry.get("url"),
            "resultType": "song"}
    readings = [{**base, "title": title,
                 "artists": [{"name": entry.get("uploader") or ""}]}]
    for separator in SEPARATORS:
        if separator in title:
            left, _, right = title.partition(separator)
            readings.append({**base, "title": right, "artists": [{"name": left}]})
            readings.append({**base, "title": left, "artists": [{"name": right}]})
            break
    return readings


def _soundcloud(track: dict[str, Any]) -> Found:
    results = [reading for entry in _soundcloud_entries(_query(track))
               for reading in soundcloud_readings(entry)]
    return _best(results, track, NAMES["soundcloud"])


# --- Bandcamp ----------------------------------------------------------------

def _bandcamp_search(text: str) -> list[dict[str, Any]]:
    """Tracks from the search Bandcamp's own search box uses.

    Its search *page* sits behind a JavaScript challenge; this endpoint
    answers with JSON, at least from most addresses. An HTML answer is the
    challenge, which is "could not ask", not "nothing there".
    """
    try:
        response = requests.post(
            BANDCAMP_SEARCH, timeout=TIMEOUT, headers=HEADERS,
            json={"search_text": text, "search_filter": "t",
                  "full_page": False, "fan_id": None})
        response.raise_for_status()
        data = response.json()
    except ValueError as exc:
        raise matcher.SearchUnavailable(
            "Bandcamp answered with a challenge page rather than results") from exc
    except requests.RequestException as exc:
        raise matcher.SearchUnavailable(f"Bandcamp: {exc}"[:200]) from exc
    return [item for item in (data.get("auto") or {}).get("results") or []
            if item.get("type") == "t" and item.get("item_url_path")]


def _bandcamp_duration(url: str) -> float | None:
    """How long a Bandcamp track is, from its page. None when it cannot be
    played for free, which is a track to pass over, not an error."""
    try:
        with _ydl() as ydl:
            return (ydl.extract_info(url, download=False) or {}).get("duration")
    except Exception as exc:
        log.debug("cannot open %s: %s", url, exc)
        return None


def bandcamp_reading(item: dict[str, Any]) -> dict[str, Any]:
    return {"title": item.get("name") or "",
            "artists": [{"name": item.get("band_name") or ""}],
            "album": {"name": item.get("album_name") or ""},
            "url": item["item_url_path"], "resultType": "song"}


def _bandcamp(track: dict[str, Any]) -> Found:
    found = _bandcamp_search(_query(track)) or _bandcamp_search(track["title"])
    # Ranked by name first, since only a few pages are opened for their
    # running time; a name that fails the title or artist gate never is.
    named = []
    for item in found:
        reading = bandcamp_reading(item)
        total, parts = matcher.score(reading, track)
        if not parts["gated"]:
            named.append((total, reading))
    named.sort(key=lambda row: row[0], reverse=True)
    opened = []
    for _total, reading in named[:BANDCAMP_OPENED]:
        reading["duration_seconds"] = _bandcamp_duration(reading["url"])
        opened.append(reading)
    return _best(opened, track, NAMES["bandcamp"])
