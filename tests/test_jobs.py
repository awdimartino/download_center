"""Bounds on the in-memory job queue.

Nothing bounded any of this. Jobs were held for the life of the process and
re-serialised on every GET /api/jobs; the download semaphore was created per
job, so N jobs meant N times the concurrency; and a link to an enormous
playlist became that many dicts going over the websocket twice a second.
"""

from __future__ import annotations

import asyncio

import pytest

from app import main, worker
from app.config import settings


@pytest.fixture(autouse=True)
def clean_jobs():
    main.JOBS.clear()
    main.RUNNING.clear()
    yield
    main.JOBS.clear()
    main.RUNNING.clear()


def _job(job_id, owner="alex", status="complete", created="2026-01-01T00:00:00"):
    main.JOBS[job_id] = {
        "id": job_id, "owner": owner, "status": status,
        "created_at": created, "items": [],
    }
    return main.JOBS[job_id]


# --- keeping the queue from growing forever -------------------------------

def test_finished_jobs_are_evicted_oldest_first():
    for n in range(main.MAX_FINISHED_JOBS + 10):
        _job(f"j{n:03d}", created=f"2026-01-01T00:{n:02d}:00")

    main._evict_old_jobs("alex")

    assert len(main.JOBS) == main.MAX_FINISHED_JOBS
    assert "j000" not in main.JOBS, "oldest should go first"
    assert f"j{main.MAX_FINISHED_JOBS + 9:03d}" in main.JOBS


def test_eviction_never_touches_a_running_job():
    for n in range(main.MAX_FINISHED_JOBS + 5):
        _job(f"j{n:03d}", created=f"2026-01-01T00:{n:02d}:00")
    _job("live", status="running", created="2026-01-01T00:00:00")

    main._evict_old_jobs("alex")
    assert "live" in main.JOBS


def test_eviction_never_touches_a_job_with_a_task_still_tracked():
    """A cancelled job whose task has not finished unwinding is still
    entitled to its scratch directory."""
    for n in range(main.MAX_FINISHED_JOBS + 5):
        _job(f"j{n:03d}", created=f"2026-01-01T00:{n:02d}:00")
    _job("unwinding", status="cancelled", created="2026-01-01T00:00:00")
    main.RUNNING["unwinding"] = object()

    main._evict_old_jobs("alex")
    assert "unwinding" in main.JOBS


def test_eviction_only_touches_the_named_person():
    for n in range(main.MAX_FINISHED_JOBS + 5):
        _job(f"j{n:03d}", created=f"2026-01-01T00:{n:02d}:00")
    _job("hers", owner="kelly", created="2026-01-01T00:00:00")

    main._evict_old_jobs("alex")
    assert "hers" in main.JOBS


def test_nothing_is_evicted_below_the_limit():
    for n in range(3):
        _job(f"j{n}")
    main._evict_old_jobs("alex")
    assert len(main.JOBS) == 3


# --- the download gate ------------------------------------------------------

def test_the_download_gate_is_shared_across_jobs(monkeypatch):
    """It used to be created inside run_job, so three queued playlists ran
    three times the configured concurrency - each spawning yt-dlp and
    ffmpeg, on a Raspberry Pi."""
    monkeypatch.setattr(worker, "_gate", None)
    monkeypatch.setattr(worker, "_gate_size", 0)

    async def check():
        return worker.gate() is worker.gate()

    assert asyncio.run(check()) is True


def test_the_gate_is_rebuilt_when_the_setting_changes(monkeypatch):
    monkeypatch.setattr(worker, "_gate", None)
    monkeypatch.setattr(worker, "_gate_size", 0)

    async def check():
        monkeypatch.setattr(settings, "concurrency", 2)
        first = worker.gate()
        monkeypatch.setattr(settings, "concurrency", 5)
        second = worker.gate()
        return first, second

    first, second = asyncio.run(check())
    assert first is not second
    assert second._value == 5


