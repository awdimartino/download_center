"""Bounds on the in-memory job queue.

Nothing bounded any of this. Jobs were held for the life of the process and
re-serialised on every GET /api/jobs; the download semaphore was created per
job, so N jobs meant N times the concurrency; and a link to an enormous
playlist became that many dicts going over the websocket twice a second.
"""

from __future__ import annotations

import asyncio

import pytest

from app import worker
from app.config import settings


@pytest.fixture(autouse=True)
def clean_jobs():
    jobs.JOBS.clear()
    jobs.RUNNING.clear()
    yield
    jobs.JOBS.clear()
    jobs.RUNNING.clear()


def _job(job_id, owner="alex", status="complete", created="2026-01-01T00:00:00"):
    jobs.JOBS[job_id] = {
        "id": job_id, "owner": owner, "status": status,
        "created_at": created, "items": [],
    }
    return jobs.JOBS[job_id]


# --- keeping the queue from growing forever -------------------------------

def test_finished_jobs_are_evicted_oldest_first():
    for n in range(jobs.MAX_FINISHED_JOBS + 10):
        _job(f"j{n:03d}", created=f"2026-01-01T00:{n:02d}:00")

    jobs._evict_old_jobs("alex")

    assert len(jobs.JOBS) == jobs.MAX_FINISHED_JOBS
    assert "j000" not in jobs.JOBS, "oldest should go first"
    assert f"j{jobs.MAX_FINISHED_JOBS + 9:03d}" in jobs.JOBS


def test_eviction_never_touches_a_running_job():
    for n in range(jobs.MAX_FINISHED_JOBS + 5):
        _job(f"j{n:03d}", created=f"2026-01-01T00:{n:02d}:00")
    _job("live", status="running", created="2026-01-01T00:00:00")

    jobs._evict_old_jobs("alex")
    assert "live" in jobs.JOBS


def test_eviction_never_touches_a_job_with_a_task_still_tracked():
    """A cancelled job whose task has not finished unwinding is still
    entitled to its scratch directory."""
    for n in range(jobs.MAX_FINISHED_JOBS + 5):
        _job(f"j{n:03d}", created=f"2026-01-01T00:{n:02d}:00")
    _job("unwinding", status="cancelled", created="2026-01-01T00:00:00")
    jobs.RUNNING["unwinding"] = object()

    jobs._evict_old_jobs("alex")
    assert "unwinding" in jobs.JOBS


def test_eviction_only_touches_the_named_person():
    for n in range(jobs.MAX_FINISHED_JOBS + 5):
        _job(f"j{n:03d}", created=f"2026-01-01T00:{n:02d}:00")
    _job("hers", owner="kelly", created="2026-01-01T00:00:00")

    jobs._evict_old_jobs("alex")
    assert "hers" in jobs.JOBS


def test_nothing_is_evicted_below_the_limit():
    for n in range(3):
        _job(f"j{n}")
    jobs._evict_old_jobs("alex")
    assert len(jobs.JOBS) == 3


# --- the download gate ------------------------------------------------------

def test_the_download_gate_is_shared_across_jobs(monkeypatch):
    """It used to be created inside run_job, so three queued playlists ran
    three times the configured concurrency - each spawning yt-dlp and
    ffmpeg, on a Raspberry Pi."""
    monkeypatch.setattr(worker, "_gate", None)

    async def check():
        return worker.gate() is worker.gate()

    assert asyncio.run(check()) is True


def test_changing_the_limit_mid_job_never_runs_past_it(monkeypatch):
    """Changing the setting used to build a second semaphore: jobs already
    running kept the old one, and old and new together ran past either
    limit (L9)."""
    monkeypatch.setattr(worker, "_gate", None)
    monkeypatch.setattr(settings, "concurrency", 3)

    current = peak = later_peak = 0
    later = 0

    async def body(hold, is_later=False):
        nonlocal current, peak, later, later_peak
        async with worker.gate():
            current += 1
            peak = max(peak, current)
            if is_later:
                later += 1
                later_peak = max(later_peak, later)
            await hold
            if is_later:
                later -= 1
            current -= 1

    async def run():
        release = asyncio.get_running_loop().create_future()
        first = [asyncio.create_task(body(release)) for _ in range(3)]
        await asyncio.sleep(0.01)
        monkeypatch.setattr(settings, "concurrency", 1)
        second = [asyncio.create_task(body(asyncio.sleep(0.01), True))
                  for _ in range(3)]
        await asyncio.sleep(0.01)
        assert current == 3, "a download started past the lowered limit"
        release.set_result(None)
        await asyncio.gather(*first, *second)

    asyncio.run(run())
    assert peak == 3
    assert later_peak == 1


def test_raising_the_limit_lets_more_in(monkeypatch):
    monkeypatch.setattr(worker, "_gate", None)
    monkeypatch.setattr(settings, "concurrency", 1)
    peak = current = 0

    async def body():
        nonlocal peak, current
        async with worker.gate():
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.01)
            current -= 1

    async def run():
        monkeypatch.setattr(settings, "concurrency", 4)
        await asyncio.gather(*(body() for _ in range(8)))

    asyncio.run(run())
    assert peak == 4


