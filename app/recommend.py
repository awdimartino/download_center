"""What to download next: the shelves the Download tab shows before a search.

Five shelves, each built from something this application already knows:

- **New from your artists** - releases from the last ninety days by the
  artists you play most.
- **Missing from your artists** - albums by those artists that the library
  does not hold.
- **Artists like yours** - Last.fm's similar artists for your favourites,
  minus anyone already in the library, ranked by how many favourites point
  at them.
- **Because you played** - Last.fm's similar tracks for what has been on
  repeat this month.
- **More of a genre** - Last.fm's top albums for the genres you play most.

"The artists you play most" is the last year of plays, with stars and high
ratings added on top, so somebody whose history is short still has
favourites.

**Why Last.fm and not Spotify for "similar".** Spotify withdrew its
recommendations, related-artists and top-tracks endpoints from new
applications in late 2024. Spotify is still where every card comes from -
its id is what a download is queued with - but the judgement of what is
alike is Last.fm's.

**Why it is computed once a day, in the background.** One pass is a hundred
or so requests to two services, which is most of a minute from a Pi on a
slow link. It is stored per person in state.db, so a restart or a deploy
does not throw it away, and recomputed when it is a day old: the stale
copy is served meanwhile, and nobody waits on it.

**What is filtered when it is read, not when it is computed.** What the
library holds and what somebody said they are not interested in both change
between passes, and a card for an album downloaded an hour ago, or for an
artist dismissed a second ago, is the thing this panel must not show.
"""

from __future__ import annotations

import collections
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from typing import Any

from . import lastfm, navidrome, playcounts, registry, spotify, store, threads
from .config import settings

log = logging.getLogger("navidrome_companion.recommend")

# How old a pass may be before the next visit starts another.
MAX_AGE = timedelta(hours=24)

# How far back "the artists you play most" looks. All-time would be
# whoever somebody liked in 2019, the same choice Home's artist list makes.
FAVOURITE_DAYS = 365
# Artists whose discographies are read, for new releases and missing
# albums. Each is three or four Spotify requests.
DISCOGRAPHY_ARTISTS = 15
# Of those, the ones whose missing albums are listed. Past ten the shelf
# fills with somebody you played twice.
MISSING_ARTISTS = 10
MISSING_PER_ARTIST = 3
NEW_DAYS = 90
# Favourites asked for similar artists, and how many similar artists are
# looked up on Spotify.
SIMILAR_SEEDS = 8
SIMILAR_KEPT = 18
# Songs on repeat this month, and similar songs kept for each.
BECAUSE_SEEDS = 3
BECAUSE_DAYS = 30
BECAUSE_KEPT = 6
GENRES = 3
GENRE_KEPT = 10

# Each shelf is stored longer than it is shown, so a few dismissals or
# downloads leave something to show rather than an empty row.
SHOWN = {"new": 16, "missing": 16, "similar": 12, "because": 5, "genre": 8}

# Stars and ratings, as plays. A starred artist counts as forty plays in the
# year: enough to make a favourite of somebody whose history is short, not
# enough to outrank what is actually being listened to.
STAR_WEIGHT = {"artist": 40, "album": 20, "media_file": 5}
RATED_WEIGHT = 3

# Credits that are not an artist anybody can be like.
NOT_ARTISTS = {"various artists", "various", "unknown artist", "soundtrack", ""}

# Between Last.fm calls, which asks for about five a second at most; and
# between Spotify lookups, which share a rate limit with every search.
LASTFM_PAUSE = lastfm.PAUSE
SPOTIFY_PAUSE = 0.1

SCHEMA = """
-- One pass per person, as the JSON the panel is drawn from. Kept so a
-- restart does not cost the next visit a minute of requests.
CREATE TABLE IF NOT EXISTS recommendation_cache (
    user_id      TEXT PRIMARY KEY,
    library_id   INTEGER,
    computed_at  TEXT NOT NULL,
    payload      TEXT NOT NULL
);

-- "Not interested". `kind` is artist, album or track, and `item_key` the
-- same normalised key the library is compared on, so a reissue of a
-- dismissed album under another Spotify id stays dismissed. An artist
-- hides everything by them.
CREATE TABLE IF NOT EXISTS recommendation_dismissed (
    user_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    item_key    TEXT NOT NULL,
    label       TEXT,
    decided_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, kind, item_key)
);
"""

