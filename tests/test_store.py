"""state.db - the connection, and what it is still allowed to remember.

This replaced `test_ledger.py`. Most of what that file tested was the download
ledger and the several reshapings of its table, all of which went with the
ledger itself on 2026-09-25. What survives is the connection handling, which
every other module reaches state.db through, plus the one rule that binds
them: sqlite runs a single transaction per connection, so a module that
commits under its own lock can end one another module is midway through.
"""

from __future__ import annotations

import sqlite3

from app import registry, store


def _tables(path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute(
            "select name from sqlite_master where type='table'")}
    finally:
        connection.close()


def _close() -> None:
    if store._conn is not None:
        store._conn.close()
        store._conn = None


# --- connecting -------------------------------------------------------------

def test_connecting_twice_is_harmless(tmp_path):
    """connect() runs on every start, so applying the schema again has to be
    a no-op rather than an error."""
    path = tmp_path / "state.db"
    store.connect(path)
    store.dismiss_duplicate("group-1", "keep both")
    _close()

    store.connect(path)
    try:
        assert store.dismissed_duplicates() == {"group-1"}
    finally:
        _close()


def test_a_fresh_database_gets_every_table(tmp_path):
    path = tmp_path / "state.db"
    store.connect(path)
    try:
        assert {"duplicate_dismissed", "duplicate_quarantined",
                "play_snapshot", "play_anomaly", "play_imported",
                "play_snapshot_run"} <= _tables(path)
    finally:
        _close()


def test_a_fresh_database_does_not_recreate_what_was_removed(tmp_path):
    """The ledger and the sweep's two tables are gone. A new install should
    never grow them again - and an existing one keeps whatever it has, which
    is why nothing here drops anything."""
    path = tmp_path / "state.db"
    store.connect(path)
    try:
        assert not ({"ledger", "import_refusal", "sweep_run"} & _tables(path))
    finally:
        _close()


def test_an_existing_ledger_table_is_left_alone(tmp_path):
    """Destroying somebody's rows on an upgrade is not this code's decision
    to make. Nothing reads them; they simply stay until a person drops them.
    """
    path = tmp_path / "state.db"
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("create table ledger (source_id text primary key)")
        connection.execute("insert into ledger values ('sp-1')")
    connection.close()

    store.connect(path)
    try:
        assert "ledger" in _tables(path)
        assert store.connection().execute(
            "select count(*) from ledger").fetchone()[0] == 1
    finally:
        _close()


def test_asking_for_the_connection_before_connecting_fails_loudly(tmp_path):
    """Silently minting UUIDs nothing will remember is far worse than an
    error naming the missing step."""
    _close()
    try:
        store.connection()
    except AssertionError as exc:
        assert "state.db" in str(exc)
    else:
        raise AssertionError("connection() should refuse before connect()")


# --- sharing one connection -------------------------------------------------

def test_every_writer_takes_the_same_lock(tmp_path):
    """The registry used to guard state.db with a private lock and commit
    under it, which could end the play-count snapshot's transaction between
    its rows and its run marker - leaving the day's plays stored with nothing
    recording that the day had been done."""
    from app import playcounts

    assert registry._lock is store._lock
    assert playcounts.store._lock is store._lock


def test_the_registry_schema_is_applied_once_per_connection(tmp_path):
    """`executescript` issues an implicit COMMIT, so running it on every
    access was the other half of the same problem."""
    store.connect(tmp_path / "state.db")
    try:
        registry.uuid_for_key(
            1, registry.album_key("The Beatles", "Abbey Road"))
        first = registry._schema_on
        registry.uuid_for_key(1, registry.album_key("The Beatles", "Revolver"))
        assert registry._schema_on is first
    finally:
        _close()


def test_a_reconnect_applies_the_registry_schema_again(tmp_path):
    """Which is what every test does, and what a restart does."""
    store.connect(tmp_path / "one.db")
    registry.uuid_for_key(1, registry.album_key("A", "B"))
    _close()

    store.connect(tmp_path / "two.db")
    try:
        assert registry.count() == 0
        registry.uuid_for_key(1, registry.album_key("A", "B"))
        assert registry.count() == 1
    finally:
        _close()


