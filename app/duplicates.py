"""Finding tracks held more than once, and choosing which copy to keep.

The earlier deduplication worked by destination collision: two files that beets
would file to the same path had to be the same track. That catches a lot and
is blind to the rest, because a copy tagged "- Remastered 2009", or ripped to
FLAC while the other is MP3, lands at a different path and never collides.

So this compares tracks rather than paths, and it deliberately does not trust
one signal:

  * a shared MusicBrainz recording id is the strongest evidence there is, and
    is the only thing allowed to resolve without a person looking
  * the same artist and title, normalised, with near-identical duration, is
    strong evidence and still gets reviewed

Nothing is ever deleted. The losing file moves to a quarantine directory, and
its stars and ratings are migrated onto the keeper through Navidrome's API
first - so quality decides which copy survives, rather than which one happens
to carry an annotation.
"""

from __future__ import annotations

import collections
import hashlib
import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ledger, navidrome
from .config import settings

log = logging.getLogger("download_center.duplicates")

# Qualifiers that mark a different pressing of one recording rather than a
# different song, so an original and its reissue compare equal.
_EDITION = re.compile(
    r"\s*[-(\[]\s*(?:\d{4}\s+)?(?:digital\s+)?"
    r"(?:remaster(?:ed)?(?:\s*\d{4})?|\d{4}\s*remaster|deluxe|bonus\s*track|"
    r"album\s*version|single\s*version|explicit|clean|mono|stereo|"
    r"radio\s*edit|extended(?:\s*mix)?)"
    r"[^)\]]*[)\]]?\s*$", re.I)
_FEATURING = re.compile(r"\s*[\(\[]?\s*(?:feat|ft)\.?\s+[^)\]]*[\)\]]?\s*$", re.I)

# Two files of the same recording differ by less than this. Beyond it they are
# a different edit, and which one you want is a matter of taste, not quality.
SAME_RECORDING_SECONDS = 5.0
# Tighter still before anything happens unattended.
CONFIDENT_SECONDS = 1.0

LOSSLESS = {"flac", "wav", "alac", "ape", "wv", "aiff"}


def _album_key(album: str) -> str:
    """Album names compared loosely, for deciding if two copies are on one
    record. Casing and punctuation vary between imports of the same release."""
    return re.sub(r"[^a-z0-9]+", "", (album or "").lower())


def normalise(title: str) -> str:
    text = (title or "").strip().lower()
    # Repeated because a title can carry several qualifiers at once.
    for _ in range(3):
        text = _EDITION.sub("", text)
        text = _FEATURING.sub("", text)
    return re.sub(r"[^a-z0-9]+", "", text)


@dataclass
class Copy:
    id: str
    path: str
    title: str
    album: str
    artist: str
    suffix: str
    bit_rate: int
    duration: float
    size: int
    mbid: str
    # The track's own artist, kept beside the album artist: grouping on the
    # album artist buckets every track of a compilation together.
    track_artist: str
    # This person's own annotations, and a note of anyone else holding one.
    # Kept apart deliberately: conflating them made "starred" mean "starred
    # by somebody", so a star could be quarantined away with its file.
    starred: bool
    rating: int
    library_id: int
    library: str
    starred_by_others: str = ""

    @property
    def lossless(self) -> bool:
        return (self.suffix or "").lower() in LOSSLESS

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "path": self.path, "title": self.title,
            "album": self.album, "artist": self.artist, "suffix": self.suffix,
            "bit_rate": self.bit_rate, "duration": round(self.duration or 0),
            "size": self.size, "mbid": self.mbid, "starred": self.starred,
            "rating": self.rating, "lossless": self.lossless,
            "library": self.library,
            "starred_by_others": self.starred_by_others,
        }


@dataclass
class Group:
    key: str
    reason: str
    copies: list[Copy]
    keeper: Copy
    confident: bool
    why: str = ""

    @property
    def dismiss_key(self) -> str:
        """What "keep both" is remembered against.

        Derived from the files themselves rather than from how they were
        found. A group is discovered by MusicBrainz id or by title, and the
        title key in particular changes whenever the grouping is refined - so
        keying the decision on that would quietly resurrect every pair
        somebody had already looked at and settled.
        """
        joined = "|".join(sorted(c.id for c in self.copies))
        return "files:" + hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.dismiss_key, "reason": self.reason,
            "confident": self.confident, "why": self.why,
            "keeper": self.keeper.id,
            "copies": [c.as_dict() for c in self.copies],
        }


def _can_migrate(copy: Copy, identity: navidrome.Identity) -> bool:
    """Whether every annotation on this copy could be carried elsewhere.

    Our own star can be re-created on another file through the API. Somebody
    else's cannot: acting as them is not possible, so quarantining their copy
    would take their star with it.
    """
    return not copy.starred_by_others


