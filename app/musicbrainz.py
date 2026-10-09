"""MusicBrainz's web service, for the tracklists of releases.

beets already asks MusicBrainz for candidates when matching; this is for the
plainer question the Library's *Missing tracks* asks: what is on this
release, and which other releases of the same album are there.

MusicBrainz asks for at most one request a second per client and a
User-Agent that says who is asking (https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting),
so every call waits its turn here. Answers are kept for the life of the
process: a release's tracklist does not change between two presses of a
button.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from . import memo

API = "https://musicbrainz.org/ws/2"
USER_AGENT = ("navidrome-companion/1.0 "
              "( https://github.com/awdimartino/navidrome-companion )")
TIMEOUT = 20
# A little over one a second, so a clock edge never makes it two.
INTERVAL = 1.1

_lock = threading.Lock()
_last = 0.0


class Unavailable(RuntimeError):
    """MusicBrainz could not be asked, or did not answer with data."""


def _get(path: str, **params: str) -> dict[str, Any]:
    global _last
    with _lock:
        wait = _last + INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            response = requests.get(f"{API}/{path}", timeout=TIMEOUT,
                                    params={**params, "fmt": "json"},
                                    headers={"User-Agent": USER_AGENT,
                                             "Accept": "application/json"})
        except requests.RequestException as exc:
            raise Unavailable(f"MusicBrainz: {exc}"[:200]) from exc
        finally:
            _last = time.monotonic()
    if response.status_code == 404:
        raise Unavailable("MusicBrainz does not know that release.")
    if response.status_code != 200:
        raise Unavailable(f"MusicBrainz answered {response.status_code}.")
    try:
        return response.json()
    except ValueError as exc:
        raise Unavailable("MusicBrainz answered with something other than JSON.") from exc


def _credit(credits: list[dict[str, Any]] | None) -> str:
    return "".join(f"{c.get('name') or (c.get('artist') or {}).get('name', '')}"
                   f"{c.get('joinphrase', '')}" for c in credits or [])


def release(release_id: str) -> dict[str, Any]:
    """One release: its names, and every track on every medium, in order."""
    return memo.cached(("mb-release", release_id), 0,
                       lambda: _release(release_id))


def _release(release_id: str) -> dict[str, Any]:
    data = _get(f"release/{release_id}",
                inc="recordings+artist-credits+release-groups+media")
    tracks = []
    for medium in data.get("media") or []:
        for track in medium.get("tracks") or []:
            recording = track.get("recording") or {}
            tracks.append({
                "disc": medium.get("position") or 1,
                "number": track.get("position") or 0,
                "title": track.get("title") or recording.get("title") or "",
                "artist": _credit(track.get("artist-credit")
                                  or recording.get("artist-credit")),
                "length_ms": track.get("length") or recording.get("length"),
                "recording_id": recording.get("id"),
            })
    return {
        "id": data.get("id"),
        "title": data.get("title") or "",
        "artist": _credit(data.get("artist-credit")),
        "date": data.get("date") or "",
        "country": data.get("country") or "",
        "disambiguation": data.get("disambiguation") or "",
        "formats": sorted({m.get("format") for m in data.get("media") or []
                           if m.get("format")}),
        "release_group": (data.get("release-group") or {}).get("id"),
        "tracks": tracks,
    }


def editions(release_group_id: str) -> list[dict[str, Any]]:
    """Every release of one album, with how many tracks each has - enough to
    choose between them, without a request per release."""
    return memo.cached(("mb-editions", release_group_id), 0,
                       lambda: _editions(release_group_id))


def _editions(release_group_id: str) -> list[dict[str, Any]]:
    data = _get("release", **{"release-group": release_group_id,
                              "inc": "media", "limit": "100"})
    found = []
    for one in data.get("releases") or []:
        media = one.get("media") or []
        found.append({
            "id": one.get("id"),
            "title": one.get("title") or "",
            "date": one.get("date") or "",
            "country": one.get("country") or "",
            "disambiguation": one.get("disambiguation") or "",
            "formats": sorted({m.get("format") for m in media if m.get("format")}),
            "track_count": sum(m.get("track-count") or 0 for m in media),
        })
    return found