KINDS = ("artist", "album", "track")

_schema_on: Any = None


def _db() -> Any:
    """The state.db handle, with this module's tables made on first use."""
    global _schema_on
    conn = store.connection()
    if _schema_on is not conn:
        conn.executescript(SCHEMA)
        _schema_on = conn
    return conn


# --- names, compared ---------------------------------------------------------

# What a reissue adds to a title. "OK Computer (Collector's Edition)" and
# "Abbey Road - Remastered 2009" are the record you already have, as far as
# a suggestion is concerned - which is the opposite of the choice the album
# registry makes, and deliberately so: the registry decides where files go,
# this only decides whether to mention something.
_TRAILING_BRACKETS = re.compile(r"\s*[\(\[][^\(\)\[\]]*[\)\]]\s*$")
_EDITION_DASH = re.compile(
    r"\s+[-–]\s+[^-–]*\b(?:remaster(?:ed)?|deluxe|edition|version|anniversary|"
    r"expanded|mono|stereo|bonus)\b[^-–]*$", re.I)


def base_title(title: str) -> str:
    """A title with its edition taken off, normalised."""
    text = title or ""
    while True:
        stripped = _EDITION_DASH.sub("", _TRAILING_BRACKETS.sub("", text))
        if stripped == text or not stripped.strip():
            break
        text = stripped
    return registry.normalize(text)


def _artist_key(name: str) -> str:
    return registry.normalize(name or "")


def album_dismiss_key(card: dict[str, Any]) -> str:
    return registry.album_key(card.get("primary_artist") or card.get("artist") or "",
                              card.get("name") or "")


def track_dismiss_key(card: dict[str, Any]) -> str:
    return registry.recording_key(card.get("primary_artist") or card.get("artist") or "",
                                  card.get("name") or "")


def _credits(card: dict[str, Any]) -> set[str]:
    return {_artist_key(c) for c in (card.get("artist"), card.get("primary_artist")) if c}


# --- what the library holds, by name -----------------------------------------

def library_names(library_id: int) -> dict[str, set]:
    """The library's artists, and its albums by artist and base title.

    `held_in` and `albums_held_in` answer "this exact song" and "this exact
    album". A suggestion needs looser questions: is this artist here at all,
    and is any edition of this album here.
    """
    try:
        connection = navidrome.open_db()
    except navidrome.Unavailable as exc:
        log.warning("cannot read what library %s holds: %s", library_id, exc)
        return {"artists": set(), "albums": set()}
    with connection:
        rows = connection.execute(
            f"select coalesce(mf.artist, ''), coalesce(mf.album_artist, ''),"
            f"       coalesce(mf.album, '')"
            f"  from media_file mf"
            f" where {navidrome.live_clause(connection, [library_id])}").fetchall()
    artists: set[str] = set()
    albums: set[tuple[str, str]] = set()
    for artist, album_artist, album in rows:
        for credit in {artist, album_artist}:
            if not credit:
                continue
            key = _artist_key(credit)
            artists.add(key)
            if album:
                albums.add((key, base_title(album)))
    return {"artists": artists, "albums": albums}


def _album_owned(card: dict[str, Any], names: dict[str, set]) -> bool:
    title = base_title(card.get("name") or "")
    return any((credit, title) in names["albums"] for credit in _credits(card))


# --- who somebody likes ------------------------------------------------------