def _rank(copy: Copy, identity: navidrome.Identity) -> tuple:
    """Better copies sort higher.

    Quality decides, but only among copies whose annotations can be carried
    across. A star this session cannot migrate is protected in place instead,
    so the copy holding it outranks a better file.
    """
    return (not _can_migrate(copy, identity), copy.lossless,
            copy.bit_rate or 0, copy.size or 0, 1 if copy.mbid else 0)


def _clearly_better(best: Copy, rest: list[Copy]) -> str:
    """Why the winner wins, or empty if the choice is a toss-up.

    A near-tie is not something to settle unattended: if two copies are the
    same format within a few percent of each other, keeping either is fine
    and neither is worth acting on without being asked.
    """
    second = rest[0]
    if best.lossless and not second.lossless:
        return f"{best.suffix} over {second.suffix}"
    if best.lossless == second.lossless:
        a, b = best.bit_rate or 0, second.bit_rate or 0
        # A tenth is comfortably past re-encoding noise: 320 against 267 is a
        # real difference, 320 against 310 is not worth calling a winner.
        if b and a >= b * 1.1:
            return f"{a}k over {b}k"
    return ""


def _load(connection: sqlite3.Connection,
          identity: navidrome.Identity) -> list[Copy]:
    """Every live track, with which library it belongs to and who starred it.

    Both matter. A library is one person's collection, and an annotation
    belongs to a user - neither is a property of the file.
    """
    live = ("mf.missing = 0 and mf.folder_id in "
            "(select id from folder where missing = 0)")
    # Only libraries this person may see. Somebody else's collection is not
    # theirs to deduplicate, and their copy of a song is not a duplicate of
    # anything.
    allowed = [lib["id"] for lib in identity.libraries]
    if not allowed:
        return []
    live += " and mf.library_id in (%s)" % ",".join("?" * len(allowed))
    rows = connection.execute(f"""
        select mf.id, mf.path, mf.title, mf.album, mf.artist, mf.album_artist,
               mf.suffix, mf.bit_rate, mf.duration, mf.size,
               coalesce(mf.mbz_recording_id, ''),
               mf.library_id, coalesce(l.name, ''),
               coalesce(mine.starred, 0), coalesce(mine.rating, 0),
               (select group_concat(u.user_name)
                  from annotation an join user u on u.id = an.user_id
                 where an.item_id = mf.id and an.item_type = 'media_file'
                   and an.starred = 1 and an.user_id != ?)
          from media_file mf
          left join library l on l.id = mf.library_id
          left join annotation mine
            on mine.item_id = mf.id and mine.item_type = 'media_file'
           and mine.user_id = ?
         where {live}""", (identity.user_id, identity.user_id, *allowed)).fetchall()
    return [
        Copy(id=r[0], path=r[1], title=r[2] or "", album=r[3] or "",
             artist=(r[5] or r[4] or ""), suffix=r[6] or "",
             bit_rate=r[7] or 0, duration=r[8] or 0.0, size=r[9] or 0,
             mbid=r[10], track_artist=(r[4] or r[5] or ""),
             library_id=r[11] or 0, library=r[12] or "",
             starred=bool(r[13]), rating=r[14] or 0,
             starred_by_others=r[15] or "")
        for r in rows
    ]


def find(connection: sqlite3.Connection,
         identity: navidrome.Identity) -> list[Group]:
    """Every group of copies that look like the same recording."""
    copies = _load(connection, identity)
    dismissed = ledger.dismissed_duplicates()
    groups: list[Group] = []
    # The same pair is often found twice - once by MusicBrainz id and once by
    # title - and listing it twice would have someone resolve it, then meet it
    # again. MusicBrainz groups are built first, so a title group covering
    # nothing new is dropped.
    emitted: list[frozenset[str]] = []

    def build(key: str, reason: str, members: list[Copy]) -> None:
        members_key = frozenset(c.id for c in members)
        if any(members_key <= previous for previous in emitted):
            return
        joined = "|".join(sorted(c.id for c in members))
        settled = "files:" + hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]
        if settled in dismissed:
            # Recorded as emitted even so. The same pair is found twice, once
            # by MusicBrainz id and once by title, and returning early here
            # let a dismissed pair come straight back under the other key.
            emitted.append(members_key)
            return
        lengths = [c.duration for c in members]
        if max(lengths) - min(lengths) > SAME_RECORDING_SECONDS:
            return
        ordered = sorted(members, key=lambda c: _rank(c, identity),
                         reverse=True)
        why = _clearly_better(ordered[0], ordered[1:])
        # Copies on different records are not a duplicate at all: one
        # recording is often issued as a single and again on the album it
        # belongs to, and removing either leaves that album a track short.
        # Compared loosely, so "Timeless" and "TIMELESS" still count as one.
        one_album = len({_album_key(c.album) for c in members}) == 1
        # Only a shared MusicBrainz id earns the right to act unattended.
        confident = bool(
            why
            and one_album
            and reason == "musicbrainz"
            and max(lengths) - min(lengths) <= CONFIDENT_SECONDS
        )
        if not one_album and why:
            why += " — but they are on different albums"
        emitted.append(members_key)
        groups.append(Group(key, reason, ordered, ordered[0], confident, why))

    # Grouping is per library, always. A library is one person's collection:
    # the same song held by two people is not a duplicate of anything, and
    # treating it as one would delete somebody else's music to keep the
    # higher-bitrate copy of a file that was never theirs.
    by_mbid: dict[tuple, list[Copy]] = collections.defaultdict(list)
    by_title: dict[tuple, list[Copy]] = collections.defaultdict(list)
    for copy in copies:
        if copy.mbid:
            by_mbid[(copy.library_id, copy.mbid)].append(copy)
        title = normalise(copy.title)
        if title:
            # Grouped on the track's own artist. Album artist buckets every
            # track on a compilation under "Various Artists", where different
            # songs of similar length start looking like copies of each other.
            by_title[(copy.library_id, copy.track_artist.strip().lower(),
                      title)].append(copy)

    for (library, mbid), members in sorted(by_mbid.items()):
        if len(members) > 1:
            build(f"mb:{library}:{mbid}", "musicbrainz", members)
    for (library, artist, title), members in sorted(by_title.items()):
        if len(members) > 1:
            build(f"t:{library}:{artist}|{title}", "title", members)

    groups.sort(key=lambda g: (not g.confident, g.copies[0].artist.lower()))
    return groups


