"""state.db - the small amount this application has to remember itself.

Everything about the *music* is read from the files and from Navidrome, which
is the whole architecture: the tags are the truth and anything kept here would
eventually disagree with them. What is left is the handful of facts that exist
nowhere else.

- `album_registry`, owned by `registry.py` - which album UUID an album key
  maps to. The one thing that genuinely cannot be derived, because a UUID is
  invented rather than observed.
- `play_snapshot`, `play_anomaly`, `play_imported`, `play_snapshot_run` -
  listening history. Navidrome keeps a cumulative count and one date, so
  anything not captured here is gone for good. This is the only copy.
- `duplicate_dismissed`, `duplicate_quarantined` - decisions a person made
  about duplicate copies, and where the losing files were put.
- `album_reviewed` - albums a person has dealt with, so "needs review" is a
  list that can empty even for music MusicBrainz will never know.

This module owns the connection; each of those owns its own SQL, because the
queries belong with the code that understands them.

**The download ledger used to live here** and was removed on 2026-09-25. It
recorded that a track had been fetched, which stayed true after the file was
deleted, replaced or moved - so a track that left the library became
permanently unfetchable, reported as "skipped" with nothing to say why. Browse
asks Navidrome what the library actually holds instead. An existing `ledger`
table is left on disk rather than dropped; nothing reads it, and destroying
somebody's rows on an upgrade is not this code's decision to make.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
# Bumped by every connect(), so anything cached against this database's
# contents can tell a different database with the same row counts apart.
generation = 0

SCHEMA = """
-- Duplicate groups deliberately kept as they are. Without this a pair you
-- have already looked at and decided to keep comes back every time the
-- library is scanned, and a review list that never shrinks is one nobody
-- reads. Keyed by the group's identity, not by path, so the decision holds
-- when beets moves the files.
-- `decided_by` is the user id of whoever chose "keep both", so one person's
-- decision does not hide a group from somebody else sharing the library.
-- Empty for decisions made before it was recorded, which apply to everyone.
CREATE TABLE IF NOT EXISTS duplicate_dismissed (
    group_key   TEXT NOT NULL,
    decided_by  TEXT NOT NULL DEFAULT '',
    note        TEXT,
    decided_at  TEXT NOT NULL,
    PRIMARY KEY (group_key, decided_by)
);