def _since(days: int) -> str:
    today = datetime.strptime(playcounts.today(), "%Y-%m-%d")
    return (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")


def _annotated_artists(user_id: str, library_id: int,
                       ) -> tuple[collections.Counter, dict[str, str]]:
    """Stars and high ratings, as plays, per artist key, and each key's name.

    Navidrome's own tables, probed rather than assumed: this reads a
    database another application owns and upgrades on its own schedule.
    """
    scores: collections.Counter = collections.Counter()
    names: dict[str, str] = {}
    try:
        connection = navidrome.open_db()
    except navidrome.Unavailable:
        return scores, names
    with connection:
        tables = {row[0] for row in connection.execute(
            "select name from sqlite_master where type = 'table'")}
        if "annotation" not in tables:
            return scores, names
        rated = "rating" in navidrome.columns_of(connection, "annotation")
        wanted = "(an.starred = 1 or an.rating >= 4)" if rated else "an.starred = 1"
        rating = "an.rating" if rated else "0"
        live = navidrome.live_clause(connection, [library_id])
        queries = [("media_file", f"""
            select coalesce(nullif(mf.artist, ''), mf.album_artist), an.starred, {rating}
              from annotation an join media_file mf on mf.id = an.item_id
             where an.user_id = ? and an.item_type = 'media_file' and {wanted}
               and {live}""")]
        if "album" in tables and "album_artist" in navidrome.columns_of(connection, "album"):
            queries.append(("album", f"""
                select al.album_artist, an.starred, {rating}
                  from annotation an join album al on al.id = an.item_id
                 where an.user_id = ? and an.item_type = 'album' and {wanted}"""))
        if "artist" in tables:
            queries.append(("artist", f"""
                select ar.name, an.starred, {rating}
                  from annotation an join artist ar on ar.id = an.item_id
                 where an.user_id = ? and an.item_type = 'artist' and {wanted}"""))
        for kind, sql in queries:
            try:
                rows = connection.execute(sql, (user_id,)).fetchall()
            except Exception as exc:
                log.warning("cannot read %s stars: %s", kind, exc)
                continue
            for name, starred, stars in rows:
                key = _artist_key(name)
                if key in NOT_ARTISTS:
                    continue
                names.setdefault(key, name)
                scores[key] += (STAR_WEIGHT[kind] if starred else 0) \
                    + (RATED_WEIGHT if (stars or 0) >= 4 else 0)
    return scores, names


def favourite_artists(user_id: str, library_id: int) -> list[tuple[str, int]]:
    """(name, score) for the artists somebody plays most, best first.

    A year of plays per artist, plus stars and high ratings. The track's own
    artist rather than its album artist: this application writes the primary
    artist there, and "Michael Jackson, Paul McCartney" is nobody to look up.
    """
    since = _since(FAVOURITE_DAYS)
    played: collections.Counter = collections.Counter()
    for when, track_uuid, n in playcounts.increments(user_id):
        if when[:10] >= since:
            played[track_uuid] += n
    _version, tracks = playcounts.track_index()

    scores: collections.Counter = collections.Counter()
    names: dict[str, str] = {}
    for track_uuid, n in played.items():
        known = tracks.get(track_uuid)
        if not known:
            continue
        key = _artist_key(known["artist"])
        if key in NOT_ARTISTS:
            continue
        names.setdefault(key, known["artist"])
        scores[key] += n

    starred, starred_names = _annotated_artists(user_id, library_id)
    for key, n in starred.items():
        scores[key] += n
        names.setdefault(key, starred_names[key])
    return [(names[key], n) for key, n in scores.most_common() if n > 0]


def repeat_tracks(user_id: str) -> list[dict[str, Any]]:
    """What has been on repeat lately, one song per artist."""
    end = playcounts.today()
    seen: set[str] = set()
    picked = []
    for days in (BECAUSE_DAYS, BECAUSE_DAYS * 3):
        for row in playcounts.top_tracks(_since(days), end, user_id, 40):
            key = _artist_key(row["artist"])
            if not row["known"] or key in seen or key in NOT_ARTISTS:
                continue
            seen.add(key)
            picked.append({"title": row["title"], "artist": row["artist"]})
            if len(picked) >= BECAUSE_SEEDS:
                return picked
    return picked


def favourite_genres(user_id: str) -> list[str]:
    rows = playcounts.top_genres(_since(FAVOURITE_DAYS), playcounts.today(),
                                 user_id, GENRES * 2)
    genres = []
    for row in rows:
        # Navidrome's first genre tag; a few files carry "Rock; Pop" in one.
        name = re.split(r"[;/,]", row["genre"])[0].strip()
        if name and name.lower() not in {g.lower() for g in genres}:
            genres.append(name)
    return genres[:GENRES]


# --- asking the two services -------------------------------------------------

def _listed(value: Any) -> list[dict[str, Any]]:
    """Last.fm sends one result as an object and several as a list."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return [value] if isinstance(value, dict) else []


def _lastfm(method: str, **params: str) -> dict[str, Any]:
    time.sleep(LASTFM_PAUSE)
    return lastfm._call(method, api_key=settings.lastfm_api_key, **params)


def _quoted(text: str) -> str:
    # Spotify's field filters take a quoted phrase; a quote inside it ends
    # the phrase early.
    return '"' + (text or "").replace('"', " ").strip() + '"'


def _spotify_search(query: str, kind: str) -> list[dict[str, Any]]:
    time.sleep(SPOTIFY_PAUSE)
    return [item for item in spotify.search(query, kind, 5) if item]


def find_artist(name: str) -> dict[str, Any] | None:
    """The Spotify artist with exactly this name, or None.

    Exactly, after normalising: the first result for an obscure name is
    often somebody more famous with a similar one, and a card for the wrong
    artist is worse than no card.
    """
    want = _artist_key(name)
    for item in _spotify_search(f"artist:{_quoted(name)}", "artist"):
        if _artist_key(item.get("name") or "") == want:
            return spotify.artist_card(item)
    return None


def _same_credit(item: dict[str, Any], artist: str) -> bool:
    want = _artist_key(artist)
    return any(_artist_key(a.get("name") or "") == want
               for a in item.get("artists") or [])


def find_album(name: str, artist: str) -> dict[str, Any] | None:
    want = base_title(name)
    for item in _spotify_search(
            f"album:{_quoted(name)} artist:{_quoted(artist)}", "album"):
        if _same_credit(item, artist) and base_title(item.get("name") or "") == want:
            return spotify.album_card(item)
    return None


def find_track(name: str, artist: str) -> dict[str, Any] | None:
    want = base_title(name)
    items = _spotify_search(f"track:{_quoted(name)} artist:{_quoted(artist)}", "track")
    # The plain title first; a live take or a remix by the same artist if
    # that is all there is.
    for exact in (True, False):
        for item in items:
            if _same_credit(item, artist) and (
                    not exact or base_title(item.get("name") or "") == want):
                return spotify.track_card(item)
    return None


# --- the shelves -------------------------------------------------------------

def _discographies(favourites: list[tuple[str, int]], problems: list[str],
                   ) -> list[tuple[str, list[dict[str, Any]]]]:
    """(artist, releases) for the top favourites that Spotify knows."""
    found = []
    failed = 0
    for name, _score in favourites[:DISCOGRAPHY_ARTISTS]:
        try:
            artist = find_artist(name)
            if artist is None:
                continue
            time.sleep(SPOTIFY_PAUSE)
            found.append((name, spotify.artist_albums(artist["id"])["albums"]))
        except Exception as exc:
            failed += 1
            log.warning("cannot read %s's releases: %s", name, exc)
    if failed and not found:
        problems.append("Spotify could not be asked for your artists' releases.")
    return found


def new_releases(discographies: list[tuple[str, list[dict[str, Any]]]],
                 names: dict[str, set]) -> list[dict[str, Any]]:
    cutoff = (datetime.now(UTC) - timedelta(days=NEW_DAYS)).strftime("%Y-%m-%d")
    fresh = [album for _name, albums in discographies for album in albums
             # A bare year sorts before any full date in that year, so a
             # release Spotify only dates to the year is never "new".
             if (album.get("released") or "") >= cutoff
             and album.get("type") != "compilation"
             and not _album_owned(album, names)]
    fresh.sort(key=lambda a: a.get("released") or "", reverse=True)
    return _unique(fresh)[:SHOWN["new"] + 8]


def missing_albums(discographies: list[tuple[str, list[dict[str, Any]]]],
                   names: dict[str, set]) -> list[dict[str, Any]]:
    """Albums by the favourites that the library has no edition of.

    Albums only, not singles: a single is usually on an album already, and
    a shelf of them is a shelf of things already owned another way. Taken
    in turn from each artist, so the first artist's ten records do not fill
    the shelf.
    """
    per_artist = []
    for _name, albums in discographies[:MISSING_ARTISTS]:
        wanted = [a for a in albums
                  if a.get("type") == "album" and not _album_owned(a, names)]
        per_artist.append(wanted[:MISSING_PER_ARTIST])
    shelf = []
    for round_ in range(MISSING_PER_ARTIST):
        shelf.extend(albums[round_] for albums in per_artist if len(albums) > round_)
    return _unique(shelf)


def similar_artists(favourites: list[tuple[str, int]], names: dict[str, set],
                    problems: list[str]) -> list[dict[str, Any]]:
    """Artists Last.fm calls like several favourites, not in the library.

    Ranked by how many favourites point at them, then by how alike Last.fm
    says they are. One favourite's long list of near-namesakes is less of a
    signal than four favourites agreeing.
    """
    seeds = favourites[:SIMILAR_SEEDS]
    seed_keys = {_artist_key(name) for name, _ in favourites}
    score: collections.Counter = collections.Counter()
    because: dict[str, list[str]] = collections.defaultdict(list)
    shown: dict[str, str] = {}
    failed = 0
    for seed, _ in seeds:
        try:
            body = _lastfm("artist.getSimilar", artist=seed, limit="30", autocorrect="1")
        except lastfm.LastfmError as exc:
            failed += 1
            log.warning("last.fm similar artists for %s: %s", seed, exc)
            continue
        for item in _listed((body.get("similarartists") or {}).get("artist")):
            name = item.get("name") or ""
            key = _artist_key(name)
            if not key or key in seed_keys or key in names["artists"] or key in NOT_ARTISTS:
                continue
            shown.setdefault(key, name)
            score[key] += float(item.get("match") or 0)
            because[key].append(seed)
    if failed and failed == len(seeds):
        problems.append("Last.fm could not be asked for similar artists.")

    ranked = sorted(score, key=lambda k: (len(because[k]), score[k]), reverse=True)
    cards = []
    for key in ranked:
        if len(cards) >= SIMILAR_KEPT:
            break
        try:
            card = find_artist(shown[key])
        except Exception as exc:
            log.warning("cannot find %s on Spotify: %s", shown[key], exc)
            continue
        if card:
            card["because"] = because[key][:3]
            cards.append(card)
    return cards


def because_you_played(seeds: list[dict[str, Any]], held: set[str],
                       problems: list[str]) -> list[dict[str, Any]]:
    """For each song on repeat, songs like it by somebody else."""
    shelves = []
    failed = 0
    for seed in seeds:
        try:
            body = _lastfm("track.getSimilar", artist=seed["artist"], track=seed["title"],
                           limit="40", autocorrect="1")
        except lastfm.LastfmError as exc:
            failed += 1
            log.warning("last.fm similar tracks for %s: %s", seed["title"], exc)
            continue
        seed_key = _artist_key(seed["artist"])
        per_artist: collections.Counter = collections.Counter()
        tracks = []
        for item in _listed((body.get("similartracks") or {}).get("track")):
            if len(tracks) >= BECAUSE_KEPT:
                break
            title = item.get("name") or ""
            artist = (item.get("artist") or {}).get("name") or ""
            key = _artist_key(artist)
            # The seed's own artist is already in the library, and two by
            # anyone is enough.
            if (not title or key == seed_key or per_artist[key] >= 2
                    or registry.recording_key(artist, title) in held):
                continue
            try:
                card = find_track(title, artist)
            except Exception as exc:
                log.warning("cannot find %s on Spotify: %s", title, exc)
                continue
            if card:
                per_artist[key] += 1
                tracks.append(card)
        if tracks:
            shelves.append({"seed": seed, "tracks": tracks})
    if failed and failed == len(seeds):
        problems.append("Last.fm could not be asked for similar songs.")
    return shelves


def genre_picks(genres: list[str], names: dict[str, set],
                problems: list[str]) -> list[dict[str, Any]]:
    """For each genre played most, its best-known albums not in the library."""
    shelves = []
    failed = 0
    for genre in genres:
        try:
            body = _lastfm("tag.getTopAlbums", tag=genre, limit="50")
        except lastfm.LastfmError as exc:
            failed += 1
            log.warning("last.fm top albums for %s: %s", genre, exc)
            continue
        seen_artists: set[str] = set()
        albums = []
        for item in _listed((body.get("albums") or {}).get("album")):
            if len(albums) >= GENRE_KEPT:
                break
            name = item.get("name") or ""
            artist = (item.get("artist") or {}).get("name") or ""
            key = _artist_key(artist)
            # One album an artist, or a genre's shelf is one band's catalogue.
            if not name or key in seen_artists or key in NOT_ARTISTS:
                continue
            if (key, base_title(name)) in names["albums"]:
                continue
            try:
                card = find_album(name, artist)
            except Exception as exc:
                log.warning("cannot find %s on Spotify: %s", name, exc)
                continue
            if card and not _album_owned(card, names):
                seen_artists.add(key)
                albums.append(card)
        if albums:
            shelves.append({"genre": genre, "albums": albums})
    if failed and failed == len(genres):
        problems.append("Last.fm could not be asked for genre picks.")
    return shelves


def _unique(cards: list[dict[str, Any]],
            already: list[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """One card per record. Two editions of an album are one suggestion -
    "Life 1" and "Life 1 (where did the time go)" were side by side - and
    a record already on an earlier shelf is not repeated on a later one."""
    def keys(card: dict[str, Any]) -> set:
        return {card.get("id"), (card.get("primary_artist") or card.get("artist") or "",
                                 base_title(card.get("name") or ""))}

    seen: set = set()
    for card in already:
        seen |= keys(card)
    out = []
    for card in cards:
        if card.get("id") and not keys(card) & seen:
            seen |= keys(card)
            out.append(card)
    return out


def compute(user_id: str, library_id: int) -> dict[str, Any]:
    """One full pass. Each shelf stands alone: one service failing leaves
    the others, with a line saying what is missing and why."""
    problems: list[str] = []
    names = library_names(library_id)
    favourites = favourite_artists(user_id, library_id)

    discographies = _discographies(favourites, problems)
    new = new_releases(discographies, names)
    shelves: dict[str, Any] = {
        "new": new,
        "missing": _unique(missing_albums(discographies, names), new),
        "similar": [], "because": [], "genres": [],
    }

    have_lastfm = bool(settings.lastfm_api_key)
    if have_lastfm:
        shelves["similar"] = similar_artists(favourites, names, problems)
        held = navidrome.held_in(library_id)
        shelves["because"] = because_you_played(repeat_tracks(user_id), held, problems)
        shelves["genres"] = genre_picks(favourite_genres(user_id), names, problems)

    return {
        "computed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "library_id": library_id,
        "favourites": [name for name, _ in favourites[:5]],
        "lastfm": have_lastfm,
        "problems": problems,
        "shelves": shelves,
    }


# --- stored, and refreshed in the background ---------------------------------

_lock = threading.Lock()
_running: dict[str, Future] = {}
# Why the last pass for somebody failed, until one succeeds.
_failed: dict[str, str] = {}


def _stored(user_id: str) -> dict[str, Any] | None:
    row = _db().execute(
        "SELECT payload FROM recommendation_cache WHERE user_id = ?",
        (user_id,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def _save(user_id: str, payload: dict[str, Any]) -> None:
    _db()
    with store.transaction() as tx:
        tx.execute(
            "INSERT OR REPLACE INTO recommendation_cache"
            " (user_id, library_id, computed_at, payload) VALUES (?, ?, ?, ?)",
            (user_id, payload["library_id"], payload["computed_at"],
             json.dumps(payload)))


def _run(user_id: str, library_id: int,
         work: Callable[[str, int], dict[str, Any]]) -> None:
    try:
        payload = work(user_id, library_id)
        _save(user_id, payload)
        _failed.pop(user_id, None)
    except Exception as exc:
        log.exception("recommendations for %s failed", user_id)
        _failed[user_id] = str(exc) or type(exc).__name__
    finally:
        with _lock:
            _running.pop(user_id, None)


def start(user_id: str, library_id: int,
          work: Callable[[str, int], dict[str, Any]] | None = None) -> bool:
    """Begin a pass for somebody, unless one is already under way."""
    with _lock:
        if user_id in _running:
            return False
        _running[user_id] = threads.submit(_run, user_id, library_id, work or compute)
    return True


def wait(user_id: str, timeout: float | None = None) -> None:
    """Until somebody's pass, if one is running, has finished."""
    with _lock:
        future = _running.get(user_id)
    if future is not None:
        future.result(timeout)


def is_running(user_id: str) -> bool:
    with _lock:
        return user_id in _running


def _stale(payload: dict[str, Any], library_id: int) -> bool:
    if payload.get("library_id") != library_id:
        return True
    try:
        made = datetime.fromisoformat(payload["computed_at"])
    except (KeyError, ValueError):
        return True
    return datetime.now(UTC) - made > MAX_AGE


# --- "not interested" --------------------------------------------------------

def dismiss(user_id: str, kind: str, key: str, label: str = "") -> None:
    if kind not in KINDS:
        raise ValueError(f"cannot dismiss a {kind!r}")
    _db()
    with store.transaction() as tx:
        tx.execute(
            "INSERT OR REPLACE INTO recommendation_dismissed"
            " (user_id, kind, item_key, label, decided_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, kind, key, label,
             datetime.now(UTC).isoformat(timespec="seconds")))


