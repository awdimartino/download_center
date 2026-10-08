"""Turns a Spotify URL into a flat list of tracks with full metadata.

Everything downstream consumes the dictionaries produced here, so this module
is the single place that knows the shape of Spotify's API responses.
"""

from __future__ import annotations

import re
import threading
from typing import Any
from collections.abc import Iterator

import requests
import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyClientCredentials
from urllib3.util.retry import Retry

from .config import CONFIG_DIR, settings

# Matches both URL and URI forms, tolerating the /intl-xx/ locale segment that
# Spotify inserts when a link is copied from a non-English client, and the
# /embed/ one in a player's share code.
_LINK = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?(?:embed/)?|spotify:)"
    r"(track|album|playlist|artist)[/:]([A-Za-z0-9]+)"
)

# The phone app's share sheet hands out spotify.link short links, which only
# say what they are once followed.
_SHORT = re.compile(r"^(?:https?://)?spotify\.link/\S+$", re.I)

SUPPORTED = ("track", "album", "playlist")


class ResolveError(Exception):
    """Raised when a link cannot be turned into a track list."""


_client: spotipy.Spotify | None = None
_client_lock = threading.Lock()


def _session() -> requests.Session:
    """A session whose retries do not wait for Spotify's Retry-After.

    spotipy's own retries honour the header, and urllib3 caps it at six
    hours per retry: one long rate limit held a thread for the whole wait,
    per search and per resolve, and cancelling the job did not free it. A
    short backoff then a clear failure is better; the caller says Spotify is
    busy, and the person tries again.
    """
    retry = Retry(
        total=3, connect=None, read=False, status=3,
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]),
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        respect_retry_after_header=False)
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


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
                requests_session=_session(),
            )
        return _client


def is_spotify(text: str) -> bool:
    """Whether text is a Spotify link in any of its forms."""
    text = text.strip()
    return bool(_LINK.search(text) or _SHORT.match(text))


def canonical(text: str) -> str | None:
    """The plain https form of a Spotify link, or None if it is not one.

    A job remembers the link it came from, and Browse finds an album's job
    by that link, so a URI or an embed link is stored the way a copied
    link would be.
    """
    match = _LINK.search(text.strip())
    if not match:
        return None
    return f"https://open.spotify.com/{match.group(1)}/{match.group(2)}"


def expand_short(text: str) -> str:
    """Follow a spotify.link short link to the open.spotify.com one."""
    url = text.strip()
    if not url.lower().startswith("http"):
        url = "https://" + url
    try:
        response = requests.get(url, timeout=15, allow_redirects=True)
    except requests.RequestException as exc:
        raise ResolveError(f"Could not follow the short link: {exc}") from exc
    # Usually a redirect; failing that, the page names the link it stands for.
    for place in (response.url, response.text[:200_000]):
        found = canonical(place or "")
        if found:
            return found
    raise ResolveError("That short link does not lead to a Spotify track, album or playlist.")


def is_short(text: str) -> bool:
    return bool(_SHORT.match(text.strip()))


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


def _primary_artist(artists: list[dict] | None) -> str:
    """Just the first credited artist.

    Kept separate from the full credit because the two are wanted for
    different things, and conflating them cost this project every album with
    a guest on one track. beets decides an album is a Various Artists release
    when its tracks do not agree on `artist` - one "Michael Jackson, Paul
    McCartney" among eight "Michael Jackson" was enough - and then searches
    MusicBrainz for a compilation, so the real record never appears among the
    candidates. Verified against Thriller: as staged, five Various Artists
    candidates and a best distance of 0.41, refused; with this one tag
    normalised, Michael Jackson - Thriller at 0.01.

    Taken from Spotify's structured list rather than split off the joined
    string, because "Tyler, The Creator" is one artist with a comma in it.
    """
    for artist in artists or []:
        if artist.get("name"):
            return artist["name"]
    return ""


def _first_artist_id(artists: list[dict] | None) -> str | None:
    """The lead artist's id, for a link from a card to that artist's page."""
    for artist in artists or []:
        if artist.get("id"):
            return artist["id"]
    return None


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
        # What goes in the file's artist tag. The full credit above is for
        # the browser and for the YouTube Music search, where naming the
        # guest helps find the right recording; the tag has to agree with
        # this track's siblings or beets reads the album as a compilation.
        "primary_artist": _primary_artist(full.get("artists")),
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


def too_many(title: str, count: int, limit: int) -> ResolveError:
    return ResolveError(
        f"{title} has {count} tracks, more than the {limit} this can queue "
        "at once. Queue it in parts - by album, say.")


