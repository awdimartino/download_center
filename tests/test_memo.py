"""Cached statistics, and the one way they can go wrong: answering from a
history or a track list that has since moved on.

Every test here changes the data underneath a result that has already been
computed and asks again. A cache that only ever passes the first question is
indistinguishable from no cache until somebody notices last week's numbers.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from app import memo, overview, playcounts, store
from conftest import add_track

ALEX = "u-alex"


@pytest.fixture
def identity_with_db(navidrome_db, monkeypatch, identity):
    from app.config import settings
    monkeypatch.setattr(settings, "navidrome_db", navidrome_db)
    return identity


def tagged(uuid: str) -> str:
    return json.dumps({"navidrome_uuid": [{"value": uuid}]})


def snapshot(day, track_uuid, count):
    store.connection().execute(
        "insert or replace into play_snapshot"
        " (taken_on, track_uuid, user_id, username, play_count, play_date)"
        " values (?, ?, ?, 'alex', ?, null)", (day, track_uuid, ALEX, count))
    store.connection().commit()


def this_month() -> str:
    # UTC, as the month buckets are cut.
    now = datetime.now(UTC)
    return f"{now.year:04d}-{now.month:02d}"


# --- the helper itself ------------------------------------------------------

def test_the_same_version_is_answered_without_recomputing():
    memo.clear()
    calls = []
    compute = lambda: calls.append(1) or len(calls)  # noqa: E731

    assert memo.cached("k", 1, compute) == 1
    assert memo.cached("k", 1, compute) == 1
    assert memo.cached("k", 2, compute) == 2, "a new version recomputes"
    assert len(calls) == 2


def test_the_oldest_entry_goes_once_the_limit_is_reached(monkeypatch):
    memo.clear()
    monkeypatch.setattr(memo, "LIMIT", 2)
    for key in "abc":
        memo.cached(key, 0, lambda key=key: key)
    calls = []
    memo.cached("a", 0, lambda: calls.append(1))
    assert calls == [1], "'a' was evicted, so it is computed again"


# --- the history ------------------------------------------------------------

def test_a_new_play_reaches_a_cached_home_page(state_db, identity_with_db,
                                               navidrome_db):
    month = this_month()
    add_track(navidrome_db, "m1", artist="Burial", tags=tagged("t1"))
    snapshot(f"{month}-01", "t1", 0)
    snapshot(f"{month}-01T10:00:00+00:00", "t1", 1)
    assert overview.listening(identity_with_db)["this_month"] == 1

    snapshot(f"{month}-01T11:00:00+00:00", "t1", 3)

    assert overview.listening(identity_with_db)["this_month"] == 3


def test_a_rewritten_reading_moves_the_version(state_db):
    """INSERT OR REPLACE on the same key keeps the count the same; the
    version still has to move, because the value under it did."""
    snapshot("2026-09-01", "t1", 1)
    before = playcounts.history_version()
    snapshot("2026-09-01", "t1", 2)
    assert playcounts.history_version() != before


def test_undoing_the_import_by_hand_moves_the_version(state_db):
    """The documented undo is a DELETE in the sqlite shell, which no writer
    in this application gets to hear about."""
    store.connection().execute(
        "insert into play_imported (played_at, track_uuid, user_id, username,"
        " plays, source) values ('2025-01-01', 't1', ?, 'alex', 4, 'lastfm')",
        (ALEX,))
    store.connection().commit()
    before = playcounts.history_version()
    store.connection().execute(
        "delete from play_imported where source = 'lastfm'")
    store.connection().commit()
    assert playcounts.history_version() != before


def test_a_different_database_is_never_mistaken_for_the_last(tmp_path):
    """Two empty databases have identical row counts; the generation is
    what tells them apart."""
    store.connect(tmp_path / "one.db")
    first = playcounts.history_version()
    store.connect(tmp_path / "two.db")
    try:
        assert playcounts.history_version() != first
    finally:
        store._conn.close()
        store._conn = None


# --- the track index --------------------------------------------------------

def test_a_retag_reaches_a_cached_home_page(state_db, identity_with_db,
                                            navidrome_db):
    month = this_month()
    add_track(navidrome_db, "m1", artist="Burial", tags=tagged("t1"))
    snapshot(f"{month}-01", "t1", 0)
    snapshot(f"{month}-02", "t1", 2)
    assert overview.listening(identity_with_db)["top_artists"][0]["artist"] == "Burial"

    connection = sqlite3.connect(navidrome_db)
    with connection:
        connection.execute("update media_file set artist = 'Kode9' where id = 'm1'")
    connection.close()

    assert overview.listening(identity_with_db)["top_artists"][0]["artist"] == "Kode9"


def test_navidrome_writing_something_else_keeps_the_version(
        state_db, identity_with_db, navidrome_db):
    """Navidrome writes to its database on every play. Unless the tracks
    themselves changed, every cached statistic should survive that."""
    add_track(navidrome_db, "m1", tags=tagged("t1"))
    version, tracks = playcounts.track_index()
    assert "t1" in tracks

    connection = sqlite3.connect(navidrome_db)
    with connection:
        connection.execute(
            "insert into annotation (user_id, item_id, item_type)"
            " values ('u-alex', 'm1', 'media_file')")
    connection.close()

    assert playcounts.track_index()[0] == version


def test_a_uuid_left_on_a_missing_copy_names_the_live_one(
        state_db, identity_with_db, navidrome_db):
    add_track(navidrome_db, "live", title="Here", tags=tagged("t1"))
    add_track(navidrome_db, "old", title="Gone", tags=tagged("t1"), missing=1)
    assert playcounts._titles(["t1"])["t1"]["title"] == "Here"


def test_shared_rows_are_not_changed_by_the_track_list(
        state_db, identity_with_db, navidrome_db):
    """top_tracks names its rows; the albums and genres asked of the same
    cached range must still see them as they were read."""
    add_track(navidrome_db, "m1", tags=tagged("t1"))
    snapshot("2026-03-01", "t1", 0)
    snapshot("2026-03-02", "t1", 2)

    playcounts.top_tracks("2026-03-02", "2026-03-02", ALEX)

    assert "title" not in playcounts.plays_between(
        "2026-03-02", "2026-03-02", ALEX)[0]


# --- warming ----------------------------------------------------------------

def test_warming_computes_what_home_opens_with(state_db, identity_with_db,
                                               navidrome_db, monkeypatch):
    """Keyed exactly as the request will be - a warm-up that filled some
    other entry would cost the Pi the work and save the visitor nothing."""
    add_track(navidrome_db, "m1", tags=tagged("t1"))
    snapshot("2026-03-01", "t1", 0)
    snapshot("2026-03-02", "t1", 2)

    overview.warm()

    monkeypatch.setattr(playcounts, "top_tracks",
                        lambda *a, **k: pytest.fail("recomputed"))
    end = playcounts.today()
    overview.window(ALEX, overview.days_back(end, overview.OPENING_DAYS), end,
                    overview.OPENING_DAYS, overview.OPENING_LIMIT)


# --- compression ------------------------------------------------------------

def _serve(path: str, body: bytes, kind: str) -> tuple[dict, bytes]:
    from app.main import TextGZip

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", kind.encode())]})
        await send({"type": "http.response.body", "body": body})

    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": path, "method": "GET",
             "headers": [(b"accept-encoding", b"gzip")]}
    asyncio.run(TextGZip(inner)(scope, receive, send))
    headers = {k.decode(): v.decode() for k, v in sent[0]["headers"]}
    return headers, b"".join(m.get("body", b"") for m in sent[1:])


def test_json_is_compressed():
    body = json.dumps({"tracks": ["a song"] * 500}).encode()
    headers, sent = _serve("/api/overview", body, "application/json")
    assert headers.get("content-encoding") == "gzip"
    assert gzip.decompress(sent) == body


def test_cover_art_is_left_alone():
    body = b"\xff\xd8" + b"\x00" * 5000
    headers, sent = _serve("/api/library/art", body, "image/jpeg")
    assert "content-encoding" not in headers
    assert sent == body
