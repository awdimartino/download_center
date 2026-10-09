"""Talking to Navidrome - as a specific person, or as the server itself.

Navidrome owns its SQLite file and caches from it. A second writer risks lock
contention and inconsistent state that no amount of care on this side would
prevent, so anything that changes state goes through the front door. The
database is read, but read-only and for reporting.

Almost everything worth doing here belongs to a *user*. Stars, ratings,
playlists and play counts are per account, and an API call acts as whoever it
authenticated as - so starring a track "for the library" is not a thing that
exists. Calls therefore take the session of the person they are being made
for, and only scanning, which no user owns, falls back to the service
credentials in the configuration.

Navidrome's own login hands back everything needed for both of its APIs: a
bearer token for the native one, and a Subsonic salt and token so its older
API can be used on that person's behalf without ever holding their password.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import settings

log = logging.getLogger("navidrome_companion.navidrome")

CLIENT = "navidrome-companion"
API_VERSION = "1.16.1"
TIMEOUT = 15


class NotConfigured(RuntimeError):
    """No server address or credentials, so there is nothing to call."""


class LoginFailed(RuntimeError):
    """Navidrome rejected those credentials."""


class SessionExpired(RuntimeError):
    """Navidrome no longer accepts this person's token; they must sign in."""


class Unavailable(RuntimeError):
    """Navidrome's database could not be opened - unreportable, not fatal."""


@dataclass
class Identity:
    """Who an API call is being made for.

    Subsonic credentials come from Navidrome's login response rather than
    from a stored password, so a session can act for someone without this
    application ever knowing what they typed.
    """

    user_id: str
    username: str
    is_admin: bool
    token: str                      # native API bearer token
    subsonic_token: str
    subsonic_salt: str
    libraries: list[dict[str, Any]] = field(default_factory=list)

    def subsonic_params(self) -> dict[str, str]:
        return {"u": self.username, "t": self.subsonic_token,
                "s": self.subsonic_salt, "v": API_VERSION,
                "c": CLIENT, "f": "json"}

    def native_headers(self) -> dict[str, str]:
        return {"x-nd-authorization": f"Bearer {self.token}"}


def base_url() -> str:
    if not settings.navidrome_url:
        raise NotConfigured("navidrome_url is not set")
    return settings.navidrome_url.rstrip("/")


# --- authentication -------------------------------------------------------

def login(username: str, password: str) -> Identity:
    """Exchange a password for a session, using Navidrome as the authority.

    No user accounts are kept here. Navidrome already knows who exists, what
    they may see and what they have starred, and duplicating any of that
    would only create a second answer that can disagree with the first.
    """
    response = requests.post(f"{base_url()}/auth/login", timeout=TIMEOUT,
                             json={"username": username, "password": password})
    if response.status_code in (401, 403):
        raise LoginFailed("Incorrect username or password.")
    response.raise_for_status()
    body = response.json()

    identity = Identity(
        user_id=body["id"], username=body["username"],
        is_admin=bool(body.get("isAdmin")), token=body["token"],
        subsonic_token=body.get("subsonicToken", ""),
        subsonic_salt=body.get("subsonicSalt", ""),
    )
    identity.libraries = libraries_for(identity)
    return identity


class _Closing(sqlite3.Connection):
    """A connection that `with` closes as well as commits.

    sqlite3's own context manager only ends the transaction, so every
    `with open_db() ...` left its connection - a file handle and a read
    lock's worth of WAL bookkeeping on Navidrome's database - for the
    garbage collector to find.
    """

    def __exit__(self, *exc: object) -> bool:
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def open_db() -> sqlite3.Connection:
    """Navidrome's database, read-only. Closed by the `with` that uses it."""
    path = settings.navidrome_db
    if not path.is_file():
        raise Unavailable(f"no database at {path}")
    try:
        # mode=ro still reads the write-ahead log, so the view is current
        # rather than a stale snapshot. immutable=1 would be faster and wrong.
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5,
                                     factory=_Closing)
    except sqlite3.Error as exc:
        raise Unavailable(f"{exc}") from exc
    try:
        # A mount pointing somewhere unexpected opens fine and fails on the
        # first real query, so check for a table we actually need.
        connection.execute("select 1 from media_file limit 1")
    except sqlite3.Error as exc:
        connection.close()
        raise Unavailable(f"{exc}") from exc
    connection.row_factory = sqlite3.Row
    return connection