# --- acting on a group ----------------------------------------------------

def _quarantine_dir() -> Path:
    path = settings.music_dir.parent / "duplicates-removed"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _library_root(copy: Copy, identity: navidrome.Identity) -> Path:
    """Where this copy actually lives on disk.

    Paths in Navidrome are relative to the library that holds them, so a
    library other than our own cannot be resolved against music_dir.
    """
    for library in identity.libraries:
        if library["id"] == copy.library_id:
            return Path(library["path"])
    return settings.music_dir


def resolve(group: Group, keeper_id: str,
            identity: navidrome.Identity) -> dict[str, Any]:
    """Keep one copy, set the others aside, and move annotations across."""
    keeper = next((c for c in group.copies if c.id == keeper_id), None)
    if keeper is None:
        raise ValueError("that copy is not in this group")

    losers = [c for c in group.copies if c.id != keeper_id]

    # Refuse before touching anything. Checking as we went meant the first
    # loser's star was already written to the keeper by the time a later one
    # turned out to be somebody else's - leaving a half-applied change that
    # repeated on every retry.
    stranded = [f"{c.path}: starred by {c.starred_by_others}"
                for c in losers if not _can_migrate(c, identity)]
    if stranded:
        raise ValueError(
            "That copy carries an annotation this app cannot move: "
            + "; ".join(stranded))

    # Annotations move first: if quarantining fails afterwards the worst case
    # is a star on both copies, whereas the reverse loses it outright.
    migrated = []
    if any(c.starred for c in losers) and not keeper.starred:
        if not navidrome.star(identity, keeper.id):
            # These report failure rather than raising, and quarantining
            # anyway would destroy the very annotation this was meant to
            # carry across. Nothing has moved yet, so stopping costs nothing.
            raise ValueError(
                "Could not move the star onto the copy you are keeping, so "
                "nothing was removed. Check that Navidrome is reachable.")
        migrated.append("starred")

    best_rating = max((c.rating for c in losers), default=0)
    if best_rating > keeper.rating:
        if not navidrome.set_rating(identity, keeper.id, best_rating):
            raise ValueError(
                "Could not move the rating onto the copy you are keeping, so "
                "nothing was removed. Check that Navidrome is reachable.")
        migrated.append(f"rated {best_rating}")

    moved, failed = [], []
    for loser in losers:
        source = _library_root(loser, identity) / loser.path
        if not source.exists():
            failed.append(f"{loser.path}: already gone")
            continue
        target = _quarantine_dir() / source.name
        n = 2
        while target.exists():
            target = _quarantine_dir() / f"{source.stem} ({n}){source.suffix}"
            n += 1
        try:
            shutil.move(str(source), str(target))
            moved.append(str(target.name))
        except Exception as exc:
            failed.append(f"{loser.path}: {type(exc).__name__}: {exc}")

    log.info("duplicate resolved: kept %s, quarantined %d", keeper.path, len(moved))
    return {"kept": keeper.path, "quarantined": moved,
            "migrated": migrated, "failed": failed}


def auto_resolve(connection: sqlite3.Connection, identity: navidrome.Identity,
                 apply: bool = False) -> dict[str, Any]:
    """Act only on groups a shared MusicBrainz id makes unambiguous."""
    groups = [g for g in find(connection, identity) if g.confident]
    if not apply:
        return {"eligible": len(groups),
                "preview": [g.as_dict() for g in groups[:20]]}

    resolved, failures = 0, []
    for group in groups:
        try:
            outcome = resolve(group, group.keeper.id, identity)
            failures.extend(outcome["failed"])
            resolved += 1
        except Exception as exc:
            failures.append(f"{group.key}: {type(exc).__name__}: {exc}")
    return {"resolved": resolved, "failed": failures}
