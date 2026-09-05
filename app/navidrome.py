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

log = logging.getLogger("download_center.navidrome")

CLIENT = "download-center"
API_VERSION = "1.16.1"
TIMEOUT = 15


class NotConfigured(RuntimeError):
    """No server address or credentials, so there is nothing to call."""


class LoginFailed(RuntimeError):
    """Navidrome rejected those credentials."""


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


def open_db() -> sqlite3.Connection:
    """Navidrome's database, read-only."""
    path = settings.navidrome_db
    if not path.is_file():
        raise Unavailable(f"no database at {path}")
    try:
        # mode=ro still reads the write-ahead log, so the view is current
        # rather than a stale snapshot. immutable=1 would be faster and wrong.
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        # A mount pointing somewhere unexpected opens fine and fails on the
        # first real query, so check for a table we actually need.
        connection.execute("select 1 from media_file limit 1")
    except sqlite3.Error as exc:
        raise Unavailable(f"{exc}") from exc
    connection.row_factory = sqlite3.Row
    return connection


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

    with connection:
        tables = {r[0] for r in connection.execute(
            "select name from sqlite_master where type='table'")}
        if "user_library" in tables:
            rows = connection.execute(
                "select l.id, l.name, l.path from library l"
                " join user_library ul on ul.library_id = l.id"
                " where ul.user_id = ?", (identity.user_id,)).fetchall()
        else:
            rows = connection.execute(
                "select id, name, path from library").fetchall()
    return [{"id": r[0], "name": r[1], "path": r[2]} for r in rows]


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


def playlists(identity: Identity) -> list[dict[str, Any]]:
    response = requests.get(f"{base_url()}/api/playlist", timeout=TIMEOUT,
                            headers=identity.native_headers())
    response.raise_for_status()
    return response.json()


def save_playlist(identity: Identity, playlist: dict[str, Any],
                  playlist_id: str | None = None) -> dict[str, Any]:
    """Create or update a playlist, owned by that person.

    Smart playlist rules evaluate against the owner's own stars and play
    counts, so who this is created as decides whether it matches anything
    at all.
    """
    url = f"{base_url()}/api/playlist"
    if playlist_id:
        response = requests.put(f"{url}/{playlist_id}", json=playlist,
                                headers=identity.native_headers(),
                                timeout=TIMEOUT)
    else:
        response = requests.post(url, json=playlist,
                                 headers=identity.native_headers(),
                                 timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def delete_playlist(identity: Identity, playlist_id: str) -> None:
    response = requests.delete(f"{base_url()}/api/playlist/{playlist_id}",
                               headers=identity.native_headers(),
                               timeout=TIMEOUT)
    response.raise_for_status()


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


def scan_status() -> dict:
    return _service_call("getScanStatus.view").get("scanStatus", {})


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