def account(identity: Identity) -> tuple[bool, list[dict[str, Any]]] | None:
    """This person's admin flag and libraries as Navidrome has them now, or
    None if the account no longer exists. Raises Unavailable rather than
    answering "none" when the database cannot be read, so a blip is never
    mistaken for a revoked library."""
    connection = open_db()
    with connection:
        columns = columns_of(connection, "user")
        if "is_admin" in columns:
            row = connection.execute(
                'select is_admin from "user" where id = ?',
                (identity.user_id,)).fetchone()
            if row is None:
                return None
            is_admin = bool(row[0])
        else:
            is_admin = identity.is_admin
    current = Identity(**{**identity.__dict__, "is_admin": is_admin})
    return is_admin, _libraries(current, open_db())


def password_mark(identity: Identity) -> str | None:
    """A fingerprint of the password Navidrome holds for this account, to
    notice it change. Never the value itself: a hash of what is stored,
    which is already Navidrome's own encryption of it.

    None when the account is gone or this Navidrome keeps no such column -
    "cannot tell", which a caller must not read as "changed". Raises
    Unavailable when the database cannot be read.
    """
    connection = open_db()
    with connection:
        if "password" not in columns_of(connection, "user"):
            return None
        row = connection.execute('select password from "user" where id = ?',
                                 (identity.user_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    return hashlib.sha256(str(row[0]).encode()).hexdigest()


def library_stamp(connection: sqlite3.Connection,
                  user_id: str | None = None) -> tuple | None:
    """A cheap answer to "has the library changed?".

    The database files' modification time was the old answer, and Navidrome
    writes on every play, so everything keyed on it was rebuilt after each
    song. This changes only when tracks do - added, removed, rescanned, gone
    missing - and, given a user, when that person's plays or ratings do.

    None when there is no `updated_at` to go on: a track retagged in place
    keeps the same row count, so without it this cannot tell, and callers
    fall back to something that can.
    """
    columns = columns_of(connection, "media_file")
    if "updated_at" not in columns:
        return None
    parts: list[Any] = list(connection.execute(
        "select count(*), max(updated_at), {} from media_file".format(
            "sum(missing)" if "missing" in columns else "0")).fetchone())
    if "missing" in columns_of(connection, "folder"):
        parts.append(connection.execute(
            "select count(*) from folder where missing = 1").fetchone()[0])
    if user_id is not None:
        annotation = columns_of(connection, "annotation")
        plays = ("sum(play_count)" if "play_count" in annotation else "0")
        parts.extend(connection.execute(
            f"select count(*), {plays}, sum(starred), sum(rating)"
            " from annotation where user_id = ?", (user_id,)).fetchone())
    return tuple(parts)


def libraries_for(identity: Identity) -> list[dict[str, Any]]:
    """Which libraries this person may see, straight from Navidrome.

    Read rather than configured: Navidrome is already the authority on who
    can see what, and a second list here would eventually disagree with it.
    """
    try:
        connection = open_db()
    except Unavailable as exc:
        log.warning("cannot read libraries: %s", exc)
        return []
    return _libraries(identity, connection)


def _libraries(identity: Identity,
               connection: sqlite3.Connection) -> list[dict[str, Any]]:
    with connection:
        tables = {r[0] for r in connection.execute(
            "select name from sqlite_master where type='table'")}
        if "user_library" in tables:
            rows = connection.execute(
                "select l.id, l.name, l.path from library l"
                " join user_library ul on ul.library_id = l.id"
                " where ul.user_id = ?", (identity.user_id,)).fetchall()
        elif identity.is_admin:
            # Older Navidrome had no per-user assignment, so an administrator
            # legitimately sees everything.
            rows = connection.execute(
                "select id, name, path from library").fetchall()
        else:
            # Failing open here would hand somebody else's collection to an
            # ordinary account and point their downloads at it. Refusing is
            # visible; quietly over-sharing is not.
            log.warning("this Navidrome has no per-user library assignment, "
                        "so %s is given none", identity.username)
            rows = []
    return [{"id": r[0], "name": r[1], "path": r[2]} for r in rows]


def columns_of(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"pragma table_info({table})")}


# Where Navidrome keeps the two UUIDs it derives persistent ids from, parsed,
# in media_file.tags. Defined once: Health and the play-count reader each
# had their own copy of the same string.
UUID_TAG = "$.navidrome_uuid[0].value"
ALBUM_UUID_TAG = "$.navidrome_album_uuid[0].value"


def live_clause(connection: sqlite3.Connection,
                library_ids: list[int] | None = None) -> str:
    """SQL for the `media_file` rows Navidrome still believes are there.

    `missing` alone is not enough: when a whole directory goes, Navidrome
    marks the *folder* rather than every file under it, so a query that only
    checks the file over-counts by however many tracks were in it.

    Both columns are probed rather than assumed. This reads a database
    another application owns and upgrades on its own schedule, and a query
    naming a column that release does not have fails outright rather than
    degrading.

    Rows are aliased `mf`, and the caller passes the library ids it is
    entitled to - somebody else's collection is not theirs to count.
    """
    columns = columns_of(connection, "media_file")
    clause = "1=1" if "missing" not in columns else "mf.missing = 0"
    if ("missing" in columns and "folder_id" in columns
            and columns_of(connection, "folder")):
        clause += " and mf.folder_id in (select id from folder where missing = 0)"
    if library_ids is not None:
        inside = ",".join(str(int(i)) for i in library_ids) or "-1"
        clause += f" and mf.library_id in ({inside})"
    return clause


def held_in(library_id: int) -> set[str]:
    """Every recording a library already holds, as normalised artist+title.

    This is what the Browse tab's "you have this" badge is read from. It used
    to come from the download ledger, which answered a different and weaker
    question: the ledger says *this was fetched once*, and stays true after
    the file is deleted by hand, replaced, or moved to another library. The
    library itself says what is actually there.

    It is also broader in the way that matters. A record ripped from a CD or
    dropped into the inbox was never in the ledger, so the badge said nothing
    about it and the album came up unmarked in Browse - which is precisely
    when you are about to download a second copy.

    Keyed on the same normalisation the album registry uses, so "Don't" and
    "Dont" are one song here too.
    """
    return set(held_where(library_id))


def held_where(library_id: int) -> dict[str, set[str]]:
    """`held_in`, with the folders each recording is in: what Missing tracks
    needs to say which album already has a song, and to offer merging it."""
    from . import registry

    try:
        connection = open_db()
    except Unavailable as exc:
        log.warning("cannot read what library %s holds: %s", library_id, exc)
        return {}

    with connection:
        rows = connection.execute(
            f"select coalesce(mf.artist, ''), coalesce(mf.album_artist, ''),"
            f"       coalesce(mf.title, ''), mf.path"
            f"  from media_file mf"
            f" where {live_clause(connection, [library_id])}").fetchall()

    held: dict[str, set[str]] = {}
    for artist, album_artist, title, path in rows:
        if not title:
            continue
        folder = path.replace("\\", "/").rpartition("/")[0]
        # Both credits. This application writes the *primary* artist to the
        # file and keeps the full credit on the album artist, but a CD rip or
        # a hand-tagged file may have it either way round, and a badge that
        # only recognises our own downloads is most of the way to useless.
        held.setdefault(registry.recording_key(artist, title), set()).add(folder)
        if album_artist and album_artist != artist:
            held.setdefault(registry.recording_key(album_artist, title),
                            set()).add(folder)
    return held


def albums_held_in(library_id: int) -> dict[str, int]:
    """How many tracks a library holds of each album, by album key.

    Browse's covers say "In library" or "3 of 11 in library" from this, so a
    record half-fetched as singles shows as half there before anyone opens it.
    A count rather than a set because the half-there case is the one worth
    seeing: a whole album is easy to remember owning, three of its songs are
    not.

    Keyed under the album artist and under the track artist both, for the
    same reason `held_in` keeps both credits - a CD rip may carry either.
    """
    from . import registry

    try:
        connection = open_db()
    except Unavailable as exc:
        log.warning("cannot read what library %s holds: %s", library_id, exc)
        return {}

    with connection:
        rows = connection.execute(
            f"select coalesce(mf.artist, ''), coalesce(mf.album_artist, ''),"
            f"       coalesce(mf.album, '')"
            f"  from media_file mf"
            f" where {live_clause(connection, [library_id])}").fetchall()

    counts: dict[str, int] = {}
    for artist, album_artist, album in rows:
        if not album:
            continue
        keys = {registry.album_key(credit, album)
                for credit in (artist, album_artist) if credit}
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
    return counts


# --- calls made for a person ----------------------------------------------

def _subsonic(identity: Identity, endpoint: str, **extra: str) -> dict:
    url = f"{base_url()}/rest/{endpoint}"
    response = requests.get(url, timeout=TIMEOUT,
                            params={**identity.subsonic_params(), **extra})
    response.raise_for_status()
    payload = response.json().get("subsonic-response", {})
    if payload.get("status") != "ok":
        error = payload.get("error", {})
        raise RuntimeError(
            f"{endpoint} failed: {error.get('message', 'unknown error')}")
    return payload


def star(identity: Identity, track_id: str) -> bool:
    """Star a track as that person.

    This is what lets deduplication prefer the better file: the annotation
    follows the decision instead of constraining it. Failures are reported,
    never raised - worth knowing about, not worth aborting halfway through.
    """
    try:
        _subsonic(identity, "star.view", id=track_id)
        return True
    except Exception as exc:
        log.warning("could not star %s for %s: %s", track_id,
                    identity.username, exc)
        return False


def set_rating(identity: Identity, track_id: str, rating: int) -> bool:
    try:
        _subsonic(identity, "setRating.view", id=track_id,
                  rating=str(int(rating)))
        return True
    except Exception as exc:
        log.warning("could not rate %s for %s: %s", track_id,
                    identity.username, exc)
        return False


def _native(identity: Identity, method: str, path: str,
            **kwargs: Any) -> requests.Response:
    """One call to Navidrome's native API, as that person.

    Navidrome sends a refreshed token back on every authenticated response;
    the one from sign-in used to be kept for the session's whole fortnight
    and expired after Navidrome's session timeout (24 hours by default), so
    Playlists answered "Navidrome refused that" from day two while
    everything else worked. The fresh one is kept. A 401 means even that
    has lapsed - only signing in again mints another - and is raised as
    SessionExpired rather than as a refusal of whatever was asked.
    """
    response = requests.request(method, f"{base_url()}{path}",
                                headers=identity.native_headers(),
                                timeout=TIMEOUT, **kwargs)
    fresh = response.headers.get("x-nd-authorization", "")
    if fresh.lower().startswith("bearer "):
        identity.token = fresh[len("bearer "):].strip()
    if response.status_code == 401:
        raise SessionExpired(
            "Navidrome has ended this sign-in. Sign in again to carry on.")
    response.raise_for_status()
    return response


def playlists(identity: Identity) -> list[dict[str, Any]]:
    return _native(identity, "GET", "/api/playlist").json()


def save_playlist(identity: Identity, playlist: dict[str, Any],
                  playlist_id: str | None = None) -> dict[str, Any]:
    """Create or update a playlist, owned by that person.

    Smart playlist rules evaluate against the owner's own stars and play
    counts, so who this is created as decides whether it matches anything
    at all.
    """
    if playlist_id:
        response = _native(identity, "PUT", f"/api/playlist/{playlist_id}",
                           json=playlist)
    else:
        response = _native(identity, "POST", "/api/playlist", json=playlist)
    return response.json()


def delete_playlist(identity: Identity, playlist_id: str) -> None:
    _native(identity, "DELETE", f"/api/playlist/{playlist_id}")


# --- calls nobody owns ----------------------------------------------------

def service_configured() -> bool:
    return bool(settings.navidrome_url and settings.navidrome_user
                and settings.navidrome_password)


def _service_params() -> dict[str, str]:
    salt = secrets.token_hex(8)
    token = hashlib.md5(
        (settings.navidrome_password + salt).encode("utf-8")).hexdigest()
    return {"u": settings.navidrome_user, "t": token, "s": salt,
            "v": API_VERSION, "c": CLIENT, "f": "json"}


def _service_call(endpoint: str, **extra: str) -> dict:
    if not service_configured():
        raise NotConfigured("navidrome_url, navidrome_user and "
                            "navidrome_password are not all set")
    response = requests.get(f"{base_url()}/rest/{endpoint}", timeout=TIMEOUT,
                            params={**_service_params(), **extra})
    response.raise_for_status()
    payload = response.json().get("subsonic-response", {})
    if payload.get("status") != "ok":
        error = payload.get("error", {})
        raise RuntimeError(
            f"{endpoint} failed: {error.get('message', 'unknown error')}")
    return payload


def cover_art(item_id: str, size: int = 96) -> tuple[bytes, str]:
    """One track's cover, at the size asked for, as (bytes, content type).

    Proxied from Navidrome rather than read out of the file, for two
    reasons. It resizes - a library page shows fifty covers at a hundred
    pixels, and the embedded images behind them are often a megabyte each,
    which is the difference between a page and a download. And it already
    knows where the art is when a file has none embedded but the folder has
    a cover.jpg beside it.

    Service credentials, not the person's: this is called for a track the
    caller has already been shown, and the check that it is theirs happens
    before we get here.
    """
    if not service_configured():
        raise NotConfigured("navidrome_url, navidrome_user and "
                            "navidrome_password are not all set")
    response = requests.get(
        f"{base_url()}/rest/getCoverArt.view", timeout=TIMEOUT,
        params={**_service_params(), "id": item_id, "size": str(int(size))})
    response.raise_for_status()
    kind = response.headers.get("content-type", "")
    if not kind.startswith("image/"):
        # Subsonic reports a miss as a JSON error with a 200, so the content
        # type is the only honest signal that this is not a picture.
        raise RuntimeError("Navidrome has no cover for that track")
    return response.content, kind


def trigger_scan(full: bool = False) -> dict:
    """Ask Navidrome to scan.

    Scanning belongs to no user and Navidrome restricts it to admins, which
    is why the service credentials still exist. A full scan re-reads every
    file rather than trusting mtimes, which matters after stamping: identity
    tags are written with mtime restored, so an incremental scan would never
    notice them.
    """
    payload = _service_call("startScan.view",
                            fullScan="true" if full else "false")
    return payload.get("scanStatus", {})


def notify(full: bool = False) -> bool:
    """Trigger a scan, treating every failure as unimportant.

    Nothing here is worth failing an import over - the music is already on
    disk and correctly filed, and Navidrome finds it on its own schedule
    regardless. This only makes it sooner.
    """
    if not service_configured():
        return False
    try:
        trigger_scan(full=full)
        log.info("asked Navidrome to scan%s", " (full)" if full else "")
        return True
    except Exception as exc:
        log.warning("could not trigger a Navidrome scan: %s", exc)
        return False
