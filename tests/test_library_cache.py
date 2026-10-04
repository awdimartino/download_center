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

from app import memo, playcounts
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
