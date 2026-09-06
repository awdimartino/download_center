"""Shared fixtures.

Everything here builds real files and real SQLite databases in a tmp_path.
Nothing is mocked that can be built for free: the bugs these tests exist to
catch were all cases of code that looked right and did not run - a query
against a column that was not there, a regex that did not match what it
claimed, a path assembled from the wrong root. A mock would have agreed with
every one of them.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ledger  # noqa: E402


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    """A connected ledger on a throwaway database."""
    path = tmp_path / "state.db"
    ledger.connect(path)
    yield path
    if ledger._conn is not None:
        ledger._conn.close()
        ledger._conn = None


# --- a stand-in for Navidrome's schema ------------------------------------
# Only the columns this app actually reads. Written out rather than dumped
# from a real database so a schema change shows up as a test that has to be
# updated deliberately.

NAVIDROME_SCHEMA = """
CREATE TABLE library (
    id INTEGER PRIMARY KEY, name TEXT, path TEXT, last_scan_at TEXT
);
CREATE TABLE folder (id TEXT PRIMARY KEY, missing INTEGER DEFAULT 0);
CREATE TABLE media_file (
    id TEXT PRIMARY KEY,
    path TEXT, title TEXT, album TEXT, artist TEXT, album_artist TEXT,
    suffix TEXT, bit_rate INTEGER, duration REAL, size INTEGER,
    mbz_recording_id TEXT, track_number INTEGER, rg_track_gain REAL,
    tags TEXT, library_id INTEGER, folder_id TEXT, missing INTEGER DEFAULT 0
);
CREATE TABLE "user" (id TEXT PRIMARY KEY, user_name TEXT);
CREATE TABLE user_library (user_id TEXT, library_id INTEGER);
CREATE TABLE annotation (
    user_id TEXT, item_id TEXT, item_type TEXT,
    starred INTEGER DEFAULT 0, rating INTEGER DEFAULT 0
);
"""


@pytest.fixture
def navidrome_db(tmp_path):
    """A Navidrome-shaped database with two libraries and two users.

    Mirrors the real deployment: alex sees library 1, kelly sees library 2,
    and neither is meant to see the other's collection.
    """
    path = tmp_path / "navidrome.db"
    connection = sqlite3.connect(path)
    connection.executescript(NAVIDROME_SCHEMA)
    connection.execute(
        "insert into library (id, name, path) values (1, 'Music', ?)",
        (str(tmp_path / "music"),))
    connection.execute(
        "insert into library (id, name, path) values (2, 'Kelly', ?)",
        (str(tmp_path / "kelly"),))
    connection.execute("insert into folder (id, missing) values ('f1', 0)")
    connection.execute("insert into folder (id, missing) values ('gone', 1)")
    connection.executemany(
        'insert into "user" (id, user_name) values (?, ?)',
        [("u-alex", "alex"), ("u-kelly", "kelly")])
    connection.executemany(
        "insert into user_library (user_id, library_id) values (?, ?)",
        [("u-alex", 1), ("u-kelly", 2)])
    connection.commit()
    connection.close()
    (tmp_path / "music").mkdir()
    (tmp_path / "kelly").mkdir()
    return path


def add_track(db: Path, track_id: str, **fields) -> None:
    """Insert one media_file row, defaulting everything not named."""
    row = {
        "id": track_id, "path": f"{track_id}.mp3", "title": "Song",
        "album": "Album", "artist": "Artist", "album_artist": "Artist",
        "suffix": "mp3", "bit_rate": 320, "duration": 200.0,
        "size": 8_000_000, "mbz_recording_id": "", "track_number": 1,
        "rg_track_gain": None, "tags": None, "library_id": 1,
        "folder_id": "f1", "missing": 0,
    }
    row.update(fields)
    connection = sqlite3.connect(db)
    with connection:
        connection.execute(
            "insert into media_file ({}) values ({})".format(
                ", ".join(row), ", ".join("?" * len(row))),
            tuple(row.values()))
    connection.close()


def annotate(db: Path, user_id: str, item_id: str,
             starred: int = 0, rating: int = 0) -> None:
    connection = sqlite3.connect(db)
    with connection:
        connection.execute(
            "insert into annotation (user_id, item_id, item_type, starred,"
            " rating) values (?, ?, 'media_file', ?, ?)",
            (user_id, item_id, starred, rating))
    connection.close()


@pytest.fixture
def identity(tmp_path):
    """alex, who can see library 1 only."""
    from app.navidrome import Identity
    return Identity(
        user_id="u-alex", username="alex", is_admin=False, token="t",
        subsonic_token="st", subsonic_salt="ss",
        libraries=[{"id": 1, "name": "Music", "path": str(tmp_path / "music")}],
    )
