"""Turns a Spotify URL into a flat list of tracks with full metadata.

Everything downstream consumes the dictionaries produced here, so this module
is the single place that knows the shape of Spotify's API responses.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Iterator

import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyClientCredentials

from .config import CONFIG_DIR, settings

# Matches both URL and URI forms, tolerating the /intl-xx/ locale segment that
# Spotify inserts when a link is copied from a non-English client.
_LINK = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?|spotify:)"
    r"(track|album|playlist|artist)[/:]([A-Za-z0-9]+)"
)

SUPPORTED = ("track", "album", "playlist")


class ResolveError(Exception):
    """Raised when a link cannot be turned into a track list."""


_client: spotipy.Spotify | None = None
_client_lock = threading.Lock()


def client() -> spotipy.Spotify:
    global _client
    with _client_lock:
        if _client is None:
            if not settings.spotify_configured:
                raise ResolveError(
                    "Spotify credentials are not set. Add spotify_client_id and "
                    "spotify_client_secret to config/config.toml."
                )
            _client = spotipy.Spotify(
                auth_manager=SpotifyClientCredentials(
                    client_id=settings.spotify_client_id,
                    client_secret=settings.spotify_client_secret,
                    # Default is the working directory, which is not writable
                    # in the container; keep it on the config volume instead.
                    cache_handler=CacheFileHandler(
                        cache_path=str(CONFIG_DIR / ".spotipy-cache")
                    ),
                ),
                requests_timeout=15,
                retries=3,
            )
        return _client


def parse_link(text: str) -> tuple[str, str]:
    """Return (kind, spotify_id) for a supported link, else raise."""
    match = _LINK.search(text.strip())
    if not match:
        raise ResolveError(
            "Not a recognised Spotify link. Paste a track, album, or playlist URL."
        )
    kind, spotify_id = match.group(1), match.group(2)
    if kind == "artist":
        raise ResolveError(
            "Artist links are not supported. Paste one of the artist's albums instead."
        )
    if kind not in SUPPORTED:
        raise ResolveError(f"Unsupported link type: {kind}")
    return kind, spotify_id


def _artist_names(artists: list[dict] | None) -> str:
    return ", ".join(a["name"] for a in (artists or []) if a.get("name"))


def _cover(album: dict) -> str | None:
    images = album.get("images") or []
    return images[0]["url"] if images else None


def to_track(full: dict) -> dict[str, Any]:
    """Normalise a full Spotify track object into our item shape."""
    album = full.get("album") or {}
    return {
        "spotify_id": full["id"],
        "isrc": (full.get("external_ids") or {}).get("isrc"),
        "title": full["name"],
        "artist": _artist_names(full.get("artists")),
        "album_artist": _artist_names(album.get("artists")) or _artist_names(full.get("artists")),
        "album": album.get("name") or full["name"],
        "album_id": album.get("id"),
        "album_total": album.get("total_tracks"),
        "track_no": full.get("track_number"),
        "disc_no": full.get("disc_number") or 1,
        "release_date": album.get("release_date"),
        "duration_ms": full.get("duration_ms"),
        "cover_url": _cover(album),
    }


def _paginate(first: dict, sp: spotipy.Spotify) -> Iterator[dict]:
    """Walk a paged Spotify response, yielding every item."""
    page = first
    while page:
        yield from page.get("items", [])
        page = sp.next(page) if page.get("next") else None


def _hydrate(sp: spotipy.Spotify, track_ids: list[str]) -> list[dict[str, Any]]:
    """Fetch full track objects in batches, preserving order.

    Album and playlist endpoints return simplified tracks that omit ISRC, and
    ISRC is the strongest signal beets has for matching against MusicBrainz,
    so it is worth the extra request per fifty tracks to collect it.
    """
    tracks: list[dict[str, Any]] = []
    for start in range(0, len(track_ids), 50):
        batch = track_ids[start:start + 50]
        for full in sp.tracks(batch)["tracks"]:
            if full:
                tracks.append(to_track(full))
    return tracks


def resolve(kind: str, spotify_id: str) -> tuple[str, list[dict[str, Any]]]:
    """Return (job title, tracks) for a parsed link."""
    sp = client()

    if kind == "track":
        full = sp.track(spotify_id)
        track = to_track(full)
        return f"{track['artist']} - {track['title']}", [track]

    if kind == "album":
        album = sp.album(spotify_id)
        ids = [t["id"] for t in _paginate(album["tracks"], sp) if t and t.get("id")]
        title = f"{_artist_names(album.get('artists'))} - {album['name']}"
        return title, _hydrate(sp, ids)

    # Playlist entries may be podcast episodes or local files, neither of which
    # can be downloaded; both surface with a null or non-track payload.
    playlist = sp.playlist(spotify_id)
    ids = [
        entry["track"]["id"]
        for entry in _paginate(playlist["tracks"], sp)
        if entry and entry.get("track")
        and entry["track"].get("id")
        and entry["track"].get("type") == "track"
    ]
    return playlist["name"], _hydrate(sp, ids)


def resolve_link(text: str) -> tuple[str, str, list[dict[str, Any]]]:
    """Parse then resolve. Returns (kind, title, tracks)."""
    kind, spotify_id = parse_link(text)
    title, tracks = resolve(kind, spotify_id)
    if not tracks:
        raise ResolveError("That link resolved to zero downloadable tracks.")
    return kind, title, tracks


# --- reserved for the in-app browser --------------------------------------
# Not called yet. Kept here so the browser becomes two endpoints and a view
# rather than a restructuring of this module.

def search(query: str, kind: str = "album", limit: int = 20) -> list[dict[str, Any]]:
    results = client().search(q=query, type=kind, limit=limit)
    return results.get(f"{kind}s", {}).get("items", [])


def get_album(album_id: str) -> dict[str, Any]:
    return client().album(album_id)


def reset_client() -> None:
    """Drop the cached client so new credentials take effect immediately."""
    global _client
    with _client_lock:
        _client = None
