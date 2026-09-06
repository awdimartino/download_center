"""Durable record of which tracks have already been downloaded.

This is the only state worth persisting. Beets moves finished files out of the
staging directory and into the music library, so checking the filesystem can
never answer "do I already have this track" - the file is gone from where we
put it. Job and queue state lives in memory instead; a restart mid-album is
recovered by pasting the link again, which this table then makes cheap.

**Scoped to a library, not to a person.** The question it answers is "is this
recording already in this collection", and a collection is a library. Two
accounts writing into one library share the answer, which is what stops an
administrator re-fetching what somebody else already filed there; two
libraries do not, which is what stops Kelly's first download being silently
skipped because alex already owns the record.

It was global until 2026-09-06, which meant exactly that second failure: her
job would report `complete` with every track `skipped` and nothing written.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

# The library every pre-existing row is attributed to. Rows written before
# the ledger was scoped came from the single-library era, and Navidrome
# numbers libraries from one.
LEGACY_LIBRARY_ID = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    source_id    TEXT NOT NULL,
    library_id   INTEGER NOT NULL,
    isrc         TEXT,
    title        TEXT NOT NULL,
    artist       TEXT NOT NULL,
    album        TEXT NOT NULL,
    file_path    TEXT,
    completed_at TEXT NOT NULL,
    PRIMARY KEY (source_id, library_id)
);
-- Leading on library_id because every lookup is scoped to one.
CREATE INDEX IF NOT EXISTS idx_ledger_isrc ON ledger(library_id, isrc);

-- Duplicate groups deliberately kept as they are. Without this a pair you
-- have already looked at and decided to keep comes back every time the
-- library is scanned, and a review list that never shrinks is one nobody
-- reads. Keyed by the group's identity, not by path, so the decision holds
-- when beets moves the files.
CREATE TABLE IF NOT EXISTS duplicate_dismissed (
    group_key   TEXT PRIMARY KEY,
    note        TEXT,
    decided_at  TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS play_anomaly (
    noticed_on  TEXT NOT NULL,
    track_uuid  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    was         INTEGER NOT NULL,
    became      INTEGER NOT NULL,
    PRIMARY KEY (noticed_on, track_uuid, user_id)
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing ledger up to the current shape.

    Runs before the schema is applied, so it works on the table as it stands
    and lets CREATE TABLE IF NOT EXISTS handle anything genuinely new. A
    fresh database has no ledger table at all; every guard here simply finds
    nothing and does nothing.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ledger)")}
    if not columns:
        return

    # Existing ledgers were written before non-Spotify sources existed, so
    # the key column was named for the only source there was.
    if "spotify_id" in columns and "source_id" not in columns:
        conn.execute("ALTER TABLE ledger RENAME COLUMN spotify_id TO source_id")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ledger)")}

    if "library_id" in columns:
        return

    # Adding library_id means changing the primary key from source_id to
    # (source_id, library_id), and SQLite cannot alter a primary key - so the
    # table is rebuilt. Every existing row is attributed to the library it
    # was actually downloaded into, which for a ledger written before this
    # column existed is the first one.
    conn.execute("""
        CREATE TABLE ledger_migrating (
            source_id    TEXT NOT NULL,
            library_id   INTEGER NOT NULL,
            isrc         TEXT,
            title        TEXT NOT NULL,
            artist       TEXT NOT NULL,
            album        TEXT NOT NULL,
            file_path    TEXT,
            completed_at TEXT NOT NULL,
            PRIMARY KEY (source_id, library_id)
        )""")
    conn.execute(
        "INSERT INTO ledger_migrating"
        " (source_id, library_id, isrc, title, artist, album, file_path,"
        "  completed_at)"
        " SELECT source_id, ?, isrc, title, artist, album, file_path,"
        "        completed_at FROM ledger",
        (LEGACY_LIBRARY_ID,))
    conn.execute("DROP TABLE ledger")
    conn.execute("ALTER TABLE ledger_migrating RENAME TO ledger")
    conn.commit()


def connect(path: Path) -> None:
    global _conn
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False)
    # Migrate first, then apply the schema: the rebuild drops the old table
    # and its indexes with it, and CREATE ... IF NOT EXISTS puts them back.
    _migrate(_conn)
    _conn.executescript(SCHEMA)
    _conn.commit()


def connection() -> sqlite3.Connection:
    """The open state.db handle, for modules that own their own tables.

    Exposed rather than reached for privately: play-count snapshots live in
    this database but their SQL belongs with the code that understands them.
    """
    assert _conn is not None, "ledger not connected"
    return _conn


def already_downloaded(source_id: str, isrc: str | None,
                       library_id: int) -> bool:
    """True if this track is already in that library, by source id or ISRC.

    Source ids are namespaced per extractor ("youtube:dQw4w9WgXcQ"); bare
    values are Spotify ids, kept unprefixed so existing ledgers still match.
    ISRC is checked as well because the same recording is issued under many
    Spotify ids across regional releases and reissues.

    `library_id` is required rather than optional. A default would silently
    reintroduce the bug: every caller that forgot it would go back to asking
    whether *anybody* holds the track.
    """
    assert _conn is not None, "ledger not connected"
    with _lock:
        if _conn.execute(
            "SELECT 1 FROM ledger WHERE source_id = ? AND library_id = ?",
            (source_id, library_id),
        ).fetchone():
            return True
        if isrc and _conn.execute(
            "SELECT 1 FROM ledger WHERE isrc = ? AND library_id = ?",
            (isrc, library_id),
        ).fetchone():
            return True
    return False


def record(item: dict[str, Any], file_path: str | None,
           library_id: int) -> None:
    assert _conn is not None, "ledger not connected"
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO ledger"
            " (source_id, library_id, isrc, title, artist, album, file_path,"
            "  completed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item["spotify_id"], library_id, item.get("isrc"), item["title"],
             item["artist"], item["album"], file_path, stamp),
        )
        _conn.commit()


def forget(source_id: str, library_id: int) -> bool:
    """Drop a row so the track can be fetched again.

    The ledger is the only thing that knows a track was downloaded, because
    beets moved the file out of staging. So when a file leaves the library -
    deleted, lost, replaced by hand - nothing notices, and the track stays
    unfetchable forever. This is the way back.
    """
    assert _conn is not None, "ledger not connected"
    with _lock:
        cursor = _conn.execute(
            "DELETE FROM ledger WHERE source_id = ? AND library_id = ?",
            (source_id, library_id))
        _conn.commit()
        return cursor.rowcount > 0


def count(library_id: int | None = None) -> int:
    """How many tracks are held, in one library or in all of them."""
    assert _conn is not None, "ledger not connected"
    with _lock:
        if library_id is None:
            return _conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        return _conn.execute(
            "SELECT COUNT(*) FROM ledger WHERE library_id = ?",
            (library_id,)).fetchone()[0]


# --- duplicate review decisions -------------------------------------------

def dismiss_duplicate(group_key: str, note: str = "") -> None:
    """Remember that a duplicate group was looked at and left alone."""
    assert _conn is not None, "ledger not connected"
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO duplicate_dismissed"
            " (group_key, note, decided_at) VALUES (?, ?, ?)",
            (group_key, note, stamp))
        _conn.commit()


def dismissed_duplicates() -> set[str]:
    assert _conn is not None, "ledger not connected"
    with _lock:
        return {row[0] for row in
                _conn.execute("SELECT group_key FROM duplicate_dismissed")}


def undismiss_duplicate(group_key: str) -> None:
    assert _conn is not None, "ledger not connected"
    with _lock:
        _conn.execute("DELETE FROM duplicate_dismissed WHERE group_key = ?",
                      (group_key,))
        _conn.commit()


# --- what was set aside ---------------------------------------------------

def record_quarantine(group_key: str, copy: Any, keeper: Any,
                      source: str, target: str, decided_by: str) -> None:
    """Note that a losing copy was moved, and where it went.

    `copy` and `keeper` are duplicates.Copy objects; only the fields worth
    reading back later are stored, so this module keeps no dependency on that
    one. Recorded after the move so the row only ever describes a file that
    is really at `target`.
    """
    assert _conn is not None, "ledger not connected"
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with _lock:
        _conn.execute(
            "INSERT INTO duplicate_quarantined"
            " (group_key, track_id, library_id, title, artist, album,"
            "  source_path, target_path, keeper_id, keeper_path, decided_by,"
            "  moved_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (group_key, copy.id, copy.library_id, copy.title, copy.artist,
             copy.album, source, target, keeper.id, keeper.path, decided_by,
             stamp))
        _conn.commit()


def quarantined(limit: int = 200,
                include_restored: bool = False) -> list[dict[str, Any]]:
    """Everything set aside, newest first. The list you undo from."""
    assert _conn is not None, "ledger not connected"
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


def mark_restored(entry_id: int) -> None:
    assert _conn is not None, "ledger not connected"
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with _lock:
        _conn.execute(
            "UPDATE duplicate_quarantined SET restored_at = ? WHERE id = ?",
            (stamp, entry_id))
        _conn.commit()