def test_the_gate_actually_limits(monkeypatch):
    monkeypatch.setattr(worker, "_gate", None)
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
    assert 0 < jobs.MAX_TRACKS_PER_JOB <= 2000


def test_the_active_job_cap_is_a_real_number():
    assert 0 < jobs.MAX_ACTIVE_JOBS <= 20


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

    jobs.RUNNING["j1"] = asyncio.create_task(work())
    await asyncio.sleep(0)

    await jobs._stop_job("j1")

    assert stopped.is_set(), "the task should have seen the cancellation"
    assert "j1" not in jobs.RUNNING


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

    jobs.RUNNING["j1"] = asyncio.create_task(work())
    await asyncio.sleep(0)

    await jobs._stop_job("j1")
    order.append("stop returned")

    assert order == ["task cleaned up", "stop returned"]


@pytest.mark.asyncio
async def test_stopping_an_unknown_or_finished_job_is_harmless():
    await jobs._stop_job("never-existed")

    async def done():
        return None

    task = asyncio.create_task(done())
    await task
    jobs.RUNNING["j1"] = task
    await jobs._stop_job("j1")
    assert "j1" not in jobs.RUNNING


@pytest.mark.asyncio
async def test_a_task_that_will_not_stop_does_not_hang_the_delete(monkeypatch):
    """Bounded, so one wedged task cannot make Delete unresponsive too."""
    monkeypatch.setattr(jobs, "STOP_TIMEOUT", 0.05)

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
    jobs.RUNNING["j1"] = task
    await asyncio.sleep(0)

    # Returns despite the task still running: the timeout is the bound.
    await asyncio.wait_for(jobs._stop_job("j1"), timeout=2)
    assert not task.done(), "the point of this test is that it did not stop"

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_cancelled_download_gives_its_gate_slot_back(monkeypatch):
    """The reason an orphaned task was fatal rather than merely untidy. The
    gate is process-wide, so a task that never releases starves every later
    job of every user - a queue stuck at "running" with nothing in the log."""
    monkeypatch.setattr(worker, "_gate", None)
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
    async def next_one():
        async with worker.gate():
            pass

    await asyncio.wait_for(next_one(), timeout=1)


# --- the active-job cap holds (L8) ------------------------------------------

from types import SimpleNamespace

from fastapi import HTTPException
from app import beets_runner
from app import jobs
from app import workspace
from app.api import jobs as jobs_routes
from app import events

SESSION = SimpleNamespace(identity=SimpleNamespace(username="alex"))
LINK = "https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj"


@pytest.fixture
def queueing(monkeypatch):
    import time

    space = SimpleNamespace(username="alex", library_name="Music", library_id=1,
                            prepared=0)

    def prepare():
        # Slow enough that two requests are both inside it at once.
        time.sleep(0.05)
        space.prepared += 1

    space.prepare = prepare
    monkeypatch.setattr(workspace, "for_session", lambda identity, lid: space)

    async def resolved(job, url, space):
        return None

    async def ran(job, space):
        return None

    monkeypatch.setattr(jobs, "_resolve_job", resolved)
    monkeypatch.setattr(jobs, "_run", ran)
    monkeypatch.setattr(events, "push_job", lambda job: asyncio.sleep(0))
    return space


@pytest.mark.asyncio
async def test_two_requests_at_once_cannot_both_take_the_last_slot(queueing):
    for n in range(jobs.MAX_ACTIVE_JOBS - 1):
        _job(f"busy{n}", status="running")

    outcomes = await asyncio.gather(
        jobs_routes.create_job(jobs_routes.JobRequest(url=LINK), SESSION),
        jobs_routes.create_job(jobs_routes.JobRequest(url=LINK), SESSION),
        return_exceptions=True)

    refused = [o for o in outcomes if isinstance(o, HTTPException)]
    assert len(refused) == 1 and refused[0].status_code == 429
    active = [j for j in jobs.JOBS.values() if j["status"] not in jobs.FINISHED]
    assert len(active) == jobs.MAX_ACTIVE_JOBS


@pytest.mark.asyncio
async def test_queueing_a_download_writes_no_beets_config(queueing,
                                                          monkeypatch):
    """A download never touches beets, and wrote its config anyway - even
    with beets switched off (R5)."""
    def refuse(space):
        raise AssertionError("queueing a download wrote a beets config")

    monkeypatch.setattr(beets_runner, "ensure_config", refuse)
    await jobs_routes.create_job(jobs_routes.JobRequest(url=LINK), SESSION)
    assert queueing.prepared == 1


def _failed_job():
    job = _job("old", status="failed")
    job["library_id"] = 1
    job["items"] = [{"id": "i1", "status": "failed", "error": "no audio",
                     "progress": 0, "attempts": 3}]
    return job


@pytest.mark.asyncio
async def test_retry_counts_against_the_cap(queueing):
    job = _failed_job()
    for n in range(jobs.MAX_ACTIVE_JOBS):
        _job(f"busy{n}", status="running")

    with pytest.raises(HTTPException) as refused:
        await jobs_routes.retry_job("old", SESSION)

    assert refused.value.status_code == 429
    assert job["status"] == "failed"
    assert job["items"][0]["status"] == "failed"
    assert job["items"][0]["error"] == "no audio"


