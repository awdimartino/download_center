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

import collections
import dataclasses
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import memo, navidrome, store, walk
from .playcounts import GENRE_TAG

log = logging.getLogger("navidrome_companion.library")

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
    # Without an album artist, the track artist decides the folder, so
    # changing it moves the file. The page asks first when this is False.
    has_albumartist: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "path": self.path, "title": self.title,
                "artist": self.artist, "track_no": self.track_no,
                "disc_no": self.disc_no, "tagged": self.tagged,
                "has_albumartist": self.has_albumartist}


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
    # The newest year any track carries, this person's plays of the whole
    # folder, and its running time - what the grid sorts and labels by.
    year: int = 0
    plays: int = 0
    duration: float = 0.0
    # Whether its cover is the wrong shape, as far as the cover survey has
    # found. Never read here: the list cannot open a file per row.
    barred: bool = False

    @property
    def kind(self) -> str:
        """A single is an album of one, which is what every YouTube
        download is and how Spotify presents one."""
        return "single" if self.tracks == 1 else "album"

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
            "year": self.year,
            "plays": self.plays,
            "duration": round(self.duration),
            "kind": self.kind,
            "barred": self.barred,
            # More than one Navidrome album in one folder. Folder-wide
            # actions refuse it, so the page says why before they are tried.
            "albums_here": len(self.album_ids),
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
    year = "mf.year" if "year" in columns else "0"
    duration = "mf.duration" if "duration" in columns else "0"
    # This person's plays only. Navidrome keeps them on the annotation row,
    # which an older release, or the test schema, may not have a column for.
    plays, params = "0", []
    if "play_count" in navidrome.columns_of(connection, "annotation"):
        plays = """(select coalesce(sum(an.play_count), 0) from annotation an
                     where an.item_id = mf.id and an.item_type = 'media_file'
                       and an.user_id = ?)"""
        params = [identity.user_id]
    rows = connection.execute(f"""
        select mf.path, coalesce(mf.album, ''),
               coalesce(mf.album_artist, ''), coalesce(mf.artist, ''),
               coalesce(mf.mbz_recording_id, ''), mf.library_id,
               coalesce({added}, ''), mf.id, coalesce({album_id}, ''),
               {gained}, coalesce({year}, 0), coalesce({duration}, 0),
               {plays}
          from media_file mf
         where {navidrome.live_clause(connection, allowed)}""",
        params).fetchall()

    albums: dict[tuple[int, str], Album] = {}
    for (path, album, album_artist, artist, mbid, library_id, when,
         track_id, navidrome_album, has_gain, track_year, length,
         track_plays) in rows:
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
        found.year = max(found.year, int(track_year or 0))
        found.duration += float(length or 0)
        found.plays += int(track_plays or 0)
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


# What the list can be ordered by. Each ends on the name, so two albums that
# tie on the first key do not swap places between one page and the next.
SORTS = {
    "recent": (lambda a: (a.added, a.sort_name), True),
    "artist": (lambda a: a.sort_name, False),
    "album": (lambda a: (a.album.casefold(), a.artist.casefold()), False),
    "year": (lambda a: (a.year, a.sort_name), True),
    "plays": (lambda a: (a.plays, a.sort_name), True),
}

KINDS = {
    "all": lambda a: True,
    "album": lambda a: a.kind == "album",
    "single": lambda a: a.kind == "single",
}


def _root(identity: navidrome.Identity, library_id: int) -> Path | None:
    return next((Path(lib["path"]) for lib in identity.libraries
                 if lib["id"] == library_id), None)


def _mark_barred(identity: navidrome.Identity, albums: list[Album]) -> None:
    """Flag the covers the survey has already found barred. Reads no files."""
    from . import covers

    for album in albums:
        root = _root(identity, album.library_id)
        if root is not None and album.folder:
            album.barred = covers.barred_known(root / album.folder)