# --- "keep both" belongs to whoever decided it (CODE_REVIEW M32) -----------

def test_one_persons_keep_both_does_not_hide_a_group_from_another(state_db):
    store.dismiss_duplicate("group-1", "keep both", decided_by="u-alex")

    assert store.dismissed_duplicates("u-alex") == {"group-1"}
    assert store.dismissed_duplicates("u-kelly") == set()


def test_decisions_from_before_anyone_was_recorded_apply_to_everyone(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE duplicate_dismissed (group_key TEXT PRIMARY KEY,"
                " note TEXT, decided_at TEXT NOT NULL)")
    old.execute("INSERT INTO duplicate_dismissed VALUES ('legacy', 'n', 'then')")
    old.commit()
    old.close()

    store.connect(path)
    try:
        assert store.dismissed_duplicates("u-kelly") == {"legacy"}
        store.dismiss_duplicate("legacy", "mine too", decided_by="u-alex")
        assert store.dismissed_duplicates("u-alex") == {"legacy"}
    finally:
        store._conn.close()
        store._conn = None


# --- a write that fails part way (2M20) ------------------------------------------

def test_a_write_that_fails_part_way_leaves_nothing_for_the_next_commit(tmp_path):
    """No writer rolled back. A statement failing part way left the shared
    connection inside a transaction, and the next unrelated commit - marking
    an album reviewed - wrote the half-done change to disk."""
    import pytest

    path = tmp_path / "state.db"
    store.connect(path)
    try:
        with pytest.raises(sqlite3.Error):
            with store.transaction() as db:
                db.execute("INSERT INTO play_collection (id, began) VALUES (1, 'x')")
                db.execute("INSERT INTO no_such_table VALUES (1)")

        assert not store.connection().in_transaction
        store.mark_reviewed(1, {"album"}, "by hand")

        other = sqlite3.connect(path)
        try:
            assert other.execute("SELECT COUNT(*) FROM play_collection").fetchone() == (0,)
            assert other.execute("SELECT COUNT(*) FROM album_reviewed").fetchone() == (1,)
        finally:
            other.close()
    finally:
        _close()


def test_a_reader_waits_for_a_write_in_flight(tmp_path):
    """Readers took no lock, so a reader in another thread was served rows a
    transaction had not committed and might yet roll back."""
    import threading

    store.connect(tmp_path / "state.db")
    seen = []
    try:
        with store.transaction() as db:
            db.execute("INSERT INTO play_collection (id, began) VALUES (1, 'x')")
            reader = threading.Thread(target=lambda: seen.append(
                store.connection().execute(
                    "SELECT COUNT(*) FROM play_collection").fetchone()[0]))
            reader.start()
            reader.join(0.3)
            assert seen == []          # still waiting for the lock
            db.rollback()
        reader.join(2)
        assert seen == [0]
    finally:
        _close()


# --- a set-aside file put back by hand (2L13) ----------------------------------

def test_a_file_put_back_by_hand_is_seen_again(tmp_path):
    """Nothing wrote restored_at, so a file moved back out of the quarantine
    stayed hidden from the duplicate finder for good."""
    from types import SimpleNamespace

    store.connect(tmp_path / "state.db")
    try:
        copy = SimpleNamespace(id="t1", library_id=1, title="T", artist="A", album="B")
        source, target = tmp_path / "A" / "01.mp3", tmp_path / "aside" / "01.mp3"
        for track_id in ("t1", "t2"):
            store.record_quarantine("g", SimpleNamespace(**{**copy.__dict__, "id": track_id}),
                                    None, str(source), str(target), "alex")
        target.parent.mkdir()
        target.write_bytes(b"audio")
        assert store.quarantined_track_ids() == {"t1", "t2"}

        source.parent.mkdir()
        target.rename(source)                  # put back by hand

        assert store.quarantined_track_ids() == set()
        assert all(row["restored_at"] for row in store.quarantined(include_restored=True))
    finally:
        _close()
