"""Download jobs over HTTP, and the socket that reports on them."""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .. import auth, events, generic, inbox, jobs, spotify, workspace
from ..jobs import (
    JOBS,
    RUNNING,
    _check_room,
    _evict_old_jobs,
    _owned_job,
    _stop_job,
    new_job,
    validate,
)
from . import deps
from .deps import _prepare, current_session

log = logging.getLogger("navidrome_companion")
# Every route here is for a signed-in person. Declared on the router as
# well as gated by the session middleware, so a route added here without
# its own Depends is still guarded, and the guard does not rest on a
# path prefix alone.
router = APIRouter(dependencies=[Depends(current_session)])
# The socket checks its own session and origin: a dependency built on an
# HTTP request cannot serve it.
socket = APIRouter()


# How often an open socket asks whether its session still exists.
SOCKET_RECHECK = 60


class JobRequest(BaseModel):
    url: str
    # Only meaningful for an account with more than one library; everybody
    # else never sees the choice.
    library_id: int | None = None


@router.post("/api/jobs")
async def create_job(
    request: JobRequest,
    session: auth.Session = Depends(current_session),
) -> dict[str, str]:
    url = request.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="No link provided.")
    url = spotify.canonical(url) or url
    # Validate before creating the job, so a typo does not litter the list.
    try:
        # In a thread: checking a direct link resolves its host name.
        await asyncio.to_thread(validate, url)
    except (spotify.ResolveError, generic.ResolveError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        space = await asyncio.to_thread(
            workspace.for_session, session.identity, request.library_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # The inbox and scratch space, not beets: a download never touches it,
    # and Find matches writes its config on first use.
    await _prepare(space)

    # From the count to the new job with no await between them, so two
    # requests at once cannot both see room for one more.
    _check_room(session.identity.username)
    _evict_old_jobs(session.identity.username)
    job = new_job(url, space)
    await events.push_job(job)
    # Tracked from the moment it exists, not from when downloading starts.
    # Resolving a large playlist takes a while, and cancelling during it used
    # to report 409 "that job is not running" because RUNNING was only
    # populated once _run was reached.
    RUNNING[job["id"]] = asyncio.create_task(jobs._resolve_job(job, url, space))
    return {"id": job["id"]}


@router.get("/api/jobs")
async def list_jobs(
    session: auth.Session = Depends(current_session),
) -> list[dict[str, Any]]:
    return jobs._visible_jobs(session)


@router.get("/api/jobs/{job_id}")
async def get_job(
    job_id: str, session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    return _owned_job(job_id, session)


@router.delete("/api/jobs/{job_id}")
async def delete_job(
    job_id: str, session: auth.Session = Depends(current_session),
) -> dict[str, bool]:
    job = _owned_job(job_id, session)
    # Resolved before anything is removed: if the workspace cannot be built
    # the job would otherwise be gone from memory with its scratch files
    # still on disk and no event telling any browser it went.
    try:
        # The library need not be mounted: this only removes scratch files
        # under the workspace root. Refusing would strand the job in memory
        # with its files on disk.
        space = await asyncio.to_thread(functools.partial(
            workspace.for_session, session.identity, job.get("library_id"),
            require_library=False))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Stopped before anything is removed. Deleting used to drop the job from
    # memory and delete its scratch directory while its downloads were still
    # running: yt-dlp recreated the directory underneath itself, every rename
    # failed with ENOENT, and the worker retried the whole way through its
    # backoff. The tasks never stopped, because nothing had told them to.
    #
    # They also each held a slot on the download gate. That gate is
    # process-wide, so orphaned tasks did not merely waste their own job's
    # capacity - they starved every later job of every user, which presents
    # as a queue stuck at "running" with nothing in the log to explain it.
    await _stop_job(job_id)

    JOBS.pop(job_id, None)
    await asyncio.to_thread(inbox.discard, space, job_id)
    await events.broker.publish({"type": "job_deleted", "id": job_id},
                         owner=session.identity.username)
    return {"ok": True}


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str, session: auth.Session = Depends(current_session),
) -> dict[str, bool]:
    _owned_job(job_id, session)
    task = RUNNING.get(job_id)
    if task is None:
        raise HTTPException(status_code=409, detail="That job is not running.")
    task.cancel()
    return {"ok": True}


@router.post("/api/jobs/{job_id}/retry")
async def retry_job(
    job_id: str, session: auth.Session = Depends(current_session),
) -> dict[str, int]:
    job = _owned_job(job_id, session)
    if job_id in RUNNING:
        raise HTTPException(status_code=409, detail="That job is still running.")

    # Only the failures are reset. `_process` returns immediately for an
    # item that is already complete, so the rest are not touched.
    retryable = [i for i in job["items"] if i["status"] in ("failed", "cancelled")]
    if not retryable:
        raise HTTPException(status_code=409, detail="Nothing to retry.")
    # Everything that can refuse comes before anything is reset, so a
    # refused retry leaves the job as it was, failures and all.
    try:
        space = await asyncio.to_thread(
            workspace.for_session, session.identity, job.get("library_id"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Asked again after the wait: two presses (a double-click, two tabs)
    # both passed the check above while this one was off the loop, and both
    # started a runner on the same job - every failed track downloaded and
    # filed twice.
    if job_id in RUNNING:
        raise HTTPException(status_code=409, detail="That job is still running.")
    _check_room(session.identity.username)

    for item in retryable:
        item.update(status="pending", error=None, progress=0, attempts=0)
    job.update(status="queued", error=None)
    # Tracked before anything else is awaited, so neither a second retry nor
    # a cancel pressed straight away can miss it.
    RUNNING[job_id] = asyncio.create_task(jobs._run(job, space))
    await events.push_job(job)
    return {"retrying": len(retryable)}


@socket.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    session = await asyncio.to_thread(auth.get, ws.cookies.get(auth.COOKIE))
    # A socket is not bound by CORS: any page could open one with this
    # person's cookie and read their downloads as they move.
    if session is not None and not deps.same_origin(ws.headers):
        await ws.accept()
        await ws.close(code=4403)
        return
    if session is None:
        # Accepted first: a close before accepting becomes an HTTP 403, the
        # browser only ever sees 1006, and the page went on reconnecting as
        # "offline" instead of asking for a sign-in.
        await ws.accept()
        await ws.close(code=4401)
        return
    cookie = ws.cookies.get(auth.COOKIE)
    await events.broker.register(ws, session.identity.username, cookie)
    try:
        await ws.send_json({"type": "snapshot", "jobs": jobs._visible_jobs(session)})
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=SOCKET_RECHECK)
            except TimeoutError:
                # A socket outlived its session - expired, the account
                # removed, the password changed - and went on receiving that
                # person's events. Asked again now and then; 4401 is what
                # the page reads as "show the sign-in form".
                if await asyncio.to_thread(auth.get, cookie) is None:
                    await ws.close(code=4401)
                    return
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("websocket closed unexpectedly", exc_info=True)
    finally:
        await events.broker.unregister(ws)