@pytest.mark.asyncio
async def test_a_retry_that_cannot_start_leaves_the_failures_alone(queueing,
                                                                   monkeypatch):
    def gone(identity, lid):
        raise ValueError("That library is no longer yours.")

    monkeypatch.setattr(workspace, "for_session", gone)
    job = _failed_job()

    with pytest.raises(HTTPException):
        await jobs_routes.retry_job("old", SESSION)

    assert job["items"][0]["status"] == "failed"
    assert job["items"][0]["error"] == "no audio"


@pytest.mark.asyncio
async def test_a_retry_with_room_starts(queueing):
    job = _failed_job()

    assert await jobs_routes.retry_job("old", SESSION) == {"retrying": 1}
    assert job["status"] == "queued"
    assert job["items"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_two_retries_at_once_start_one_runner(queueing, monkeypatch):
    """Both passed the "still running" check while the first was off the
    loop finding the workspace, and two runners drove one job: every failed
    track downloaded and filed twice (2L1)."""
    import time

    runs = []

    async def ran(job, space):
        runs.append(job["id"])
        await asyncio.sleep(0.2)

    def slow_workspace(identity, lid):
        time.sleep(0.05)
        return queueing

    monkeypatch.setattr(jobs, "_run", ran)
    monkeypatch.setattr(workspace, "for_session", slow_workspace)
    _failed_job()

    outcomes = await asyncio.gather(jobs_routes.retry_job("old", SESSION),
                                    jobs_routes.retry_job("old", SESSION),
                                    return_exceptions=True)
    await asyncio.sleep(0.05)

    refused = [o for o in outcomes if isinstance(o, HTTPException)]
    assert len(refused) == 1 and refused[0].status_code == 409
    assert runs == ["old"]


# --- cancel, not delete (L13) --------------------------------------------------

@pytest.mark.asyncio
async def test_a_retried_job_can_be_cancelled_straight_away(queueing):
    """The page's ✕ on an active job now cancels it. A retried job was only
    tracked once its task first ran, so a cancel pressed at once was told
    the job was not running."""
    _failed_job()
    await jobs_routes.retry_job("old", SESSION)

    assert await jobs_routes.cancel_job("old", SESSION) == {"ok": True}


def test_the_page_cancels_an_active_job_rather_than_deleting_it():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "app" / "static" / "js"
          / "downloads.js").read_text(encoding="utf-8")
    assert "/cancel`, { method: \"POST\" }" in js
    assert 'remove.dataset.action === "cancel"' in js


# --- a cancel wherever it lands while resolving (2L2) ----------------------------

@pytest.mark.asyncio
async def test_a_cancel_while_announcing_the_resolve_leaves_no_ghost(monkeypatch):
    """The handler covered only the resolve itself. A cancel landing while
    the result was being announced left the job "queued", with a dead task
    still listed as running."""
    job = _job("j-resolving", status="resolving")
    pushed = asyncio.Event()

    async def slow_push(job):
        if job["status"] == "queued":
            pushed.set()
            await asyncio.sleep(5)

    monkeypatch.setattr(events, "push_job", slow_push)
    monkeypatch.setattr(jobs, "_resolve",
                        lambda url: ("album", "Abbey Road", [{"id": "s1", "title": "T"}]))
    monkeypatch.setattr(jobs, "new_item", lambda track: {"id": track["id"], "status": "pending"})
    task = asyncio.create_task(jobs._resolve_job(job, "https://x", None))
    jobs.RUNNING[job["id"]] = task

    await asyncio.wait_for(pushed.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert job["status"] == "cancelled"
    assert job["id"] not in jobs.RUNNING


@pytest.mark.asyncio
async def test_too_many_tracks_is_refused_and_not_left_running(monkeypatch):
    job = _job("j-big", status="resolving")
    monkeypatch.setattr(events, "push_job", lambda job: asyncio.sleep(0))
    monkeypatch.setattr(jobs, "_resolve", lambda url: (
        "playlist", "Huge", [{"id": str(n)} for n in range(jobs.MAX_TRACKS_PER_JOB + 1)]))
    jobs.RUNNING[job["id"]] = asyncio.current_task()

    await jobs._resolve_job(job, "https://x", None)

    assert job["status"] == "failed"
    assert job["id"] not in jobs.RUNNING


@pytest.mark.asyncio
async def test_a_workspace_that_cannot_be_made_is_a_clear_refusal(queueing, monkeypatch):
    """Two usernames reducing to one folder name made the second person's
    prepare refuse, and that reached them as a bare 500 (2L5)."""
    def clash():
        raise ValueError("That folder already belongs to another account.")

    monkeypatch.setattr(queueing, "prepare", clash)
    with pytest.raises(HTTPException) as refused:
        await jobs_routes.create_job(jobs_routes.JobRequest(url=LINK), SESSION)
    assert refused.value.status_code == 409
