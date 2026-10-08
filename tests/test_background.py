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
from app import background
from app import inbox
from app import jobs
from app import navidrome
from app import overview
from app import playcounts
from app import store
from app.api import inbox as inbox_routes
from app.api import listening as listening_routes


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

    monkeypatch.setattr(background, "_library_roots", roots)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await background._audit_loop()

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
    monkeypatch.setattr(navidrome, "notify", lambda *a, **k: asked.append(1))

    async def sleep(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await background._inbox_loop()

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
    monkeypatch.setattr(navidrome, "notify", lambda *a, **k: asked.append(1))

    class Session:
        identity = None

    out = await inbox_routes.finish_upload(library_id=None, batch=None, session=Session())

    assert out["filed"] == ["a.mp3"]
    assert asked == [1]


# --- a forced reading warms Home like a timed one (L30) ------------------------

@pytest.mark.asyncio
async def test_a_forced_snapshot_warms_home_for_whoever_played(monkeypatch):
    warmed = []
    monkeypatch.setattr(playcounts, "take",
                        lambda: {"users": ["u-alex"], "rows": 3})
    monkeypatch.setattr(overview, "warm", lambda users: warmed.append(users))

    taken = await listening_routes.playcount_snapshot(session=None)

    assert taken["rows"] == 3
    assert warmed == [["u-alex"]]


@pytest.mark.asyncio
async def test_a_forced_snapshot_with_nothing_new_warms_nothing(monkeypatch):
    warmed = []
    monkeypatch.setattr(playcounts, "take", lambda: {"users": []})
    monkeypatch.setattr(overview, "warm", lambda users: warmed.append(users))

    await listening_routes.playcount_snapshot(session=None)

    assert warmed == []


@pytest.mark.asyncio
async def test_shutting_down_stops_jobs_and_operations(monkeypatch):
    """A shutdown cancelled the three loops and nothing else: downloads and
    operations went on in their threads until Docker killed the process,
    possibly part way through rewriting a library file (2M3)."""
    import threading

    from app import operations

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(store, "connect", lambda path: None)
    monkeypatch.setattr(inbox, "clear_scratch", lambda: None)
    for loop in ("_audit_loop", "_inbox_loop", "_snapshot_loop"):
        monkeypatch.setattr(background, loop, idle)
    operations.reset()

    stopped = threading.Event()

    def work():
        while not operations.stopping("replaygain", "alex"):
            threading.Event().wait(0.01)
        stopped.set()
        return {}

    async with main.lifespan(main.app):
        job = asyncio.create_task(idle())
        monkeypatch.setitem(jobs.RUNNING, "job1", job)
        operation, _ = operations.start("replaygain", "alex", work)
        await asyncio.sleep(0.05)

    assert job.cancelled()
    assert stopped.is_set()
    assert operation.task is None and operation.status == operations.DONE


@pytest.mark.asyncio
async def test_a_second_worker_is_refused(monkeypatch):
    """Sessions, jobs, locks and the broker live in this process's memory.
    A second worker would have its own of each, and nothing stopped one
    being started (2D4)."""
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError, match="single worker"):
        async with main.lifespan(main.app):
            pass
