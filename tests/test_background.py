"""The background loops survive their own failures (CODE_REVIEW M28).

The disk-audit loop caught only a failed audit; a sqlite3.Error from reading
the library list ended the task for the life of the process, and Health's
audit aged for ever.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app import main


@pytest.mark.asyncio
async def test_the_audit_loop_survives_an_unexpected_error(monkeypatch):
    calls = []

    def roots():
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return []

    async def sleep(seconds):
        if len(calls) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(main, "_library_roots", roots)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await main._audit_loop()

    assert len(calls) == 2


# --- a drop asks Navidrome to scan (L7) ----------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("changed, scans", [(True, 1), (False, 0)])
async def test_the_inbox_loop_asks_for_a_scan_only_when_it_filed_something(
        monkeypatch, changed, scans):
    from app import inbox

    asked = []
    # drain_all reports a workspace that filed something or failed to.
    result = inbox.Result(filed=[object()] if changed else [],
                          failures=[] if changed else ["x.mp3: unreadable"])
    monkeypatch.setattr(inbox, "drain_all", lambda: {"alex": result})
    monkeypatch.setattr(main.navidrome, "notify", lambda *a, **k: asked.append(1))

    async def sleep(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await main._inbox_loop()

    assert len(asked) == scans


@pytest.mark.asyncio
async def test_finishing_an_upload_asks_for_a_scan(monkeypatch, tmp_path):
    from app import inbox, workspace

    space = workspace.Workspace(username="alex", library_id=1,
                                library_name="Music", library_path=tmp_path)
    monkeypatch.setattr(workspace, "for_session", lambda identity, lid: space)
    monkeypatch.setattr(inbox, "drain",
                        lambda space: inbox.Result(filed=[tmp_path / "a.mp3"]))
    asked = []
    monkeypatch.setattr(main.navidrome, "notify", lambda *a, **k: asked.append(1))

    class Session:
        identity = None

    out = await main.finish_upload(library_id=None, batch=None, session=Session())

    assert out["filed"] == ["a.mp3"]
    assert asked == [1]


# --- a forced reading warms Home like a timed one (L30) ------------------------

@pytest.mark.asyncio
async def test_a_forced_snapshot_warms_home_for_whoever_played(monkeypatch):
    warmed = []
    monkeypatch.setattr(main.playcounts, "take",
                        lambda: {"users": ["u-alex"], "rows": 3})
    monkeypatch.setattr(main.overview, "warm", lambda users: warmed.append(users))

    taken = await main.playcount_snapshot(session=None)

    assert taken["rows"] == 3
    assert warmed == [["u-alex"]]


@pytest.mark.asyncio
async def test_a_forced_snapshot_with_nothing_new_warms_nothing(monkeypatch):
    warmed = []
    monkeypatch.setattr(main.playcounts, "take", lambda: {"users": []})
    monkeypatch.setattr(main.overview, "warm", lambda users: warmed.append(users))

    await main.playcount_snapshot(session=None)

    assert warmed == []
