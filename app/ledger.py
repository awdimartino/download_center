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
    spotify_id   TEXT PRIMARY KEY,
    isrc         TEXT,
    title        TEXT NOT NULL,
    artist       TEXT NOT NULL,
    album        TEXT NOT NULL,
    file_path    TEXT,
    completed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_isrc ON ledger(isrc);
"""


def connect(path: Path) -> None:
    global _conn
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.executescript(SCHEMA)
    _conn.commit()


def already_downloaded(spotify_id: str, isrc: str | None) -> bool:
    """True if this track was fetched before, by Spotify id or by ISRC.

    ISRC is checked as well because the same recording is issued under many
    Spotify ids across regional releases and reissues.
    """
    assert _conn is not None, "ledger not connected"
    with _lock:
        if _conn.execute(
            "SELECT 1 FROM ledger WHERE spotify_id = ?", (spotify_id,)
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
            " (spotify_id, isrc, title, artist, album, file_path, completed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item["spotify_id"], item.get("isrc"), item["title"],
             item["artist"], item["album"], file_path, stamp),
        )
        _conn.commit()


def count() -> int:
    assert _conn is not None, "ledger not connected"
    with _lock:
        return _conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
