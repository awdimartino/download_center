"""What Library and Home read from Navidrome, and how often (CODE_REVIEW
M36, M37).

The track index was rebuilt whenever Navidrome's database file changed,
which it does on every play; and every Library request walked the whole of
media_file, even to open one album. Both are now keyed on what actually
changes. These run with `updated_at`, as a real Navidrome has it.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from app import library, memo, playcounts
from conftest import add_track


@pytest.fixture
def db(navidrome_db, state_db, monkeypatch):
    from app.config import settings

    connection = sqlite3.connect(navidrome_db)
    with connection:
        connection.execute("ALTER TABLE media_file ADD COLUMN updated_at TEXT")
        connection.execute(
            "ALTER TABLE annotation ADD COLUMN play_count INTEGER DEFAULT 0")
    connection.close()
    monkeypatch.setattr(settings, "navidrome_db", navidrome_db)
    monkeypatch.setattr(playcounts, "_index",
                        {"stamp": None, "version": 0, "tracks": {}})
    memo.clear()
    return navidrome_db


def _sql(db, statement, *args):
    connection = sqlite3.connect(db)
    with connection:
        connection.execute(statement, args)
    connection.close()


def _counting(monkeypatch, module, name):
    calls = []
    real = getattr(module, name)

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


def test_a_play_does_not_rebuild_the_track_index(db, monkeypatch):
    add_track(db, "t1", tags='{"navidrome_uuid": [{"value": "u1"}]}',
              updated_at="2026-10-01")
    reads = _counting(monkeypatch, playcounts, "_read_index")
    playcounts.track_index()
    time.sleep(0.05)   # past the filesystem's timestamp resolution

    _sql(db, "INSERT INTO annotation (user_id, item_id, item_type, play_count)"
             " VALUES ('u-alex', 't1', 'media_file', 1)")
    playcounts.track_index()
    assert len(reads) == 1, "a play rebuilt the index"

    _sql(db, "UPDATE media_file SET title = 'Retitled', updated_at = '2026-10-02'")
    _, tracks = playcounts.track_index()
    assert len(reads) == 2
    assert tracks["u1"]["title"] == "Retitled"


def test_the_album_list_is_read_once_until_something_changes(db, identity,
                                                              monkeypatch):
    add_track(db, "a", path="A/B/1.mp3", album="B", updated_at="2026-10-01")
    loads = _counting(monkeypatch, library, "_load")

    library.listing(identity)
    library.listing(identity)
    assert len(loads) == 1

    _sql(db, "INSERT INTO annotation (user_id, item_id, item_type, play_count)"
             " VALUES ('u-alex', 'a', 'media_file', 4)")
    listed = library.listing(identity, sort="plays")
    assert len(loads) == 2, "this person's plays are part of the list"
    assert listed["albums"][0]["plays"] == 4


def test_a_cached_album_is_not_changed_by_a_caller(db, identity):
    add_track(db, "a", path="A/B/1.mp3", album="B", updated_at="2026-10-01")

    first = library.listing(identity)["albums"][0]
    first["artist"] = "scribbled on"

    assert library.listing(identity)["albums"][0]["artist"] != "scribbled on"


def test_opening_an_album_reads_only_that_folder(db, identity):
    add_track(db, "in", path="A_1/B/1.mp3", updated_at="x")
    add_track(db, "lookalike", path="AX1/B/1.mp3", updated_at="x")
    add_track(db, "deeper", path="A_1/B/CD1/1.mp3", updated_at="x")
    add_track(db, "beside", path="A_1/B C/1.mp3", updated_at="x")

    opened = library.tracks(identity, 1, "A_1/B")

    assert [t["id"] for t in opened["items"]] == ["in"]


# --- Health (CODE_REVIEW M39) ------------------------------------------------
# Every request ran about eight full scans and the duplicate finder, polled
# every five minutes by every open tab.

def test_health_reads_navidrome_once_until_something_changes(db, identity,
                                                              monkeypatch):
    from app import health

    add_track(db, "a", path="A/B/1.mp3", updated_at="2026-10-01")
    reads = _counting(monkeypatch, health, "_from_navidrome")

    health.report(0, identity.libraries, identity)
    health.report(0, identity.libraries, identity)
    assert len(reads) == 1

    _sql(db, "INSERT INTO annotation (user_id, item_id, item_type, play_count)"
             " VALUES ('u-alex', 'a', 'media_file', 1)")
    health.report(0, identity.libraries, identity)
    assert len(reads) == 2

    clock = time.time() + health.CACHE_SECONDS
    monkeypatch.setattr(health.time, "time", lambda: clock)
    health.report(0, identity.libraries, identity)
    assert len(reads) == 3


def test_a_health_report_is_not_shared_with_the_next(db, identity):
    from app import health

    add_track(db, "a", path="A/B/1.mp3", updated_at="2026-10-01")
    health.report(0, identity.libraries, identity)
    second = health.report(0, identity.libraries, identity)

    # Rows are added to each report after the cached part; added to the
    # shared copy, they would pile up from one request to the next.
    rows = [len(s["checks"]) for s in second["sections"]]
    third = health.report(0, identity.libraries, identity)
    assert [len(s["checks"]) for s in third["sections"]] == rows
