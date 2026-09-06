"""The ledger, and the fact that it is scoped to a library.

It was global until 2026-09-06. The consequence was not subtle: the second
person to use the app would queue an album, every track would be marked
`skipped` because somebody else already held it, the job would report
`complete`, and nothing would be written to disk.
"""

from __future__ import annotations

import sqlite3

import pytest

from app import ledger


ALEX, KELLY = 1, 2


def _item(source_id="sp-1", isrc=None, title="Song"):
    return {"spotify_id": source_id, "isrc": isrc, "title": title,
            "artist": "Artist", "album": "Album"}


# --- scoping ----------------------------------------------------------------

def test_a_track_held_in_one_library_is_not_held_in_another(state_db):
    """The bug, stated directly."""
    ledger.record(_item("sp-1"), "/music/a.mp3", ALEX)

    assert ledger.already_downloaded("sp-1", None, ALEX) is True
    assert ledger.already_downloaded("sp-1", None, KELLY) is False


def test_two_libraries_can_each_hold_the_same_track(state_db):
    """The composite key has to allow this; the old single-column primary key
    would have replaced one row with the other."""
    ledger.record(_item("sp-1"), "/music/a.mp3", ALEX)
    ledger.record(_item("sp-1"), "/kelly/a.mp3", KELLY)

    assert ledger.already_downloaded("sp-1", None, ALEX)
    assert ledger.already_downloaded("sp-1", None, KELLY)
    assert ledger.count() == 2


def test_isrc_matching_is_scoped_too(state_db):
    """The same recording is issued under many Spotify ids, so ISRC is
    checked as well - and it has to be scoped, or it reopens the hole the
    source id check just closed."""
    ledger.record(_item("sp-1", isrc="GB1234567890"), None, ALEX)

    assert ledger.already_downloaded("sp-different", "GB1234567890", ALEX)
    assert not ledger.already_downloaded("sp-different", "GB1234567890", KELLY)


def test_count_can_be_asked_per_library_or_overall(state_db):
    ledger.record(_item("sp-1"), None, ALEX)
    ledger.record(_item("sp-2"), None, ALEX)
    ledger.record(_item("sp-3"), None, KELLY)

    assert ledger.count(ALEX) == 2
    assert ledger.count(KELLY) == 1
    assert ledger.count() == 3


def test_re_recording_the_same_track_does_not_duplicate_it(state_db):
    ledger.record(_item("sp-1"), "/music/a.mp3", ALEX)
    ledger.record(_item("sp-1"), "/music/moved.mp3", ALEX)
    assert ledger.count(ALEX) == 1


# --- forgetting -------------------------------------------------------------

def test_forgetting_lets_a_track_be_downloaded_again(state_db):
    ledger.record(_item("sp-1"), None, ALEX)
    assert ledger.forget("sp-1", ALEX) is True
    assert ledger.already_downloaded("sp-1", None, ALEX) is False


def test_forgetting_only_touches_the_library_asked_about(state_db):
    ledger.record(_item("sp-1"), None, ALEX)
    ledger.record(_item("sp-1"), None, KELLY)

    ledger.forget("sp-1", ALEX)
    assert not ledger.already_downloaded("sp-1", None, ALEX)
    assert ledger.already_downloaded("sp-1", None, KELLY)


def test_forgetting_something_not_held_reports_that(state_db):
    assert ledger.forget("never-had-it", ALEX) is False


# --- migrating an existing ledger -------------------------------------------

def _legacy_db(path, rows, column="source_id"):
    """A ledger in the shape it had before library_id existed."""
    connection = sqlite3.connect(path)
    connection.executescript(f"""
        CREATE TABLE ledger (
            {column}     TEXT PRIMARY KEY,
            isrc         TEXT,
            title        TEXT NOT NULL,
            artist       TEXT NOT NULL,
            album        TEXT NOT NULL,
            file_path    TEXT,
            completed_at TEXT NOT NULL
        );
        CREATE INDEX idx_ledger_isrc ON ledger(isrc);
    """)
    connection.executemany(
        f"INSERT INTO ledger ({column}, isrc, title, artist, album,"
        " file_path, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()


def test_existing_rows_are_attributed_to_the_first_library(tmp_path):
    """Everything already in there was downloaded into Music Library before
    a second one existed, so that is where it belongs."""
    path = tmp_path / "state.db"
    _legacy_db(path, [
        ("sp-1", "GB1", "One", "A", "Album", "/music/1.mp3", "2026-01-01"),
        ("sp-2", None, "Two", "A", "Album", "/music/2.mp3", "2026-01-02"),
    ])

    ledger.connect(path)
    try:
        assert ledger.count() == 2
        assert ledger.already_downloaded("sp-1", None, ledger.LEGACY_LIBRARY_ID)
        assert not ledger.already_downloaded("sp-1", None, 2)
    finally:
        ledger._conn.close()
        ledger._conn = None


def test_migration_preserves_every_field(tmp_path):
    path = tmp_path / "state.db"
    _legacy_db(path, [
        ("sp-1", "GB1", "One", "Artist", "Album", "/music/1.mp3", "2026-01-01"),
    ])

    ledger.connect(path)
    try:
        row = ledger._conn.execute(
            "SELECT source_id, library_id, isrc, title, artist, album,"
            " file_path, completed_at FROM ledger").fetchone()
        assert row == ("sp-1", ledger.LEGACY_LIBRARY_ID, "GB1", "One",
                       "Artist", "Album", "/music/1.mp3", "2026-01-01")
    finally:
        ledger._conn.close()
        ledger._conn = None


def test_the_oldest_shape_migrates_all_the_way(tmp_path):
    """spotify_id -> source_id -> (source_id, library_id), in one pass."""
    path = tmp_path / "state.db"
    _legacy_db(path, [
        ("sp-1", None, "One", "A", "Album", None, "2026-01-01"),
    ], column="spotify_id")

    ledger.connect(path)
    try:
        columns = {r[1] for r in ledger._conn.execute(
            "PRAGMA table_info(ledger)")}
        assert "source_id" in columns and "library_id" in columns
        assert "spotify_id" not in columns
        assert ledger.already_downloaded("sp-1", None, ledger.LEGACY_LIBRARY_ID)
    finally:
        ledger._conn.close()
        ledger._conn = None


def test_migration_restores_the_index_it_had_to_drop(tmp_path):
    """The rebuild drops the table and its indexes with it."""
    path = tmp_path / "state.db"
    _legacy_db(path, [("sp-1", "GB1", "One", "A", "Al", None, "2026-01-01")])

    ledger.connect(path)
    try:
        indexes = {r[0] for r in ledger._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_ledger_isrc" in indexes
    finally:
        ledger._conn.close()
        ledger._conn = None


def test_connecting_twice_is_harmless(tmp_path):
    """connect() runs on every start, so the migration must be a no-op the
    second time rather than rebuilding the table again."""
    path = tmp_path / "state.db"
    ledger.connect(path)
    ledger.record(_item("sp-1"), None, ALEX)
    ledger._conn.close()

    ledger.connect(path)
    try:
        assert ledger.count() == 1
        assert ledger.already_downloaded("sp-1", None, ALEX)
    finally:
        ledger._conn.close()
        ledger._conn = None


def test_a_fresh_database_needs_no_migration(tmp_path):
    path = tmp_path / "state.db"
    ledger.connect(path)
    try:
        assert ledger.count() == 0
        ledger.record(_item("sp-1"), None, ALEX)
        assert ledger.count(ALEX) == 1
    finally:
        ledger._conn.close()
        ledger._conn = None