def resolve(kind: str, spotify_id: str,
            limit: int | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Return (job title, tracks) for a parsed link.

    Over `limit` tracks is refused from the first page's total, before
    the rest is fetched: a 10,000-track playlist was about 200 requests
    to Spotify only to be turned down afterwards.
    """
    sp = client()

    def check(title: str, page: dict) -> None:
        total = page.get("total") or 0
        if limit is not None and total > limit:
            raise too_many(title, total, limit)

    if kind == "track":
        full = sp.track(spotify_id)
        track = to_track(full)
        return f"{track['artist']} - {track['title']}", [track]

    if kind == "album":
        album = sp.album(spotify_id)
        title = f"{_artist_names(album.get('artists'))} - {album['name']}"
        check(title, album["tracks"])
        ids = [t["id"] for t in _paginate(album["tracks"], sp) if t and t.get("id")]
        return title, _hydrate(sp, ids)

    # Playlist entries may be podcast episodes or local files, neither of which
    # can be downloaded; both surface with a null or non-track payload.
    playlist = sp.playlist(spotify_id)
    check(playlist["name"], playlist["tracks"])
    ids = [
        entry["track"]["id"]
        for entry in _paginate(playlist["tracks"], sp)
        if entry and entry.get("track")
        and entry["track"].get("id")
        and entry["track"].get("type") == "track"
    ]
    return playlist["name"], _hydrate(sp, ids)


def resolve_link(text: str, limit: int | None = None,
                 ) -> tuple[str, str, list[dict[str, Any]]]:
    """Parse then resolve. Returns (kind, title, tracks)."""
    if is_short(text):
        text = expand_short(text)
    kind, spotify_id = parse_link(text)
    title, tracks = resolve(kind, spotify_id, limit)
    if not tracks:
        raise ResolveError("That link resolved to zero downloadable tracks.")
    return kind, title, tracks


# --- browsing -------------------------------------------------------------

def search(query: str, kind: str = "album", limit: int = 20) -> list[dict[str, Any]]:
    results = client().search(q=query, type=kind, limit=limit)
    return results.get(f"{kind}s", {}).get("items", [])


def _year(release_date: str | None) -> str | None:
    return release_date.split("-")[0] if release_date else None


def album_card(album: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": album["id"],
        "name": album["name"],
        "artist": _artist_names(album.get("artists")),
        # For the "in library" count, the same as on a track card: the
        # library may have filed it under either credit.
        "primary_artist": _primary_artist(album.get("artists")),
        "artist_id": _first_artist_id(album.get("artists")),
        "year": _year(album.get("release_date")),
        "cover": _cover(album),
        "total": album.get("total_tracks"),
        "type": album.get("album_type"),
        "url": (album.get("external_urls") or {}).get("spotify"),
    }


def track_card(track: dict[str, Any]) -> dict[str, Any]:
    album = track.get("album") or {}
    return {
        "id": track["id"],
        "name": track["name"],
        "artist": _artist_names(track.get("artists")),
        # What the file's artist tag says, which is not the full credit - see
        # `_primary_artist`. Browse's "in library" check needs both, because
        # the card shows one and the library holds the other.
        "primary_artist": _primary_artist(track.get("artists")),
        "album": album.get("name"),
        # So the album name on a song can open that album.
        "album_id": album.get("id"),
        "year": _year(album.get("release_date")),
        "cover": _cover(album),
        "duration_ms": track.get("duration_ms"),
        "url": (track.get("external_urls") or {}).get("spotify"),
    }


def artist_card(artist: dict[str, Any]) -> dict[str, Any]:
    images = artist.get("images") or []
    return {
        "id": artist["id"],
        "name": artist["name"],
        "cover": images[0]["url"] if images else None,
        "followers": (artist.get("followers") or {}).get("total"),
        "genres": (artist.get("genres") or [])[:2],
    }


def browse(query: str, kind: str, limit: int = 24) -> list[dict[str, Any]]:
    """Search Spotify and return cards of the requested kind."""
    if kind not in ("album", "track", "artist"):
        raise ResolveError(f"Cannot search for {kind!r}.")
    items = [i for i in search(query, kind, limit) if i]
    shaper = {"album": album_card, "track": track_card, "artist": artist_card}[kind]
    return [shaper(item) for item in items]


def browse_all(query: str, limit: int = 10) -> dict[str, list[dict[str, Any]]]:
    """Albums, tracks and artists for one query, in one request.

    What Browse shows before you narrow it to a kind: a title you remember is
    as often a song as an album, and asking three times would be three round
    trips to Spotify from a Pi for one keystroke's worth of results.
    """
    results = client().search(q=query, type="album,track,artist", limit=limit)

    def items(kind: str) -> list[dict[str, Any]]:
        return [i for i in (results.get(f"{kind}s") or {}).get("items", []) if i]

    return {
        "albums": [album_card(a) for a in items("album")],
        "tracks": [track_card(t) for t in items("track")],
        "artists": [artist_card(a) for a in items("artist")],
    }


def album_detail(album_id: str) -> dict[str, Any]:
    """An album card plus its track listing."""
    sp = client()
    album = sp.album(album_id)
    card = album_card(album)
    card["tracks"] = [
        {
            "id": track["id"],
            "name": track["name"],
            "artist": _artist_names(track.get("artists")),
            "primary_artist": _primary_artist(track.get("artists")),
            "track_no": track.get("track_number"),
            "disc_no": track.get("disc_number") or 1,
            "duration_ms": track.get("duration_ms"),
            "url": (track.get("external_urls") or {}).get("spotify"),
        }
        for track in _paginate(album["tracks"], sp)
        if track and track.get("id")
    ]
    return card


def artist_albums(artist_id: str, limit: int = 50) -> dict[str, Any]:
    """An artist's albums and singles, newest first, without duplicates.

    Spotify lists the same release once per market and often several times
    across reissues, so identical titles are collapsed and the earliest
    release date kept.
    """
    sp = client()
    artist = sp.artist(artist_id)
    first = sp.artist_albums(artist_id, album_type="album,single", limit=50)

    seen: dict[str, dict[str, Any]] = {}
    for album in _paginate(first, sp):
        if not album or not album.get("id"):
            continue
        key = album["name"].strip().lower()
        card = album_card(album)
        existing = seen.get(key)
        if existing is None or (card["year"] or "9999") < (existing["year"] or "9999"):
            seen[key] = card
        if len(seen) >= limit:
            break

    albums = sorted(seen.values(), key=lambda a: a["year"] or "0000", reverse=True)
    return {"artist": artist_card(artist), "albums": albums}


def reset_client() -> None:
    """Drop the cached client so new credentials take effect immediately."""
    global _client
    with _client_lock:
        _client = None
