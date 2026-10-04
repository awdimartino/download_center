"""Which album UUID a track belongs to, looked up rather than inferred.

Album membership used to be guessed from where a file sat on disk plus a
majority vote among its neighbours, and that guess is the source of every
identity bug this project has had. `tools/ensure_uuid.py` gave one UUID per
*directory*, so a flat dump of 746 loose tracks spanning 101 albums came out
as one album. The stamper took the commonest existing value among a file's
neighbours, so nine arriving tracks outvoted the one already filed and split
the record in two. Both of those, and the repair pass that existed to undo
them, were deleted with this table's arrival.

None of it is needed if the answer is written down. The first track of an
album mints a uuid4 and records it here; every later track of that album -
same job or months later, downloaded or dropped in by hand - looks it up and
gets the same answer. Membership stops depending on arrival order, on
directory layout, or on how many tracks happen to be present.

The table is per library by construction. A UUID identifies a *file*, not a
recording: Alex's copy and Kelly's copy of one album are different files with
different UUIDs, so an album key is only ever meaningful inside one library.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
import uuid

from . import store

log = logging.getLogger("navidrome_companion.registry")

SCHEMA = """
CREATE TABLE IF NOT EXISTS album_registry (
    library_id  INTEGER NOT NULL,
    album_key   TEXT    NOT NULL,
    album_uuid  TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    PRIMARY KEY (library_id, album_key)
);
"""

# Artist and album are keyed together, separated by a character no key can
# contain: normalisation turns every non-word character into a space, so a
# unit separator cannot arrive from a tag and collide with this one.
SEPARATOR = "\x1f"

# Apostrophes are dropped rather than spaced, because the two spellings of a
# title differ by the character being *there*, not by the gap around it:
# "Don't", "Dont" and "Don’t" are one album. NFKC leaves the typographic
# apostrophe alone, and Spotify writes that one while a hand-typed tag writes
# the plain one, so this is the difference that actually turns up.
_APOSTROPHE = re.compile(r"['‘’ʼ´`]")

# state.db's lock, not one of our own. Every writer to that connection has
# to take the same one: sqlite runs a single transaction per connection, so
# committing under a private lock ends whatever transaction another module is
# midway through. `playcounts.take` writes its snapshot rows and its
# run marker as one unit, and a download filing a track used to be able to
# commit it between the two - leaving the rows stored with nothing recording
# that the day had been done.
_lock = store._lock

# The connection the schema has been applied to. `executescript` issues an
# implicit COMMIT before it runs, so doing it per call was the other half of
# the same problem.
_schema_on: object | None = None


def _store():
    """The state.db handle, with our table guaranteed to exist.

    Compared by identity rather than by a flag, so a reconnect - which is
    what every test does - applies the schema again to the new connection.
    """
    global _schema_on
    conn = store.connection()
    if _schema_on is not conn:
        with _lock:
            conn.executescript(SCHEMA)
        _schema_on = conn
    return conn


def normalize(value: str) -> str:
    """Reduce a tag to the form two spellings of it share.

    Incidental variation only: case, surrounding and repeated whitespace,
    punctuation, and the compatibility forms - full-width characters, ligature
    codepoints - that mean the same letters typed differently. "Abbey Road",
    "abbey road " and "ABBEY ROAD" are one album.

    It makes no semantic judgement about what counts as the same record.
    "Abbey Road" and "Abbey Road (Super Deluxe Edition)" stay different, which
    is the deliberate reversal of what staging did: Spotify already decided
    those are two albums with two names and two ids, every streaming service
    presents them that way, and merging them puts two track 1s, two track 2s
    and so on inside one album.
    """
    folded = unicodedata.normalize("NFKC", value or "").casefold()
    folded = _APOSTROPHE.sub("", folded)
    cleaned = re.sub(r"[^\w]+", " ", folded, flags=re.UNICODE).strip()
    if cleaned:
        return cleaned

    # Nothing survived, because the name is punctuation and nothing else.
    # Those are real: Ed Sheeran's +, -, = and ÷ are four albums, !!! is a
    # band, and this project has already met two tracks called "...". Folding
    # them all to "" would give every one of them the same key and merge four
    # records into one - the exact failure this table exists to prevent.
    #
    # So keep the characters and drop only the spacing, which is the same
    # rule the ordinary path applies: "+ +" and "++" are one name.
    return re.sub(r"\s+", "", folded)


def album_key(albumartist: str, album: str) -> str:
    """The identity of one album, for lookup only - never shown, never a path."""
    return f"{normalize(albumartist)}{SEPARATOR}{normalize(album)}"


def recording_key(artist: str, title: str) -> str:
    """The identity of one song, for "does the library already have this".

    Same shape and the same normalisation as an album key, and here for the
    same reason: the string was being built by hand at both ends and the two
    ends disagreed. Browse's badge was silently off for every track because
    one side joined with the separator and the other did not - a bug whose
    only symptom is an absence, which is the kind that survives longest.

    Approximate on purpose. It is a hint - nothing is refused because of it -
    so a cover, a live take and the studio version all reading as "you have
    this" is a better failure than the badge being wrong about the ordinary
    case.
    """
    return f"{normalize(artist)}{SEPARATOR}{normalize(title)}"


def loose_key(track_uuid: str) -> str:
    """A key of its own, for a file that does not say what album it is on.

    A file with no album tag is not on an album - it is a loose track, and it
    stands alone. Folding them all onto "Unknown Artist / Unknown Album"
    gives every untagged file in a library one shared album UUID, so forty
    unrelated rips become one forty-track record in Navidrome. That is the
    exact failure this table exists to prevent, arrived at from the other
    direction.

    Keyed on the track UUID, which is already unique per file and already
    written into it, so re-filing the same file finds the same row.

    Two separators, which `album_key` can never produce: normalisation
    removes them from a tag, so no real artist and album can collide here.
    """
    return f"{SEPARATOR}{SEPARATOR}{track_uuid}"


def album_uuid_for(library_id: int, albumartist: str, album: str) -> str:
    """The album UUID for this record in this library, minting one on a miss."""
    return uuid_for_key(library_id, album_key(albumartist, album))


def uuid_for_key(library_id: int, key: str, on_miss: str | None = None) -> str:
    """The album UUID for this key, recording one if there is not one yet.

    `on_miss` is what to record when the key is new. A fresh uuid4 by default,
    which is the ordinary case; a caller passes one when the files already
    carry an album UUID - a re-file of something that was in the library
    before this table existed - so that the value on disk is kept rather than
    a second one invented beside it.

    Either way an existing row wins. That is the whole contract: whoever asks
    second gets the same answer as whoever asked first.

    Locked around the read and the write together. Tracks of one album are
    filed concurrently - that is the whole point of the download worker - and
    two threads that both miss and both mint would hand one record two UUIDs,
    which is exactly the split this table exists to prevent.
    """
    store_ = _store()
    with _lock:
        row = store_.execute(
            "SELECT album_uuid FROM album_registry"
            " WHERE library_id = ? AND album_key = ?",
            (library_id, key)).fetchone()
        if row:
            return row[0]

        settled = on_miss or str(uuid.uuid4())
        store_.execute(
            "INSERT INTO album_registry"
            " (library_id, album_key, album_uuid, created_at)"
            " VALUES (?, ?, ?, ?)",
            (library_id, key, settled, time.time()))
        store_.commit()
        return settled


def known(library_id: int, key: str) -> str | None:
    """The recorded UUID for a key, or None. Never mints.

    Locked like every write. state.db is one connection shared by three
    modules, so a read taken outside the lock is a read taken inside
    somebody else's in-flight transaction.
    """
    store_ = _store()
    with _lock:
        row = store_.execute(
            "SELECT album_uuid FROM album_registry"
            " WHERE library_id = ? AND album_key = ?",
            (library_id, key)).fetchone()
    return row[0] if row else None


def repoint(library_id: int, old_key: str, new_key: str) -> str:
    """Follow a retag, and return the UUID the files must carry afterwards.

    Ordinarily the album keeps the UUID it has and only its key moves: its
    Navidrome identity survives, so album-level stars and play counts survive
    with it, and no file needs its UUID rewritten - only its metadata tags
    change.

    The exception is the case that matters. If the new key is already mapped,
    because that album is already in the library correctly tagged, the
    incumbent wins and the retagged files adopt its UUID, merging into the
    record that is already there. It has to be that way round: only the
    newcomer can be rewritten, so choosing the newcomer's value would not move
    the established album, it would split it.

    The old key is dropped once nothing points at it.
    """
    if old_key == new_key:
        return uuid_for_key(library_id, new_key)

    store_ = _store()
    with _lock:
        incumbent = store_.execute(
            "SELECT album_uuid FROM album_registry"
            " WHERE library_id = ? AND album_key = ?",
            (library_id, new_key)).fetchone()
        moving = store_.execute(
            "SELECT album_uuid FROM album_registry"
            " WHERE library_id = ? AND album_key = ?",
            (library_id, old_key)).fetchone()

        if incumbent:
            settled = incumbent[0]
            if moving:
                store_.execute(
                    "DELETE FROM album_registry"
                    " WHERE library_id = ? AND album_key = ?",
                    (library_id, old_key))
                if moving[0] != settled:
                    log.info("retag merged album %s into %s in library %s",
                             moving[0], settled, library_id)
        elif moving:
            settled = moving[0]
            store_.execute(
                "UPDATE album_registry SET album_key = ?"
                " WHERE library_id = ? AND album_key = ?",
                (new_key, library_id, old_key))
        else:
            settled = str(uuid.uuid4())
            store_.execute(
                "INSERT INTO album_registry"
                " (library_id, album_key, album_uuid, created_at)"
                " VALUES (?, ?, ?, ?)",
                (library_id, new_key, settled, time.time()))

        store_.commit()
        return settled


def reassign(library_id: int, key: str, album_uuid: str) -> str:
    """Point a key at a different album UUID, deliberately.

    `uuid_for_key` cannot do this and must not: its whole contract is that
    an existing row wins, so that two tracks of one album filed at the same
    moment cannot end up with two identities. That contract is right for
    filing and wrong for repair - when the recorded UUID is the thing that
    is broken, something has to be able to overwrite it.

    So this is the one door, named for what it does, used by the unfuse
    pass and nothing else. Upserts, because a key that was never recorded
    (the survey refuses to record an ambiguous one) still needs the answer
    once the ambiguity is resolved.
    """
    store_ = _store()
    with _lock:
        store_.execute(
            "INSERT INTO album_registry"
            " (library_id, album_key, album_uuid, created_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(library_id, album_key) DO UPDATE SET"
            "   album_uuid = excluded.album_uuid",
            (library_id, key, album_uuid, time.time()))
        store_.commit()
    return album_uuid


def forget(library_id: int, key: str) -> bool:
    """Drop one mapping. The next track of that album mints a fresh UUID."""
    store_ = _store()
    with _lock:
        cursor = store_.execute(
            "DELETE FROM album_registry WHERE library_id = ? AND album_key = ?",
            (library_id, key))
        store_.commit()
        return cursor.rowcount > 0


def count(library_id: int | None = None) -> int:
    """How many albums are registered, in one library or in all of them."""
    store_ = _store()
    with _lock:
        if library_id is None:
            return store_.execute(
                "SELECT COUNT(*) FROM album_registry").fetchone()[0]
        return store_.execute(
            "SELECT COUNT(*) FROM album_registry WHERE library_id = ?",
            (library_id,)).fetchone()[0]
