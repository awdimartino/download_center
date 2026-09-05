"""Talking to Navidrome through its API, never through its database.

Navidrome owns its SQLite file and caches from it. A second writer risks lock
contention and inconsistent state that no amount of care on this side would
prevent, so anything that changes state goes through the front door. The
health panel reads the database directly, but read-only and for reporting.

Only one call is actually needed: telling Navidrome to look at the library
after beets has moved something into it. Without it new music waits for
whatever scan interval the server is set to.

Authentication is the Subsonic scheme Navidrome implements - a salted MD5 of
the password per request, so the password itself is not sent. MD5 is not
protecting anything here beyond a LAN, and the protocol offers nothing better.
"""

from __future__ import annotations

import hashlib
import logging
import secrets

import requests

from .config import settings

log = logging.getLogger("download_center.navidrome")

CLIENT = "download-center"
API_VERSION = "1.16.1"
TIMEOUT = 15


class NotConfigured(RuntimeError):
    """No server address or credentials, so there is nothing to call."""


def configured() -> bool:
    return bool(settings.navidrome_url and settings.navidrome_user
                and settings.navidrome_password)


def _params() -> dict[str, str]:
    salt = secrets.token_hex(8)
    token = hashlib.md5(
        (settings.navidrome_password + salt).encode("utf-8")).hexdigest()
    return {
        "u": settings.navidrome_user, "t": token, "s": salt,
        "v": API_VERSION, "c": CLIENT, "f": "json",
    }


def _call(endpoint: str, **extra: str) -> dict:
    if not configured():
        raise NotConfigured("navidrome_url, navidrome_user and "
                            "navidrome_password are not all set")
    url = settings.navidrome_url.rstrip("/") + f"/rest/{endpoint}"
    response = requests.get(url, params={**_params(), **extra}, timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json().get("subsonic-response", {})
    if payload.get("status") != "ok":
        error = payload.get("error", {})
        raise RuntimeError(
            f"{endpoint} failed: {error.get('message', 'unknown error')}")
    return payload


def trigger_scan(full: bool = False) -> dict:
    """Ask Navidrome to scan.

    A full scan re-reads every file rather than trusting mtimes, which matters
    after stamping: identity tags are written with the file's mtime restored,
    so an incremental scan would never notice them. Freshly imported files are
    new and get picked up either way, so the default stays cheap.
    """
    payload = _call("startScan.view", fullScan="true" if full else "false")
    return payload.get("scanStatus", {})


def scan_status() -> dict:
    return _call("getScanStatus.view").get("scanStatus", {})


def star(track_id: str) -> bool:
    """Star a track on the user's behalf, so an annotation can be moved.

    This is the reason deduplication can prefer the better file rather than
    the annotated one: the star follows the decision instead of constraining
    it. Failures are reported, never raised - losing a star is worth knowing
    about, but not worth aborting a resolution halfway through.
    """
    try:
        _call("star.view", id=track_id)
        return True
    except Exception as exc:
        log.warning("could not star %s: %s", track_id, exc)
        return False


def set_rating(track_id: str, rating: int) -> bool:
    try:
        _call("setRating.view", id=track_id, rating=str(int(rating)))
        return True
    except Exception as exc:
        log.warning("could not rate %s: %s", track_id, exc)
        return False


def notify(full: bool = False) -> bool:
    """Trigger a scan, treating every failure as unimportant.

    Nothing here is worth failing an import over - the music is already on
    disk and correctly filed, and Navidrome will find it on its own schedule
    regardless. This only makes it faster.
    """
    if not configured():
        return False
    try:
        trigger_scan(full=full)
        log.info("asked Navidrome to scan%s", " (full)" if full else "")
        return True
    except Exception as exc:
        log.warning("could not trigger a Navidrome scan: %s", exc)
        return False