-- What was actually moved, and where to. Nothing is deleted, but "nothing is
-- deleted" is only useful if you can find the file again: the quarantine
-- directory holds thousands of tracks with no record of which record each
-- came from or why it lost. This is the undo trail.
CREATE TABLE IF NOT EXISTS duplicate_quarantined (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key    TEXT NOT NULL,
    track_id     TEXT NOT NULL,
    library_id   INTEGER,
    title        TEXT,
    artist       TEXT,
    album        TEXT,
    source_path  TEXT NOT NULL,
    target_path  TEXT NOT NULL,
    keeper_id    TEXT,
    keeper_path  TEXT,
    decided_by   TEXT,
    moved_at     TEXT NOT NULL,
    restored_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_quarantined_group
    ON duplicate_quarantined(group_key);

-- Navidrome keeps a *cumulative* play_count and only the most recent
-- play_date, so "what did I listen to in March" is a question its schema
-- cannot answer. These snapshots are the only way to recover it, and only
-- from the day they start: history not captured is gone.
--
-- Keyed by track UUID rather than media_file.id because the id is an index
-- artefact - it changes when a file is re-imported, and the whole point of
-- the UUID work was that identity survives that.
--
-- Only *changed* counts are stored. A full capture is a few thousand rows a
-- night and almost all of it identical to yesterday; storing the changes
-- keeps a year in the tens of thousands rather than near a million. To read
-- the count for a day, take the most recent row at or before it.
CREATE TABLE IF NOT EXISTS play_snapshot (
    taken_on    TEXT NOT NULL,
    track_uuid  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    username    TEXT,
    play_count  INTEGER NOT NULL,
    play_date   TEXT,
    PRIMARY KEY (taken_on, track_uuid, user_id)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_track
    ON play_snapshot(track_uuid, user_id, taken_on);
CREATE INDEX IF NOT EXISTS idx_snapshot_day ON play_snapshot(taken_on);

-- A count that went *down*. A re-import or a counter reset can do that, and
-- it is not minus four plays - it is a fact about the data that any later
-- statistic needs to know, rather than a number to average into one.
-- Listening from before the snapshots started, imported once from Last.fm.
-- Kept in its own table rather than mixed into play_snapshot because the two
-- are different kinds of evidence: a snapshot is a cumulative total this app
-- read itself, an import is somebody else's record of individual plays,
-- matched to a track by text. A later question can ask about either, and the
-- join between them stays visible rather than assumed.
--
-- `source` is part of the key so a second import replaces its own rows and
-- cannot double anyone's history.
-- `played_at` is a date, `YYYY-MM-DD`, for a play whose time is not known,
-- and a full timestamp where it has been recovered. The two sort and bucket
-- alike, which is what lets one column hold both: a date is a prefix of
-- every timestamp on the same day, so it sorts first and still sorts before
-- the next day. Queries bound a day half-open, `>= D and < D+1`, because
-- `<= D` would exclude every timestamped play on D.
CREATE TABLE IF NOT EXISTS play_imported (
    played_at   TEXT NOT NULL,
    track_uuid  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    username    TEXT,
    plays       INTEGER NOT NULL,
    source      TEXT NOT NULL,
    PRIMARY KEY (played_at, track_uuid, user_id, source)
);
CREATE INDEX IF NOT EXISTS idx_imported_day ON play_imported(played_at);

-- That a day was captured, separately from whether anything changed on it.
-- Only changed counts go into play_snapshot, so a day when nobody listened
-- writes no rows at all - and asking "is this day done?" of that table
-- answers no, for ever. The nightly job then re-ran every half hour and the
-- status read permanently out of date. A quiet day is a real answer and
-- needs somewhere to be recorded.
CREATE TABLE IF NOT EXISTS play_snapshot_run (
    day        TEXT PRIMARY KEY,
    taken_at   TEXT NOT NULL,
    tracked    INTEGER NOT NULL,
    changed    INTEGER NOT NULL,
    anomalies  INTEGER NOT NULL
);

-- Albums a person has dealt with, whether or not MusicBrainz knows them. A
-- hand-tagged bootleg never gains a MusicBrainz id, so "no match" cannot be
-- the to-do list; this is the other half of it. Keyed on Navidrome's album
-- id, which PID.Album derives from the album UUID, so a rename keeps it.
-- `how` is what made it count: a match applied, an edit, or a button.
CREATE TABLE IF NOT EXISTS album_reviewed (
    library_id   INTEGER NOT NULL,
    album_id     TEXT NOT NULL,
    how          TEXT NOT NULL,
    reviewed_by  TEXT,
    reviewed_at  TEXT NOT NULL,
    PRIMARY KEY (library_id, album_id)
);

-- When play counts were first read, whether or not that reading stored
-- anything. Rows from that reading are lifetimes, not plays; any later
-- first row of a track is new listening. Taken from play_snapshot alone,
-- a fresh install whose first reading found nothing would mistake its
-- first real plays for a baseline. One row.
CREATE TABLE IF NOT EXISTS play_collection (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    began  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS play_anomaly (
    noticed_on  TEXT NOT NULL,
    track_uuid  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    was         INTEGER NOT NULL,
    became      INTEGER NOT NULL,
    PRIMARY KEY (noticed_on, track_uuid, user_id)
);
"""


def connect(path: Path) -> None:
    global _conn, generation
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False)
    generation += 1
    _migrate(_conn)
    _conn.executescript(SCHEMA)
    # A database collecting since before play_collection existed: its
    # earliest reading is when collection began.
    _conn.execute(
        "INSERT OR IGNORE INTO play_collection (id, began)"
        " SELECT 1, MIN(taken_on) FROM play_snapshot"
        " HAVING MIN(taken_on) IS NOT NULL")
    _conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Changes the CREATE TABLE statements above cannot make on their own.

    Everything here runs before the schema and must be safe on a database
    that has never seen it, safe to run twice, and safe on one that predates
    the change - so each step asks the database what it looks like rather
    than trusting a version number nothing has been keeping.
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}

    # play_imported.day -> played_at. The column held a date because that
    # was all the importer kept; it now holds a timestamp wherever one has
    # been recovered, and a column named `day` holding 17:09:46 is a lie
    # the next reader has to work out for themselves.
    if "play_imported" in tables:
        columns = {row[1] for row in
                   conn.execute("PRAGMA table_info(play_imported)")}
        if "day" in columns and "played_at" not in columns:
            conn.execute(
                "ALTER TABLE play_imported RENAME COLUMN day TO played_at")

    # duplicate_dismissed gains `decided_by`, which is part of its key, so
    # the table is rebuilt rather than altered. Existing decisions keep an
    # empty `decided_by`: nobody recorded who made them, so they still apply
    # to everyone.
    if "duplicate_dismissed" in tables:
        columns = {row[1] for row in
                   conn.execute("PRAGMA table_info(duplicate_dismissed)")}
        if "decided_by" not in columns:
            conn.execute("ALTER TABLE duplicate_dismissed"
                         " RENAME TO duplicate_dismissed_old")
            conn.execute(
                "CREATE TABLE duplicate_dismissed ("
                " group_key TEXT NOT NULL,"
                " decided_by TEXT NOT NULL DEFAULT '',"
                " note TEXT, decided_at TEXT NOT NULL,"
                " PRIMARY KEY (group_key, decided_by))")
            conn.execute(
                "INSERT INTO duplicate_dismissed"
                " (group_key, decided_by, note, decided_at)"
                " SELECT group_key, '', note, decided_at"
                " FROM duplicate_dismissed_old")
            conn.execute("DROP TABLE duplicate_dismissed_old")


def connection() -> sqlite3.Connection:
    """The open state.db handle, for modules that own their own tables.

    Exposed rather than reached for privately: the album registry and the
    play-count snapshots both live in this database, but their SQL belongs
    with the code that understands them.

    Every writer must take `_lock` around a read-then-write, and must not
    commit inside somebody else's transaction - sqlite runs one transaction
    per connection, so an unrelated commit ends whatever is in flight.
    """
    assert _conn is not None, "state.db not connected"
    return _conn


# --- duplicate review decisions -------------------------------------------

def dismiss_duplicate(group_key: str, note: str = "",
                      decided_by: str = "") -> None:
    """Remember that a duplicate group was looked at and left alone, by whom."""
    assert _conn is not None, "state.db not connected"
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO duplicate_dismissed"
            " (group_key, decided_by, note, decided_at) VALUES (?, ?, ?, ?)",
            (group_key, decided_by, note, stamp))
        _conn.commit()


def dismissed_duplicates(user_id: str | None = None) -> set[str]:
    """Groups this person left alone, plus the decisions recorded before
    anyone's were (those apply to everyone). None means every decision."""
    assert _conn is not None, "state.db not connected"
    with _lock:
        if user_id is None:
            rows = _conn.execute("SELECT group_key FROM duplicate_dismissed")
        else:
            rows = _conn.execute(
                "SELECT group_key FROM duplicate_dismissed"
                " WHERE decided_by IN ('', ?)", (user_id,))
        return {row[0] for row in rows}


# --- albums somebody has dealt with ---------------------------------------

def mark_reviewed(library_id: int, album_ids: set[str], how: str,
                  by: str | None = None) -> None:
    """Record that these albums have been looked at. Later marks win."""
    assert _conn is not None, "state.db not connected"
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with _lock:
        _conn.executemany(
            "INSERT OR REPLACE INTO album_reviewed"
            " (library_id, album_id, how, reviewed_by, reviewed_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [(library_id, one, how, by, stamp) for one in album_ids])
        _conn.commit()


def unmark_reviewed(library_id: int, album_ids: set[str]) -> None:
    assert _conn is not None, "state.db not connected"
    with _lock:
        _conn.executemany(
            "DELETE FROM album_reviewed WHERE library_id = ? AND album_id = ?",
            [(library_id, one) for one in album_ids])
        _conn.commit()


def reviewed_albums(library_ids: list[int]) -> set[tuple[int, str]]:
    """(library_id, album_id) for every reviewed album in these libraries."""
    assert _conn is not None, "state.db not connected"
    if not library_ids:
        return set()
    marks = ", ".join("?" * len(library_ids))
    with _lock:
        return {(row[0], row[1]) for row in _conn.execute(
            f"SELECT library_id, album_id FROM album_reviewed"
            f" WHERE library_id IN ({marks})", library_ids)}


# --- what was set aside ---------------------------------------------------

def record_quarantine(group_key: str, copy: Any, keeper: Any | None,
                      source: str, target: str, decided_by: str) -> None:
    """Note that a copy was moved, and where it went.

    `copy` and `keeper` are duplicates.Copy objects; only the fields worth
    reading back later are stored, so this module keeps no dependency on that
    one. Recorded after the move so the row only ever describes a file that
    is really at `target`.

    `keeper` is None for a track set aside by hand rather than as the loser
    of a duplicate pair - there is no other copy for it to have lost to.
    """
    assert _conn is not None, "state.db not connected"
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with _lock:
        _conn.execute(
            "INSERT INTO duplicate_quarantined"
            " (group_key, track_id, library_id, title, artist, album,"
            "  source_path, target_path, keeper_id, keeper_path, decided_by,"
            "  moved_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (group_key, copy.id, copy.library_id, copy.title, copy.artist,
             copy.album, source, target,
             keeper.id if keeper is not None else None,
             keeper.path if keeper is not None else None, decided_by, stamp))
        _conn.commit()


def quarantined_track_ids() -> set[str]:
    """media_file ids whose file has been set aside and not put back.

    Read by the duplicates page so a resolved group disappears at once.
    Navidrome does not know a file has moved until it rescans, so without
    this the group it was just asked to resolve comes straight back - which
    reads, from the outside, as the button not working.
    """
    assert _conn is not None, "state.db not connected"
    with _lock:
        return {row[0] for row in _conn.execute(
            "SELECT track_id FROM duplicate_quarantined"
            "  WHERE restored_at IS NULL")}


def quarantined(limit: int = 200,
                include_restored: bool = False) -> list[dict[str, Any]]:
    """Everything set aside, newest first. The list you undo from."""
    assert _conn is not None, "state.db not connected"
    where = "" if include_restored else " WHERE restored_at IS NULL"
    with _lock:
        rows = _conn.execute(
            "SELECT id, group_key, track_id, library_id, title, artist, album,"
            "       source_path, target_path, keeper_id, keeper_path,"
            "       decided_by, moved_at, restored_at"
            f"  FROM duplicate_quarantined{where}"
            "  ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    keys = ("id", "group_key", "track_id", "library_id", "title", "artist",
            "album", "source_path", "target_path", "keeper_id", "keeper_path",
            "decided_by", "moved_at", "restored_at")
    # strict: the column list and the SELECT above have to stay in step, and
    # a silent truncation here would shift every field one to the left.
    return [dict(zip(keys, row, strict=True)) for row in rows]


