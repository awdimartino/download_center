"""Download jobs: their state, resolving a link, running and stopping one.

Held in memory and not persisted - see JOBS."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from . import (
    auth,
    events,
    generic,
    inbox,
    netguard,
    spotify,
    threads,
    worker,
    workspace,
)

log = logging.getLogger("navidrome_companion")


JOBS: dict[str, dict[str, Any]] = {}


# The asyncio task driving each active job, so it can be cancelled.
RUNNING: dict[str, asyncio.Task] = {}


# How many finished jobs to keep per person. They are only history once they
# have stopped, and every one of them is re-serialised on each GET /api/jobs
# and held for the life of the process. A few hundred tracks each adds up.
MAX_FINISHED_JOBS = 40


# How many jobs one person may have in flight. Each resolves and downloads
# concurrently, and nothing else bounded this: a handful of pasted playlist
# links was an unbounded pile of tasks on a Raspberry Pi.
MAX_ACTIVE_JOBS = 5


FINISHED = ("complete", "failed", "partial", "cancelled")


# How long to wait for a cancelled job to unwind before deleting its files
# anyway. A download is asked to stop and gives up at its next chunk, and
# filing is waited for, since it moves the file whatever happens; this only
# bounds the pathological case.
STOP_TIMEOUT = 10


def _evict_old_jobs(owner: str) -> None:
    """Drop this person's oldest finished jobs once there are too many.

    Only finished ones, and never one still running or being cancelled.
    """
    theirs = [job for job in JOBS.values() if job.get("owner") == owner
              and job.get("status") in FINISHED and job["id"] not in RUNNING]
    if len(theirs) <= MAX_FINISHED_JOBS:
        return
    theirs.sort(key=lambda j: j["created_at"])
    for job in theirs[:len(theirs) - MAX_FINISHED_JOBS]:
        JOBS.pop(job["id"], None)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_job(url: str, space: workspace.Workspace) -> dict[str, Any]:
    job = {
        "id": uuid.uuid4().hex[:12],
        "source_url": url,
        # Whose download this is, and where it will end up. Recorded on the
        # job so every later phase - fetching, tagging, filing - agrees.
        "owner": space.username,
        "library": space.library_name,
        # The id, not just the name: retrying or discarding has to rebuild
        # the same workspace, and a name cannot be resolved back to one.
        "library_id": space.library_id,
        "kind": "spotify",
        "title": "",
        "status": "resolving",
        "error": None,
        "created_at": _now(),
        "items": [],
    }
    JOBS[job["id"]] = job
    return job


def new_item(track: dict[str, Any]) -> dict[str, Any]:
    return {
        **track,
        "id": uuid.uuid4().hex[:12],
        "status": "pending",
        "progress": 0,
        "direct_url": track.get("direct_url"),
        "match_url": None,
        "match_score": None,
        "file_path": None,
        "error": None,
        "attempts": 0,
    }


def sorted_jobs() -> list[dict[str, Any]]:
    return sorted(JOBS.values(), key=lambda j: j["created_at"], reverse=True)


# A resolved job is held in memory, pushed over the websocket on every tick
# and re-serialised on every GET /api/jobs. Nothing bounded it, so a link to
# a ten-thousand-track playlist was ten thousand dicts going over the socket
# twice a second to a phone. Refused with a number rather than truncated
# silently: quietly downloading the first few hundred of somebody's playlist
# and calling it complete is worse than saying no.
MAX_TRACKS_PER_JOB = 500


def _resolve(url: str) -> tuple[str, str, list[dict[str, Any]]]:
    """Dispatch a link to whichever resolver handles it."""
    if generic.looks_like_url(url) and not spotify.is_spotify(url):
        title, items = generic.resolve(url, MAX_TRACKS_PER_JOB)
        return "generic", title, items
    return spotify.resolve_link(url, MAX_TRACKS_PER_JOB)


def validate(url: str) -> None:
    """Reject obviously unusable input before a job row is created."""
    if generic.looks_like_url(url) and not spotify.is_spotify(url):
        # yt-dlp decides what it can read - there are too many sites to
        # pre-check - but not where it may go: never this network's own
        # machines. yt-dlp follows a site's redirects itself, unchecked.
        try:
            netguard.check(url)
        except netguard.Refused as exc:
            raise generic.ResolveError(str(exc)) from exc
        return
    if spotify.is_short(url):
        return  # only following it says what it is; resolving does that
    spotify.parse_link(url)


async def _resolve_job(job: dict[str, Any], url: str,
                       space: workspace.Workspace,
                       resolve: Any = None) -> None:
    """Resolve a link off the event loop, then announce the result.

    One handler for a cancel, wherever it lands. It covered only the resolve
    itself, so a cancel arriving while the result was being announced left
    the job at "queued" with a dead task behind it: Cancel answered ok and
    did nothing, Retry said it was still running, and it counted against
    the five-job limit until deleted.
    """
    try:
        try:
            # A job built from a list rather than a link (the Library's
            # missing tracks) brings its own resolver.
            kind, title, tracks = await (threads.run(resolve) if resolve
                                         else threads.run(_resolve, url))
        except (spotify.ResolveError, generic.ResolveError) as exc:
            job.update(status="failed", error=str(exc))
            log.warning("resolve failed for %s: %s", url, exc)
            tracks = None
        except Exception as exc:
            job.update(status="failed", error=f"Resolve error: {exc}")
            log.exception("unexpected resolve failure for %s", url)
            tracks = None
        if tracks is not None and len(tracks) > MAX_TRACKS_PER_JOB:
            job.update(
                status="failed",
                title=title,
                error=f"That resolved to {len(tracks)} tracks, more than the "
                      f"{MAX_TRACKS_PER_JOB} this can queue at once. Queue it "
                      "in parts - by album, say.")
            log.warning("refused %s: %d tracks", title, len(tracks))
            tracks = None
        if tracks is None:
            # Not going to run, so nothing else will clear the entry. The
            # refusal for too many tracks used to leave it, and Retry said
            # the job was still running for the life of the process.
            RUNNING.pop(job["id"], None)
            await events.push_job(job)
            return
        job.update(
            kind=kind,
            title=title,
            status="queued",
            items=[new_item(track) for track in tracks],
        )
        log.info("resolved %s -> %d track(s)", title, len(tracks))
        await events.push_job(job)
    except asyncio.CancelledError:
        # A job that had already failed keeps saying why.
        if job["status"] not in FINISHED:
            job.update(status="cancelled", error=None)
        RUNNING.pop(job["id"], None)
        await events.push_job(job)
        raise
    # _run owns the entry from here on, and clears it in its own finally.
    await _run(job, space)


async def _run(job: dict[str, Any], space: workspace.Workspace) -> None:
    """Drive a job to completion, tracking the task so it can be cancelled."""
    job_id = job["id"]
    RUNNING[job_id] = asyncio.current_task()
    try:
        await worker.run_job(job, events.push_job, space, events.push_progress)
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        for item in job["items"]:
            if item["status"] not in ("complete", "failed"):
                item["status"] = "cancelled"
        await asyncio.to_thread(inbox.discard, space, job_id)
        log.info("job %s cancelled", job_id)
        await events.push_job(job)
    except Exception as exc:
        job.update(status="failed", error=f"Download failed: {exc}"[:300])
        log.exception("job %s failed", job_id)
        await events.push_job(job)
    finally:
        RUNNING.pop(job_id, None)


def _check_room(owner: str) -> None:
    """Refuse a sixth job running at once for one person."""
    active = sum(1 for job in JOBS.values()
                 if job.get("owner") == owner
                 and job.get("status") not in FINISHED)
    if active >= MAX_ACTIVE_JOBS:
        raise HTTPException(
            status_code=429,
            detail=f"You already have {active} downloads going. Let some "
                   "finish before queuing more.")


def _visible_jobs(session: auth.Session) -> list[dict[str, Any]]:
    """Only this person's downloads.

    The queue is shared state in one process, but a download belongs to
    whoever asked for it - and somebody else's is not theirs to watch,
    cancel or delete.
    """
    return [job for job in sorted_jobs()
            if job.get("owner") == session.identity.username]


def _owned_job(job_id: str, session: auth.Session) -> dict[str, Any]:
    """A job, if it belongs to whoever is asking.

    Reported as absent rather than forbidden: whose downloads exist is not
    something one account should learn about another.
    """
    job = JOBS.get(job_id)
    if job is None or job.get("owner") != session.identity.username:
        raise HTTPException(status_code=404, detail="No such job.")
    return job


async def _stop_job(job_id: str) -> None:
    """Cancel a job's task and wait for it to unwind.

    Awaited, not fired and forgotten. `cancel()` only schedules the
    CancelledError; the task still has to reach a suspension point and run
    its cleanup. Returning before that means the caller deletes the files it
    is using, which is the race this exists to close.
    """
    task = RUNNING.get(job_id)
    if task is None or task.done():
        RUNNING.pop(job_id, None)
        return
    task.cancel()
    try:
        # Shielded, so the timeout below cannot cancel it a second time and
        # leave the wait looking like the task stopped when it has not.
        await asyncio.wait_for(asyncio.shield(task), timeout=STOP_TIMEOUT)
    except asyncio.CancelledError:
        pass                     # what we asked for
    except TimeoutError:
        # It is still running, and its files are about to be removed. Say so:
        # this is the shape of the bug that stalled every later job, and a
        # silent version of it would be indistinguishable from working.
        log.warning("job %s did not stop within %ss; deleting its files "
                    "anyway", job_id, STOP_TIMEOUT)
    except Exception:
        pass                     # the job's own failure is not ours to raise
    RUNNING.pop(job_id, None)
