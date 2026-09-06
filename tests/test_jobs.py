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