def _songs(connection: sqlite3.Connection, identity: navidrome.Identity,
           needle: str, limit: int = 8) -> list[dict[str, Any]]:
    """Tracks whose title holds the search, for the songs above the albums.

    A title you remember is as often a song as the record it is on, and the
    album search alone found nothing for it.
    """
    allowed = [lib["id"] for lib in identity.libraries]
    if not allowed or not needle:
        return []
    # LIKE treats these as wildcards, and a search for "100%" should not
    # match every title in the library.
    escaped = (needle.replace("\\", "\\\\").replace("%", "\\%")
               .replace("_", "\\_"))
    rows = connection.execute(f"""
        select mf.id, coalesce(mf.title, ''), coalesce(mf.artist, ''),
               coalesce(mf.album, ''), mf.library_id, mf.path
          from media_file mf
         where {navidrome.live_clause(connection, allowed)}
           and lower(mf.title) like ? escape '\\'
         order by mf.title
         limit ?""", (f"%{escaped}%", limit)).fetchall()
    return [{"id": row[0], "title": row[1], "artist": row[2], "album": row[3],
             "library_id": row[4], "folder": folder_of(row[5])}
            for row in rows]


def listing(identity: navidrome.Identity, limit: int = PAGE, offset: int = 0,
            show: str = "all", search: str = "",
            newest_first: bool = True, kind: str = "all",
            sort: str | None = None, artist: str = "") -> dict[str, Any]:
    """The albums this person owns, filtered and paged.

    `show` is one of FILTERS and `kind` one of KINDS. `search` is a plain
    substring of the artist or the album - no ranking, because the answer to
    "where is Abbey Road" should not depend on a scoring function - and on
    the first page it also brings back the songs whose titles hold it.
    `artist` narrows to one album artist exactly, for the artist page.
    """
    if show not in FILTERS:
        raise ValueError(f"There is no {show!r} filter.")
    if kind not in KINDS:
        raise ValueError(f"There is no {kind!r} kind.")
    sort = sort or ("recent" if newest_first else "artist")
    if sort not in SORTS:
        raise ValueError(f"There is no {sort!r} order.")
    limit = max(1, min(int(limit), MAX_PAGE))
    offset = max(0, int(offset))
    needle = (search or "").strip().casefold()

    try:
        connection = navidrome.open_db()
        with connection:
            albums = _cached_load(connection, identity)
            songs = (_songs(connection, identity, needle)
                     if needle and offset == 0 else [])
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

    keep, of_kind = FILTERS[show], KINDS[kind]
    by = artist.strip().casefold()
    shown = [a for a in every
             if keep(a) and of_kind(a)
             and (not by or a.artist.casefold() == by)
             and (not needle or _matches(a, needle))]
    key, descending = SORTS[sort]
    shown.sort(key=key, reverse=descending)
    page = shown[offset:offset + limit]
    _mark_barred(identity, page)

    return {
        "available": True,
        "albums": [a.as_dict() for a in page],
        "songs": songs,
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


def genre_tally(identity: navidrome.Identity) -> dict[str, Any]:
    """How many tracks carry each genre string, exactly as tagged.

    Deliberately not merged or casefolded: the point is to surface variant
    spellings ("Electronic" next to "electronic") as a maintenance list, not
    to hide them. Merging them is a later step.
    """
    allowed = [lib["id"] for lib in identity.libraries]
    if not allowed:
        return {"available": True, "genres": [], "untagged": 0}

    try:
        connection = navidrome.open_db()
        with connection:
            rows = connection.execute(f"""
                select json_extract(mf.tags, '{GENRE_TAG}'), count(*)
                  from media_file mf
                 where {navidrome.live_clause(connection, allowed)}
                 group by 1
            """).fetchall()
    except (navidrome.Unavailable, sqlite3.Error) as exc:
        log.warning("cannot read genres: %s", exc)
        return {"available": False, "reason": str(exc), "genres": [],
                "untagged": 0}

    genres = [{"genre": genre, "tracks": count} for genre, count in rows
              if genre]
    untagged = sum(count for genre, count in rows if not genre)
    genres.sort(key=lambda g: (-g["tracks"], g["genre"].casefold()))
    return {"available": True, "genres": genres, "untagged": untagged}


def _cached_load(connection: sqlite3.Connection,
                 identity: navidrome.Identity) -> dict[tuple[int, str], Album]:
    """`_load`, kept until this person's library or plays change.

    Every Library request walked the whole of media_file: opening an album,
    each inline save (two to five walks with the refresh and the attention
    tab), every combine target. Copies are returned, because the listing
    marks albums reviewed and barred in place, and the cached ones are
    shared.
    """
    allowed = tuple(sorted(lib["id"] for lib in identity.libraries))
    stamp = navidrome.library_stamp(connection, identity.user_id)
    if stamp is None:
        # No way to tell cheaply whether it changed: read it fresh.
        return _load(connection, identity)
    albums = memo.cached(("albums", identity.user_id, allowed), stamp,
                         lambda: _load(connection, identity))
    return {key: dataclasses.replace(album) for key, album in albums.items()}


def _albums_or_raise(identity: navidrome.Identity) -> list[Album]:
    try:
        connection = navidrome.open_db()
        with connection:
            return list(_cached_load(connection, identity).values())
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


# --- artists ------------------------------------------------------------------

def artists(identity: navidrome.Identity) -> dict[str, Any]:
    """Every album artist this person owns, with what they own of each.

    By album artist, the same name a folder is filed under, so an artist
    page holds exactly the albums the filer put under that directory.
    Grouped without regard to case: "boygenius" and "Boygenius" are one
    artist spelled twice, and showing the commoner spelling is enough.
    """
    try:
        every = _albums_or_raise(identity)
    except ValueError as exc:
        return {"available": False, "reason": str(exc), "artists": []}

    grouped: dict[str, list[Album]] = {}
    for album in every:
        grouped.setdefault(album.artist.casefold(), []).append(album)

    found = []
    for albums in grouped.values():
        spellings: dict[str, int] = {}
        for album in albums:
            spellings[album.artist] = spellings.get(album.artist, 0) + album.tracks
        # Its four most played records draw its tile; newest breaks the tie,
        # so a new artist with no plays yet still shows what just arrived.
        ranked = sorted(albums, key=lambda a: (a.plays, a.added), reverse=True)
        found.append({
            "artist": max(spellings, key=spellings.get),
            "albums": sum(1 for a in albums if a.kind == "album"),
            "singles": sum(1 for a in albums if a.kind == "single"),
            "tracks": sum(a.tracks for a in albums),
            "plays": sum(a.plays for a in albums),
            "added": max(a.added for a in albums),
            "art_ids": [a.art_id for a in ranked[:4] if a.art_id],
        })
    found.sort(key=lambda a: a["artist"].casefold())
    return {"available": True, "artists": found}


# --- what needs attention -----------------------------------------------------

# How many of each kind of problem to show as covers. The count is always the
# whole number; the strip is a way in, not the whole list.
SAMPLE = 12


def _titles_by_artist(connection: sqlite3.Connection,
                      identity: navidrome.Identity
                      ) -> dict[tuple[int, str, str], set[str]]:
    """(library, album artist, folder) -> the casefolded titles in it."""
    allowed = [lib["id"] for lib in identity.libraries]
    rows = connection.execute(f"""
        select mf.library_id, mf.path, coalesce(mf.title, ''),
               coalesce(nullif(mf.album_artist, ''), mf.artist, '')
          from media_file mf
         where {navidrome.live_clause(connection, allowed)}""").fetchall()
    found: dict[tuple[int, str, str], set[str]] = {}
    for library_id, path, title, artist in rows:
        key = (library_id, artist.casefold(), folder_of(path))
        found.setdefault(key, set()).add(title.strip().casefold())
    return found


def attention(identity: navidrome.Identity) -> dict[str, Any]:
    """What in this library wants a person, grouped by why.

    Singles that belong together come first, because they are what a
    YouTube download leaves behind: one folder per song, so a record
    downloaded track by track is a dozen "albums". Two or more singles by
    one artist are offered as a group to combine.

    Except a single whose song is already on an album of that artist's -
    that is a duplicate beside its album, not a missing piece of one, and
    combining it would put the song on the album twice. Those are listed
    apart, so they can be set aside instead. (Measured on this library:
    about fifty of the duplicate groups were exactly that.)

    Covers are not here. Finding barred ones means opening a file per album,
    which is `cover_survey`'s job and is asked for separately.
    """
    try:
        connection = navidrome.open_db()
        with connection:
            albums = list(_load(connection, identity).values())
            titles = _titles_by_artist(connection, identity)
    except (navidrome.Unavailable, sqlite3.Error) as exc:
        log.warning("cannot read the library for attention: %s", exc)
        return {"available": False, "reason": str(exc)}

    reviewed = store.reviewed_albums([lib["id"] for lib in identity.libraries])
    for album in albums:
        album.reviewed = bool(album.album_ids) and all(
            (album.library_id, one) in reviewed for one in album.album_ids)
    albums.sort(key=lambda a: (a.added, a.sort_name), reverse=True)
    _mark_barred(identity, albums)

    # Songs on a real album, per artist, to tell a stray single from a
    # duplicate of one.
    on_albums: dict[tuple[int, str], dict[str, str]] = {}
    for album in albums:
        if album.kind != "album":
            continue
        key = (album.library_id, album.artist.casefold())
        for title in titles.get((album.library_id, album.artist.casefold(),
                                 album.folder), ()):
            on_albums.setdefault(key, {}).setdefault(title, album.album)

    groups: dict[tuple[int, str], list[Album]] = {}
    beside = []
    for album in albums:
        if album.kind != "single":
            continue
        key = (album.library_id, album.artist.casefold())
        song = next(iter(titles.get((album.library_id, album.artist.casefold(),
                                     album.folder), {album.album.casefold()})))
        holder = on_albums.get(key, {}).get(song)
        if holder:
            beside.append({**album.as_dict(), "on_album": holder})
        else:
            groups.setdefault(key, []).append(album)

    together = [
        {"library_id": key[0], "artist": members[0].artist,
         "albums": [a.as_dict() for a in members]}
        for key, members in groups.items() if len(members) > 1
    ]
    together.sort(key=lambda g: max(a["added"] for a in g["albums"]),
                  reverse=True)

    review = [a for a in albums if a.needs_review]
    no_gain = [a for a in albums if a.no_gain]
    return {
        "available": True,
        "together": together,
        "beside": beside,
        "review": {"count": len(review),
                   "albums": [a.as_dict() for a in review[:SAMPLE]]},
        "no_gain": {"count": len(no_gain),
                    "albums": [a.as_dict() for a in no_gain[:SAMPLE]]},
    }


def cover_survey(identity: navidrome.Identity) -> dict[str, Any]:
    """Every album whose cover is the wrong shape - YouTube's video frame.

    Opens the first track of every album, which on the Pi is seconds for
    the whole library the first time and almost nothing after: `covers`
    remembers each answer until the file changes.
    """
    from . import covers

    every = _albums_or_raise(identity)
    every.sort(key=lambda a: (a.added, a.sort_name), reverse=True)
    found = []
    for album in every:
        root = _root(identity, album.library_id)
        if root is None or not album.folder:
            continue
        folder = root / album.folder
        if folder.is_dir() and covers.barred(folder):
            album.barred = True
            found.append(album)
    return {"count": len(found), "albums": [a.as_dict() for a in found]}


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
            # Narrowed in SQL to paths under the folder, then checked
            # exactly below. Every row of the library used to be read to
            # show one album's tracks.
            prefix = (folder.replace("\\", "\\\\").replace("%", "\\%")
                      .replace("_", "\\_") + "/%")
            rows = connection.execute(f"""
                select mf.id, mf.path, coalesce(mf.title, ''),
                       coalesce(mf.artist, ''), coalesce(mf.track_number, 0),
                       coalesce({disc}, 0),
                       coalesce(mf.mbz_recording_id, ''),
                       coalesce(nullif(mf.album_artist, ''), mf.artist, ''),
                       coalesce(mf.album, ''),
                       coalesce(mf.album_artist, '')
                  from media_file mf
                 where {navidrome.live_clause(connection, [int(library_id)])}
                   and replace(mf.path, char(92), '/') like ? escape char(92)
                """, (prefix,)).fetchall()
    except (navidrome.Unavailable, sqlite3.Error) as exc:
        raise ValueError(f"Navidrome's database is unreadable: {exc}") from exc

    mine = [row for row in rows if folder_of(row[1]) == folder]
    found = [
        Track(id=row[0], path=row[1], title=row[2], artist=row[3],
              track_no=row[4] or 0, disc_no=row[5] or 0, tagged=bool(row[6]),
              has_albumartist=bool(row[9].strip()))
        for row in mine
    ]
    if not found:
        raise ValueError("That album is not in one of your libraries.")

    found.sort(key=lambda t: (t.disc_no, t.track_no, t.title))
    # The album's own names, as the listing would give them. An album opened
    # from a song in search results only knew the *track* artist, and saving
    # Edit details then wrote "A feat. B" as every file's album artist.
    names = collections.Counter((row[7], row[8]) for row in mine)
    (artist, album), _ = names.most_common(1)[0]
    return {"library_id": int(library_id), "folder": folder,
            "artist": artist, "album": album,
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
    if here.relative_to(root.resolve()).parts[0] == walk.QUARANTINE_NAME:
        raise ValueError("That track has been set aside; it is not part of "
                         "the library.")
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
              folder: str, any_depth: bool = False) -> Path:
    """Where that album actually is on disk, if it really is that person's.

    The folder arrives from the browser, so this is the boundary that has to
    hold: asking to match "../../etc" must not hand back a path outside the
    library. Resolved and compared against the root rather than filtered for
    "..", since a symlink walks out of a filtered name too.

    `any_depth` is for measuring rather than editing: ReplayGain on a folder
    at any depth inside the library changes nothing but gain tags, and
    without it albums filed at depth 1 or 3 were listed for measuring and
    then skipped on every run.
    """
    root = next((Path(lib["path"]) for lib in identity.libraries
                 if str(lib["id"]) == str(library_id)), None)
    if root is None:
        raise ValueError("That library does not belong to this account.")

    parts = [part for part in folder.replace("\\", "/").split("/") if part]
    if any(part in (".", "..") for part in parts):
        raise ValueError("That is not a folder in your library.")
    path = root.joinpath(*parts).resolve()
    base = root.resolve()
    if base not in path.parents:
        raise ValueError("That is not a folder in your library.")
    # Counted after resolving, not before: "Artist/." was two parts and
    # resolved to the artist directory, and a retag applies to everything
    # underneath - that artist's whole discography, merged.
    inside = path.relative_to(base).parts
    if inside[0] == walk.QUARANTINE_NAME:
        raise ValueError("That folder has been set aside; it is not part "
                         "of the library.")
    # Exactly `$albumartist/$album`, which is what the filer writes and
    # therefore what one album is. Anything shallower is not an album: the
    # library root would hand a matcher the whole collection as one release,
    # and an artist directory would hand it that artist's entire discography
    # - and a retag applies to every file underneath, so being wrong here
    # merges records permanently.
    if len(inside) != 2 and not any_depth:
        raise ValueError(
            "That is not an album folder. An album lives in "
            "artist/album, and a retag applies to everything inside it.")
    if not path.is_dir():
        raise ValueError(f"{folder} is not on disk.")
    return path
