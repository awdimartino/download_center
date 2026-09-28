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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import navidrome, store

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
    # Tracks Navidrome has no ReplayGain for. Read off the same column the
    # health check counts, so the two cannot disagree.
    no_gain: int = 0
    # Any one track in the album, so the row can ask Navidrome for its
    # cover. Navidrome keys art on a track or album id, not on a folder.
    art_id: str = ""
    # Navidrome's album ids for the files in this folder - normally one.
    # Derived from the album UUID through PID.Album, so it survives a rename
    # and is what "somebody has reviewed this" is keyed on.
    album_ids: set[str] = field(default_factory=set)
    reviewed: bool = False

    @property
    def sort_name(self) -> str:
        return f"{self.artist} - {self.album}".casefold()

    def as_dict(self) -> dict[str, Any]:
        return {
            "library_id": self.library_id, "library": self.library,
            "artist": self.artist, "album": self.album,
            "folder": self.folder, "added": self.added,
            "tracks": self.tracks, "untagged": self.untagged,
            "no_gain": self.no_gain,
            "art_id": self.art_id,
            # Three states, not two: an album nobody has confirmed reads very
            # differently from one where a download joined a matched record.
            "matched": self.untagged == 0,
            "partial": 0 < self.untagged < self.tracks,
            "reviewed": self.reviewed,
            "needs_review": self.needs_review,
        }

    @property
    def needs_review(self) -> bool:
        """Unconfirmed by MusicBrainz and not yet looked at by a person.

        The narrower question the "no MusicBrainz match" filter cannot answer:
        a hand-tagged bootleg sits in that filter for ever, and correctly, but
        once somebody has dealt with it it is not work any more.
        """
        return bool(self.untagged) and not self.reviewed


def folder_of(path: str) -> str:
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
    album_id = "mf.album_id" if "album_id" in columns else "''"
    # Null is "never measured"; 0.0 is a real answer. FIXES item 22.
    gained = ("mf.rg_track_gain is not null" if "rg_track_gain" in columns
              else "1")
    rows = connection.execute(f"""
        select mf.path, coalesce(mf.album, ''),
               coalesce(mf.album_artist, ''), coalesce(mf.artist, ''),
               coalesce(mf.mbz_recording_id, ''), mf.library_id,
               coalesce({added}, ''), mf.id, coalesce({album_id}, ''),
               {gained}
          from media_file mf
         where {navidrome.live_clause(connection, allowed)}""").fetchall()

    albums: dict[tuple[int, str], Album] = {}
    for (path, album, album_artist, artist, mbid, library_id, when,
         track_id, navidrome_album, has_gain) in rows:
        key = (library_id, folder_of(path))
        found = albums.get(key)
        if found is None:
            found = albums[key] = Album(
                library_id=library_id, library=names.get(library_id, ""),
                artist=album_artist or artist, album=album,
                folder=folder_of(path))
        found.tracks += 1
        if not found.art_id:
            found.art_id = track_id
        if not mbid:
            found.untagged += 1
        if not has_gain:
            found.no_gain += 1
        if navidrome_album:
            found.album_ids.add(navidrome_album)
        # The newest file in the folder. An album is "added" when the last of
        # it arrived, which is what puts a record you are still downloading
        # at the top rather than halfway down.
        if when > found.added:
            found.added = when
    return albums


def _matches(album: Album, needle: str) -> bool:
    return needle in album.artist.casefold() or needle in album.album.casefold()


# What the list can be narrowed to. "unmatched" is a statement about
# MusicBrainz and never empties; "review" is the part of it nobody has dealt
# with yet, which does.
FILTERS = {
    "all": lambda a: True,
    "unmatched": lambda a: bool(a.untagged),
    "review": lambda a: a.needs_review,
    "nogain": lambda a: bool(a.no_gain),
}