def test_the_gate_actually_limits(monkeypatch):
    monkeypatch.setattr(worker, "_gate", None)
    monkeypatch.setattr(worker, "_gate_size", 0)
    monkeypatch.setattr(settings, "concurrency", 2)

    peak = 0
    current = 0

    async def body():
        nonlocal peak, current
        async with worker.gate():
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.01)
            current -= 1

    async def run():
        await asyncio.gather(*(body() for _ in range(10)))

    asyncio.run(run())
    assert peak == 2, f"ran {peak} at once with concurrency 2"


# --- refusing an enormous playlist ------------------------------------------

def test_the_track_cap_is_a_real_number():
    assert 0 < main.MAX_TRACKS_PER_JOB <= 2000


def test_the_active_job_cap_is_a_real_number():
    assert 0 < main.MAX_ACTIVE_JOBS <= 20


# --- stopping a job before deleting its files -----------------------------

@pytest.mark.asyncio
async def test_stopping_a_job_cancels_it_and_waits():
    """Deleting used to pop the job and rmtree its scratch directory while
    the downloads were still running. yt-dlp recreated the directory
    underneath itself, every rename failed with ENOENT, and the worker
    retried the whole way through its backoff - for ever, because nothing
    had told it to stop."""
    stopped = asyncio.Event()

    async def work():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            stopped.set()
            raise

    main.RUNNING["j1"] = asyncio.create_task(work())
    await asyncio.sleep(0)

    await main._stop_job("j1")

    assert stopped.is_set(), "the task should have seen the cancellation"
    assert "j1" not in main.RUNNING


@pytest.mark.asyncio
async def test_stopping_waits_for_cleanup_before_returning():
    """The point of awaiting: the caller deletes files the task is using, so
    it must not return while the task is still touching them."""
    order = []

    async def work():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Stand-in for staging.discard in the worker's own handler.
            await asyncio.sleep(0.05)
            order.append("task cleaned up")
            raise

    main.RUNNING["j1"] = asyncio.create_task(work())
    await asyncio.sleep(0)

    await main._stop_job("j1")
    order.append("stop returned")

    assert order == ["task cleaned up", "stop returned"]


@pytest.mark.asyncio
async def test_stopping_an_unknown_or_finished_job_is_harmless():
    await main._stop_job("never-existed")

    async def done():
        return None

    task = asyncio.create_task(done())
    await task
    main.RUNNING["j1"] = task
    await main._stop_job("j1")
    assert "j1" not in main.RUNNING


@pytest.mark.asyncio
async def test_a_task_that_will_not_stop_does_not_hang_the_delete(monkeypatch):
    """Bounded, so one wedged task cannot make Delete unresponsive too."""
    monkeypatch.setattr(main, "STOP_TIMEOUT", 0.05)

    async def stubborn():
        # Swallows the first cancellation and honours the second. Ignoring
        # every cancellation would leave a task the event loop can never
        # reap, which hangs the test run itself rather than testing anything.
        ignored = False
        while True:
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                if ignored:
                    raise
                ignored = True

    task = asyncio.create_task(stubborn())
    main.RUNNING["j1"] = task
    await asyncio.sleep(0)

    # Returns despite the task still running: the timeout is the bound.
    await asyncio.wait_for(main._stop_job("j1"), timeout=2)
    assert not task.done(), "the point of this test is that it did not stop"

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_cancelled_download_gives_its_gate_slot_back(monkeypatch):
    """The reason an orphaned task was fatal rather than merely untidy. The
    gate is process-wide, so a task that never releases starves every later
    job of every user - a queue stuck at "running" with nothing in the log."""
    monkeypatch.setattr(worker, "_gate", None)
    monkeypatch.setattr(worker, "_gate_size", 0)
    monkeypatch.setattr(settings, "concurrency", 1)

    holding = asyncio.Event()

    async def hog():
        async with worker.gate():
            holding.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(hog())
    await asyncio.wait_for(holding.wait(), timeout=1)
    assert worker.gate().locked(), "the only slot should be taken"

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # Free again, immediately: the next job is not queued behind a ghost.
    await asyncio.wait_for(worker.gate().acquire(), timeout=1)
    worker.gate().release()
