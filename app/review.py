"""What is in the library but has never been confirmed against MusicBrainz.

The staging list answered "what did beets refuse", which was the same
question as "what is missing from the library" - the music was not there, and
the list was the only place it existed. This list answers something narrower
and much less alarming: everything here is filed, playable and starred-able
right now; it simply has not been checked.

**Untagged means no MusicBrainz recording ID.** Nothing is stored. The state
is read off the file every time, so it cannot drift out of sync with reality:
a track leaves this list the moment it gains an ID, and no flag ever has to
be cleared. That is the failure the `import_refusal` table had - a note about
a path, written by a process that had since forgotten it.

**Per user, strictly private.** Scoped to the signed-in account's own
libraries through `identity.libraries`, which is Navidrome's own answer to
who may see what. An admin does not see another person's list.

Backed by Navidrome's database, which already knows every file and answers
instantly. It refreshes on scan, so this can be a few minutes stale - fine
for a page someone opens deliberately, and the rescan button covers the case
where it is not.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import navidrome

log = logging.getLogger("download_center.review")

# One page. The backlog is 1,113 tracks across a few hundred albums on day
# one, and sending all of it to a phone helps nobody.
PAGE = 50
MAX_PAGE = 200


@dataclass(frozen=True)
class Track:
    id: str
    path: str
    title: str
    artist: str
    track_no: int
    tagged: bool


@dataclass
class Entry:
    """One album folder holding at least one unconfirmed track.

    An album rather than a track, because the action is album-level: you
    confirm that a folder is a given release and all of its tracks move
    together under one album UUID. A per-track correction is what splits an
    album in two, so it is not the default.
    """

    library_id: int
    library: str
    artist: str
    album: str
    folder: str
    added: str = ""
    tracks: list[Track] = field(default_factory=list)

    @property
    def untagged(self) -> int:
        return sum(1 for t in self.tracks if not t.tagged)

    def as_dict(self) -> dict[str, Any]:
        return {
            "library_id": self.library_id, "library": self.library,
            "artist": self.artist, "album": self.album,
            "folder": self.folder, "added": self.added,
            "tracks": len(self.tracks), "untagged": self.untagged,
            # A folder where every track is unconfirmed is a record nobody
            # has looked at; one where a few are is usually a download that
            # joined an album already matched, and reads very differently.
            "partial": 0 < self.untagged < len(self.tracks),
        }


def _folder_of(path: str) -> str:
    """The album directory, relative to its library root.

    Navidrome stores paths relative to the library that holds them, and the
    filer puts exactly one album in one directory, so this is the album.
    """
    return path.replace("\\", "/").rsplit("/", 1)[0] if "/" in path.replace("\\", "/") else ""


def _load(connection: sqlite3.Connection,
          identity: navidrome.Identity) -> dict[tuple[int, str], Entry]:
    """Every live track this person can see, grouped into album folders.

    The whole table rather than only the unconfirmed rows, because an entry
    has to say how many of an album's tracks are still unconfirmed - "3 of
    12" and "12 of 12" call for different answers, and the second query that
    would otherwise answer it costs more than reading the column here.
    """
    allowed = [lib["id"] for lib in identity.libraries]
    if not allowed:
        return {}
    names = {lib["id"]: lib["name"] for lib in identity.libraries}

    columns = navidrome.columns_of(connection, "media_file")
    added = "mf.created_at" if "created_at" in columns else "''"
    rows = connection.execute(f"""
        select mf.id, mf.path, coalesce(mf.title, ''),
               coalesce(mf.album, ''), coalesce(mf.album_artist, ''),
               coalesce(mf.artist, ''), coalesce(mf.track_number, 0),
               coalesce(mf.mbz_recording_id, ''), mf.library_id,
               coalesce({added}, '')
          from media_file mf
         where {navidrome.live_clause(connection, allowed)}""").fetchall()

    entries: dict[tuple[int, str], Entry] = {}
    for (track_id, path, title, album, album_artist, artist, track_no,
         mbid, library_id, when) in rows:
        folder = _folder_of(path)
        key = (library_id, folder)
        entry = entries.get(key)
        if entry is None:
            entry = entries[key] = Entry(
                library_id=library_id, library=names.get(library_id, ""),
                artist=album_artist or artist, album=album, folder=folder)
        entry.tracks.append(Track(
            id=track_id, path=path, title=title, artist=artist,
            track_no=track_no or 0, tagged=bool(mbid)))
        # The newest file in the folder. An album is "added" when the last of
        # it arrived, which is what puts a record you are still downloading
        # at the top of the list rather than halfway down it.
        if when > entry.added:
            entry.added = when
    return entries


def listing(identity: navidrome.Identity, limit: int = PAGE,
            offset: int = 0) -> dict[str, Any]:
    """Albums holding at least one track with no MusicBrainz recording ID.

    Newest first. On day one this is large and honest: 1,113 of Alex's 6,495
    tracks have no ID, and that is the real backlog rather than a number
    beets was hiding.
    """
    limit = max(1, min(int(limit), MAX_PAGE))
    offset = max(0, int(offset))

    try:
        connection = navidrome.open_db()
        with connection:
            entries = _load(connection, identity)
    except (navidrome.Unavailable, sqlite3.Error) as exc:
        # Including a column this Navidrome release does not have. The panel
        # saying why beats a 500, which the browser renders as "everything
        # has been matched" - the most reassuring possible way to be wrong.
        log.warning("cannot read the review list: %s", exc)
        return {"available": False, "reason": str(exc), "entries": [],
                "total": 0, "tracks": 0, "untagged": 0,
                "limit": limit, "offset": offset}

    pending = [entry for entry in entries.values() if entry.untagged]
    pending.sort(key=lambda e: (e.added, e.artist, e.album), reverse=True)

    return {
        "available": True,
        "entries": [entry.as_dict() for entry in pending[offset:offset + limit]],
        "total": len(pending),
        # The two numbers that say how far through this is: how much music
        # there is, and how much of it nobody has confirmed.
        "tracks": sum(len(e.tracks) for e in entries.values()),
        "untagged": sum(e.untagged for e in pending),
        "limit": limit,
        "offset": offset,
    }


def album_dir(identity: navidrome.Identity, library_id: int,
              folder: str) -> Path:
    """Where that album actually is on disk, if it really is that person's.

    The folder arrives from the browser, so this is the boundary that has to
    hold: asking to match "../../etc" must not hand back a path outside the
    library. Resolved and compared against the root rather than filtered for
    "..", since a symlink walks out of a filtered name too.
    """
    root = next((Path(lib["path"]) for lib in identity.libraries
                 if str(lib["id"]) == str(library_id)), None)
    if root is None:
        raise ValueError("That library does not belong to this account.")

    # Exactly `$albumartist/$album`, which is what the filer writes and
    # therefore what one album is. Anything shallower is not an album: the
    # library root would hand a matcher the whole collection as one release,
    # and an artist directory would hand it that artist's entire discography
    # - and a retag applies to every file underneath, so being wrong here
    # merges records permanently.
    parts = [part for part in folder.replace("\\", "/").split("/") if part]
    if len(parts) != 2:
        raise ValueError(
            "That is not an album folder. An album lives in "
            "artist/album, and a retag applies to everything inside it.")

    path = root.joinpath(*parts).resolve()
    if root.resolve() not in path.parents:
        raise ValueError("That is not a folder in your library.")
    if not path.is_dir():
        raise ValueError(f"{folder or root.name} is not on disk.")
    return path