def listing(identity: navidrome.Identity, limit: int = PAGE, offset: int = 0,
            show: str = "all", search: str = "",
            newest_first: bool = True) -> dict[str, Any]:
    """The albums this person owns, filtered and paged.

    `show` is one of FILTERS. `search` is a plain substring of the artist or
    the album - no ranking, because the answer to "where is Abbey Road"
    should not depend on a scoring function.
    """
    if show not in FILTERS:
        raise ValueError(f"There is no {show!r} filter.")
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
                "unmatched_albums": 0, "review_albums": 0, "no_gain": 0,
                "no_gain_albums": 0, "limit": limit, "offset": offset}

    every = list(albums.values())
    reviewed = store.reviewed_albums([lib["id"] for lib in identity.libraries])
    for album in every:
        # Every Navidrome album in the folder, not any one of them: a folder
        # holding two album ids is two records sharing a directory, and
        # reviewing one says nothing about the other.
        album.reviewed = bool(album.album_ids) and all(
            (album.library_id, one) in reviewed for one in album.album_ids)

    keep = FILTERS[show]
    shown = [a for a in every
             if keep(a) and (not needle or _matches(a, needle))]
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
        "review_albums": sum(1 for a in every if a.needs_review),
        "no_gain": sum(a.no_gain for a in every),
        "no_gain_albums": sum(1 for a in every if a.no_gain),
        "limit": limit,
        "offset": offset,
    }


def _albums_or_raise(identity: navidrome.Identity) -> list[Album]:
    try:
        connection = navidrome.open_db()
        with connection:
            return list(_load(connection, identity).values())
    except (navidrome.Unavailable, sqlite3.Error) as exc:
        raise ValueError(f"Navidrome's database is unreadable: {exc}") from exc


def album_ids(identity: navidrome.Identity, library_id: int,
              folder: str) -> set[str]:
    """Navidrome's album ids for one folder, as it stands right now.

    Read *before* an edit, because afterwards Navidrome has not rescanned
    and the folder may not exist. A rename keeps the album UUID, so the id
    read here is still the album's id once the scan catches up.
    """
    found = next((a for a in _albums_or_raise(identity)
                  if a.library_id == int(library_id) and a.folder == folder),
                 None)
    if found is None:
        raise ValueError("That album is not in one of your libraries.")
    if not found.album_ids:
        raise ValueError(
            "Navidrome has no album id for that folder yet; rescan and try "
            "again.")
    return found.album_ids


def without_gain(identity: navidrome.Identity) -> list[tuple[int, str]]:
    """(library_id, folder) for every album with a track lacking ReplayGain.

    Newest first, so a long run does what was downloaded last before
    working back through the archive.
    """
    albums = [a for a in _albums_or_raise(identity) if a.no_gain and a.folder]
    albums.sort(key=lambda a: (a.added, a.sort_name), reverse=True)
    return [(a.library_id, a.folder) for a in albums]


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
        for row in rows if folder_of(row[1]) == folder
    ]
    if not found:
        raise ValueError("That album is not in one of your libraries.")

    found.sort(key=lambda t: (t.disc_no, t.track_no, t.title))
    return {"library_id": int(library_id), "folder": folder,
            "items": [t.as_dict() for t in found]}


def track_path(identity: navidrome.Identity, library_id: int,
               path: str) -> Path:
    """Where one track is on disk, if it really is that person's.

    The same boundary `album_dir` is, for the same reason: the path arrives
    from the browser. It must be inside a library this account can see, and
    it must be a file - a directory here would hand an editor something it
    is not equipped to treat as one track.
    """
    root = next((Path(lib["path"]) for lib in identity.libraries
                 if str(lib["id"]) == str(library_id)), None)
    if root is None:
        raise ValueError("That library does not belong to this account.")

    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if not parts:
        raise ValueError("That is not a track.")

    here = root.joinpath(*parts).resolve()
    if root.resolve() not in here.parents:
        raise ValueError("That is not a track in your library.")
    if not here.is_file():
        raise ValueError(f"{path} is not on disk.")
    return here


def owns_track(identity: navidrome.Identity, track_id: str) -> bool:
    """Whether a media_file id belongs to a library this account may see.

    The id arrives from the browser on its way to Navidrome's cover-art
    endpoint, which is called with *service* credentials - so without this
    an id typed by hand would fetch art from somebody else's collection.
    """
    allowed = [lib["id"] for lib in identity.libraries]
    if not allowed or not track_id:
        return False
    try:
        connection = navidrome.open_db()
        with connection:
            row = connection.execute(
                "select library_id from media_file where id = ?",
                (track_id,)).fetchone()
    except (navidrome.Unavailable, sqlite3.Error):
        return False
    return bool(row) and row[0] in allowed


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