def undismiss(user_id: str, kind: str, key: str) -> None:
    _db()
    with store.transaction() as tx:
        tx.execute(
            "DELETE FROM recommendation_dismissed"
            " WHERE user_id = ? AND kind = ? AND item_key = ?",
            (user_id, kind, key))


def dismissed(user_id: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {kind: set() for kind in KINDS}
    for kind, key in _db().execute(
            "SELECT kind, item_key FROM recommendation_dismissed WHERE user_id = ?",
            (user_id,)):
        found.setdefault(kind, set()).add(key)
    return found


# --- what the panel is sent --------------------------------------------------

def _keep(card: dict[str, Any], kind: str, hidden: dict[str, set[str]]) -> bool:
    if _credits(card) & hidden["artist"]:
        return False
    if kind == "album":
        return album_dismiss_key(card) not in hidden["album"]
    if kind == "track":
        return track_dismiss_key(card) not in hidden["track"]
    return True


def _with_keys(card: dict[str, Any], kind: str) -> dict[str, Any]:
    """A copy of a card carrying the key its "Not interested" sends back."""
    out = dict(card)
    if kind == "album":
        out["dismiss_key"] = album_dismiss_key(card)
    elif kind == "track":
        out["dismiss_key"] = track_dismiss_key(card)
    else:
        out["dismiss_key"] = _artist_key(card.get("name") or "")
    # Every card can also hide its artist, which is what is wanted more
    # often than hiding one record.
    out["artist_key"] = _artist_key(card.get("primary_artist") or card.get("artist")
                                    or card.get("name") or "")
    return out


def present(payload: dict[str, Any], user_id: str,
            names: dict[str, set]) -> dict[str, Any]:
    """A stored pass, filtered for what has changed since it was made.

    Dismissals and any edition the library has gained come off here. What
    the library holds track by track is left to the route, which marks it
    the same way search results are marked.
    """
    hidden = dismissed(user_id)
    shelves = payload.get("shelves") or {}

    def albums(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [_with_keys(c, "album") for c in cards
                if _keep(c, "album", hidden) and not _album_owned(c, names)]

    similar = [_with_keys(c, "artist") for c in shelves.get("similar", [])
               if _artist_key(c.get("name") or "") not in hidden["artist"]
               and _artist_key(c.get("name") or "") not in names["artists"]]
    because = []
    for shelf in shelves.get("because", []):
        tracks = [_with_keys(t, "track") for t in shelf["tracks"]
                  if _keep(t, "track", hidden)]
        if tracks:
            because.append({"seed": shelf["seed"], "tracks": tracks})
    genres = []
    for shelf in shelves.get("genres", []):
        kept = albums(shelf["albums"])
        if kept:
            genres.append({"genre": shelf["genre"], "albums": kept})

    return {
        "computed_at": payload.get("computed_at"),
        "favourites": payload.get("favourites", []),
        "lastfm": payload.get("lastfm", False),
        "problems": payload.get("problems", []),
        "shelves": {"new": albums(shelves.get("new", [])),
                    "missing": albums(shelves.get("missing", [])),
                    "similar": similar, "because": because, "genres": genres},
    }


def view(user_id: str, library_id: int) -> dict[str, Any]:
    """What the panel shows now, starting a pass if this one is due.

    `status` is "ready", "computing" (nothing stored yet) or "failed"; with
    a stored pass that is being replaced, it is "ready" and `refreshing` is
    true.
    """
    payload = _stored(user_id)
    if payload is None or _stale(payload, library_id):
        # A failed pass is not retried on every visit; Refresh asks again.
        if user_id not in _failed or payload is not None:
            start(user_id, library_id)
    running = is_running(user_id)
    if payload is None:
        if running:
            return {"status": "computing"}
        return {"status": "failed",
                "reason": _failed.get(user_id, "Nothing has been worked out yet.")}
    shown = present(payload, user_id, library_names(library_id))
    return {"status": "ready", "refreshing": running, **shown}


def refresh(user_id: str, library_id: int) -> bool:
    _failed.pop(user_id, None)
    return start(user_id, library_id)
