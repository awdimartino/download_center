"""Every album in a person's library, and which of them MusicBrainz knows.

This was the Review list, which showed only albums with something
unconfirmed. That made two things impossible: seeing what you own, and
reaching an album to correct it once a match had been applied - because a
matched album dropped off the only list that carried the button.

So it lists everything, and the narrowing is a filter rather than the
definition.

**"No MusicBrainz match" is that filter, and it is named for what it is.**
It means no MusicBrainz recording ID, read off Navidrome's index every time,
stored nowhere - so it cannot drift out of sync the way `import_refusal` did.
It is deliberately *not* called "untagged" any more, because a doujin release
or a bootleg can be tagged perfectly by hand and will still never have an ID.
Those albums live in this filter for ever and that is correct; the count is a
statement about MusicBrainz, not a queue of work you can finish.

**Per user, strictly private.** Scoped to the signed-in account's own
libraries through `identity.libraries`, which is Navidrome's own answer to
who may see what. An admin does not see another person's library.

**The album is the unit.** A row is a folder, because the filer puts exactly
one album in one directory, and because every action here is album-level: one
album is one UUID, and a per-track correction is what splits a record in two.
Tracks are read separately, for one album at a time, when a row is opened -
the listing counts them rather than building them, or paging 2,800 albums
would materialise 6,800 track objects to show fifty rows.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import navidrome

log = logging.getLogger("download_center.library")

# One page. Sending a whole library to a phone helps nobody.
PAGE = 50
MAX_PAGE = 200


@dataclass(frozen=True)
class Track:
    id: str
    path: str
    title: str
    artist: str
    track_no: int
    disc_no: int
    tagged: bool

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "path": self.path, "title": self.title,
                "artist": self.artist, "track_no": self.track_no,
                "disc_no": self.disc_no, "tagged": self.tagged}


@dataclass
class Album:
    """One album folder, and how much of it MusicBrainz has confirmed.

    Counts rather than tracks. The listing only ever reports two numbers per
    row, and building an object per file to reach them is what made a page
    of fifty albums read six thousand rows' worth of allocations.
    """

    library_id: int
    library: str
    artist: str
    album: str
    folder: str
    added: str = ""
    tracks: int = 0
    untagged: int = 0

    @property
    def sort_name(self) -> str:
        return f"{self.artist} - {self.album}".casefold()

    def as_dict(self) -> dict[str, Any]:
        return {
            "library_id": self.library_id, "library": self.library,
            "artist": self.artist, "album": self.album,
            "folder": self.folder, "added": self.added,
            "tracks": self.tracks, "untagged": self.untagged,
            # Three states, not two: an album nobody has confirmed reads very
            # differently from one where a download joined a matched record.
            "matched": self.untagged == 0,
            "partial": 0 < self.untagged < self.tracks,
        }


def _folder_of(path: str) -> str:
    """The album directory, relative to its library root.

    Navidrome stores paths relative to the library that holds them, and the
    filer puts exactly one album in one directory, so this is the album.
    """
    normalised = path.replace("\\", "/")
    return normalised.rsplit("/", 1)[0] if "/" in normalised else ""


def _load(connection: sqlite3.Connection,
          identity: navidrome.Identity) -> dict[tuple[int, str], Album]:
    """Every live album this person can see, as counts.

    Five columns, not ten, and no object per file. The whole table is still
    walked - Navidrome has no column for "which folder" and deriving one in
    SQL means string surgery on a path this code does not own - but what
    comes back per row is now two integers and a date.
    """
    allowed = [lib["id"] for lib in identity.libraries]
    if not allowed:
        return {}
    names = {lib["id"]: lib["name"] for lib in identity.libraries}

    columns = navidrome.columns_of(connection, "media_file")
    added = "mf.created_at" if "created_at" in columns else "''"
    rows = connection.execute(f"""
        select mf.path, coalesce(mf.album, ''),
               coalesce(mf.album_artist, ''), coalesce(mf.artist, ''),
               coalesce(mf.mbz_recording_id, ''), mf.library_id,
               coalesce({added}, '')
          from media_file mf
         where {navidrome.live_clause(connection, allowed)}""").fetchall()

    albums: dict[tuple[int, str], Album] = {}
    for path, album, album_artist, artist, mbid, library_id, when in rows:
        key = (library_id, _folder_of(path))
        found = albums.get(key)
        if found is None:
            found = albums[key] = Album(
                library_id=library_id, library=names.get(library_id, ""),
                artist=album_artist or artist, album=album,
                folder=_folder_of(path))
        found.tracks += 1
        if not mbid:
            found.untagged += 1
        # The newest file in the folder. An album is "added" when the last of
        # it arrived, which is what puts a record you are still downloading
        # at the top rather than halfway down.
        if when > found.added:
            found.added = when
    return albums


def _matches(album: Album, needle: str) -> bool:
    return needle in album.artist.casefold() or needle in album.album.casefold()


def listing(identity: navidrome.Identity, limit: int = PAGE, offset: int = 0,
            unmatched_only: bool = False, search: str = "",
            newest_first: bool = True) -> dict[str, Any]:
    """The albums this person owns, filtered and paged.

    `unmatched_only` narrows to albums with at least one track MusicBrainz
    has not confirmed. `search` is a plain substring of the artist or the
    album - no ranking, because the answer to "where is Abbey Road" should
    not depend on a scoring function.
    """
    limit = max(1, min(int(limit), MAX_PAGE))
    offset = max(0, int(offset))
    needle = (search or "").strip().casefold()

    try:
        connection = navidrome.open_db()
        with connection:
            albums = _load(connection, identity)
    except (navidrome.Unavailable, sqlite3.Error) as exc:
        # Including a column this Navidrome release does not have. The panel
        # saying why beats a 500, which the browser renders as an empty
        # library - the most alarming possible way to be wrong.
        log.warning("cannot read the library: %s", exc)
        return {"available": False, "reason": str(exc), "albums": [],
                "total": 0, "tracks": 0, "unmatched": 0,
                "unmatched_albums": 0, "limit": limit, "offset": offset}

    every = list(albums.values())
    shown = [a for a in every
             if (not unmatched_only or a.untagged)
             and (not needle or _matches(a, needle))]
    if newest_first:
        shown.sort(key=lambda a: (a.added, a.sort_name), reverse=True)
    else:
        shown.sort(key=lambda a: a.sort_name)

    return {
        "available": True,
        "albums": [a.as_dict() for a in shown[offset:offset + limit]],
        "total": len(shown),
        # About the whole library, not the filtered page: these are what the
        # filter is a filter *of*, so they must not move when it is applied.
        "albums_total": len(every),
        "tracks": sum(a.tracks for a in every),
        "unmatched": sum(a.untagged for a in every),
        "unmatched_albums": sum(1 for a in every if a.untagged),
        "limit": limit,
        "offset": offset,
    }


def tracks(identity: navidrome.Identity, library_id: int,
           folder: str) -> dict[str, Any]:
    """One album's tracks, read when a row is opened.

    Its own query rather than a slice of the listing's: the listing counts,
    and paying for every track in the library to show twelve of them is the
    cost this split exists to avoid.
    """
    allowed = [lib["id"] for lib in identity.libraries]
    if int(library_id) not in allowed:
        raise ValueError("That library does not belong to this account.")

    try:
        connection = navidrome.open_db()
        with connection:
            # Probed, not assumed. Naming a column a Navidrome release does
            # not have fails the query outright rather than degrading, and
            # this reads a database another application owns.
            columns = navidrome.columns_of(connection, "media_file")
            disc = "mf.disc_number" if "disc_number" in columns else "0"
            rows = connection.execute(f"""
                select mf.id, mf.path, coalesce(mf.title, ''),
                       coalesce(mf.artist, ''), coalesce(mf.track_number, 0),
                       coalesce({disc}, 0),
                       coalesce(mf.mbz_recording_id, '')
                  from media_file mf
                 where {navidrome.live_clause(connection, [int(library_id)])}
                """).fetchall()
    except (navidrome.Unavailable, sqlite3.Error) as exc:
        raise ValueError(f"Navidrome's database is unreadable: {exc}") from exc

    found = [
        Track(id=row[0], path=row[1], title=row[2], artist=row[3],
              track_no=row[4] or 0, disc_no=row[5] or 0, tagged=bool(row[6]))
        for row in rows if _folder_of(row[1]) == folder
    ]
    if not found:
        raise ValueError("That album is not in one of your libraries.")

    found.sort(key=lambda t: (t.disc_no, t.track_no, t.title))
    return {"library_id": int(library_id), "folder": folder,
            "items": [t.as_dict() for t in found]}


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
        raise ValueError(f"{folder} is not on disk.")
    return path
