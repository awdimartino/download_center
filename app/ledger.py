"""Durable record of which tracks have already been downloaded.

This is the only state worth persisting. Beets moves finished files out of the
staging directory and into the music library, so checking the filesystem can
never answer "do I already have this track" - the file is gone from where we
put it. Job and queue state lives in memory instead; a restart mid-album is
recovered by pasting the link again, which this table then makes cheap.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    source_id    TEXT PRIMARY KEY,
    isrc         TEXT,
    title        TEXT NOT NULL,
    artist       TEXT NOT NULL,
    album        TEXT NOT NULL,
    file_path    TEXT,
    completed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_isrc ON ledger(isrc);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Rename the original spotify_id column now that ids are namespaced.

    Existing ledgers were written before non-Spotify sources existed, so the
    key column was named for the only source there was. Renaming preserves
    every row; a fresh database is created with the new name and skips this.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ledger)")}
    if "spotify_id" in columns and "source_id" not in columns:
        conn.execute("ALTER TABLE ledger RENAME COLUMN spotify_id TO source_id")


def connect(path: Path) -> None:
    global _conn
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.executescript(SCHEMA)
    _migrate(_conn)
    _conn.commit()


def already_downloaded(source_id: str, isrc: str | None) -> bool:
    """True if this track was fetched before, by source id or by ISRC.

    Source ids are namespaced per extractor ("youtube:dQw4w9WgXcQ"); bare
    values are Spotify ids, kept unprefixed so existing ledgers still match.
    ISRC is checked as well because the same recording is issued under many
    Spotify ids across regional releases and reissues.
    """
    assert _conn is not None, "ledger not connected"
    with _lock:
        if _conn.execute(
            "SELECT 1 FROM ledger WHERE source_id = ?", (source_id,)
        ).fetchone():
            return True
        if isrc and _conn.execute(
            "SELECT 1 FROM ledger WHERE isrc = ?", (isrc,)
        ).fetchone():
            return True
    return False


def record(item: dict[str, Any], file_path: str | None) -> None:
    assert _conn is not None, "ledger not connected"
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO ledger"
            " (source_id, isrc, title, artist, album, file_path, completed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item["spotify_id"], item.get("isrc"), item["title"],
             item["artist"], item["album"], file_path, stamp),
        )
        _conn.commit()


def count() -> int:
    assert _conn is not None, "ledger not connected"
    with _lock:
        return _conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
