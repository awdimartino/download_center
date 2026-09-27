"""HTTP API, WebSocket event stream, and static file serving."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import logging
import time
import uuid
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Any

from fastapi import (Depends, FastAPI, HTTPException, Request, Response,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, beets_runner, diskaudit, duplicates
from . import generic, navidrome, operations, playcounts, store
from . import playlists as smart_playlists
from . import filer, inbox, library, overview, registry, spotify
from . import worker, workspace
from . import config
from . import health as health_checks
from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("download_center")

STATIC_DIR = Path(__file__).parent / "static"

# Process start, for the uptime the health panel reports.
STARTED_AT = time.time()

# --- job state ------------------------------------------------------------
# Jobs are plain dicts held in memory and are not persisted. They serialise
# straight to JSON for both the REST API and the socket. Nothing about a job
# needs to survive a restart: the music it finished is in the library, and
# anything it did not is re-queued by pasting the link again.

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
# anyway. Cancellation lands at the next suspension point, so a download
# mid-chunk stops promptly; this only bounds the pathological case.
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


# --- websocket fan-out ----------------------------------------------------

class Broker:
    """Pushes state changes to the browsers entitled to see them.

    Clients are receive-only: all mutations go through the REST API, so a
    dropped socket costs nothing beyond a fresh snapshot on reconnect. Each
    is remembered with whose session opened it, because a job belongs to the
    person who queued it and broadcasting every job to every browser would
    hand one account a live feed of another's downloads.
    """

    def __init__(self) -> None:
        self._clients: dict[WebSocket, str] = {}
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket, username: str) -> None:
        await ws.accept()
        async with self._lock:
            self._clients[ws] = username

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.pop(ws, None)

    # A client that has stopped reading must not hold up the others. Sends
    # were sequential and unbounded, so one phone on bad wifi with a full TCP
    # window stalled the publish loop - and with it the pusher driving every
    # active job - for everybody.
    SEND_TIMEOUT = 5.0

    async def publish(self, message: dict[str, Any],
                      owner: str | None = None) -> None:
        async with self._lock:
            targets = [(ws, who) for ws, who in self._clients.items()
                       if owner is None or who == owner]
        if not targets:
            return

        async def send(ws: WebSocket) -> WebSocket | None:
            try:
                await asyncio.wait_for(ws.send_json(message),
                                       timeout=self.SEND_TIMEOUT)
                return None
            except Exception:
                # Including the timeout. A socket that cannot take five
                # seconds of slack is gone; it will reconnect and get a fresh
                # snapshot, which is cheaper than holding everyone else up.
                return ws

        # Concurrently, so the slowest client costs the slowest client's time
        # rather than the sum of everybody's.
        stalled = await asyncio.gather(*(send(ws) for ws, _ in targets))
        for ws in stalled:
            if ws is not None:
                await self.unregister(ws)


broker = Broker()


async def push_job(job: dict[str, Any]) -> None:
    await broker.publish({"type": "job", "job": job}, owner=job.get("owner"))


async def push_progress(job: dict[str, Any], changed: list) -> None:
    """Only what moved.

    The full job goes out on every phase change; between those, a 200-track
    playlist would otherwise re-send every track twice a second to a phone,
    almost all of it identical to the last one.
    """
    await broker.publish({
        "type": "job_progress",
        "id": job["id"],
        "status": job["status"],
        "error": job.get("error"),
        "items": [{"id": i["id"], "status": i["status"],
                   "progress": i.get("progress"), "error": i.get("error")}
                  for i in changed],
    }, owner=job.get("owner"))


async def push_operation(operation: operations.Operation) -> None:
    await broker.publish({"type": "operation", "operation": operation.as_dict()},
                         owner=operation.owner)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    store.connect(settings.state_db)
    operations.subscribe(push_operation)
    log.info("workspace root: %s", settings.output_dir)
    if not settings.spotify_configured:
        log.warning("Spotify credentials missing - add them to config/config.toml")

    background = [asyncio.create_task(_audit_loop()),
                  asyncio.create_task(_inbox_loop()),
                  asyncio.create_task(_snapshot_loop())]
    try:
        yield
    finally:
        for task in background:
            task.cancel()
        for task in background:
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _inbox_loop() -> None:
    """File whatever has been dropped into an inbox, as soon as it settles.

    Nothing like the nightly sweep this sits beside. The sweep ran beets,
    which does a MusicBrainz lookup per item and moves files about, and on a
    machine serving music over one link that was felt as stuttering playback
    - so it was pushed to once a night and everything waited hours. Filing
    reads tags and renames, so it can run whenever something appears.
    """
    while True:
        try:
            await asyncio.to_thread(inbox.drain_all)
        except Exception:
            log.exception("draining the inbox failed")
        await asyncio.sleep(inbox.POLL_SECONDS)


# How often to check whether today's snapshot has been taken. Not a clock
# time: a container that was restarting at 3am would simply miss the day, and
# a day of listening history cannot be recovered afterwards. Checking on a
# short cycle for "has today been done" catches up whenever the process
# happens to be alive.
# Every five minutes, not nightly. Navidrome records the moment of a
# track's most recent play beside its running total, so a reading that
# catches a count rising by one has that play's exact time - and reading
# this often means the rise is almost always one. The cost is a single
# indexed join over a few thousand annotation rows, measured at 34ms on the
# Pi this runs on: about ten seconds of work a day for the difference
# between "you played this 1,204 times" and a listening history.
SNAPSHOT_MINUTES = 5


async def _snapshot_loop() -> None:
    """Read the play counts, every few minutes, for ever.

    Navidrome keeps a cumulative total and the moment of the most recent
    play, so anything not captured between two plays of the same track is
    gone for good. Reading often is what makes the difference recoverable:
    at this cadence a track's count almost always rises by exactly one
    between readings, and the timestamp beside it is then that play's.

    Unconditional, where this used to ask "has today been done". That
    question belonged to a job that ran once a day and had to survive
    restarts without repeating itself; here, repeating is free - only
    changed counts are stored, so a reading that finds nothing new writes
    nothing but the run-log row that says the collector is alive.
    """
    while True:
        try:
            await asyncio.to_thread(playcounts.take)
        except Exception:
            log.exception("play-count snapshot failed")
        await asyncio.sleep(SNAPSHOT_MINUTES * 60)


async def _audit_loop() -> None:
    """Keep the on-disk identity audit reasonably fresh.

    It reads tags from every file in the library, so it cannot run inside a
    request. Refreshing on a slow timer means the health panel always has an
    answer, even if it is a few hours old - and a stale answer is only old,
    never wrong, because nothing here writes anything.
    """
    while True:
        for root in _library_roots():
            if diskaudit.stale(root):
                try:
                    await asyncio.to_thread(diskaudit.refresh, root)
                except Exception:
                    log.exception("disk audit failed for %s", root)
        await asyncio.sleep(600)


def _library_roots() -> list[Path]:
    """Every library Navidrome knows about, as paths inside this container.

    Read from Navidrome rather than configured, so a library added there is
    audited here without anyone remembering to say so. A path we cannot see
    is reported once rather than retried silently forever.
    """
    try:
        connection = navidrome.open_db()
    except navidrome.Unavailable:
        return [settings.music_dir]
    with connection:
        rows = connection.execute("select name, path from library").fetchall()

    roots = []
    for name, path in rows:
        root = Path(path)
        if root.is_dir():
            roots.append(root)
        elif str(root) not in _warned_missing:
            _warned_missing.add(str(root))
            log.warning("library %r is at %s, which is not mounted here - "
                        "its files cannot be audited or stamped", name, root)
    return roots or [settings.music_dir]


# Libraries we have already complained about, so the log says it once.
_warned_missing: set[str] = set()


app = FastAPI(title="Download Center", lifespan=lifespan)


# --- who is asking --------------------------------------------------------
# Navidrome is the authority on accounts, so signing in means asking it. The
# session then decides which library a download lands in, whose stars a
# duplicate carries and who a playlist belongs to - none of which are
# properties of the files, and all of which were being treated as though
# they were.

class LoginRequest(BaseModel):
    username: str
    password: str


def current_session(request: Request) -> auth.Session:
    session = getattr(request.state, "session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="Please sign in.")
    return session


def admin_session(request: Request) -> auth.Session:
    """For settings that belong to the installation rather than to a person.

    These hold the Spotify credentials and the Navidrome service password and
    decide where every library lives, so any account being able to rewrite
    them makes an ordinary user an administrator of the whole thing.
    """
    session = current_session(request)
    if not session.identity.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only a Navidrome administrator can change these settings.")
    return session


# Signing in is the only thing you can do without being signed in. Everything
# else is gated here rather than endpoint by endpoint: this tool queues
# downloads, edits settings and quarantines files, and an authorisation check
# that has to be remembered per route is one that will eventually be missed.
OPEN_PATHS = {"/api/auth/login", "/api/auth/logout", "/api/auth/me"}


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    """Liveness, for the container probe. Deliberately outside /api and
    deliberately empty: a probe that needs credentials is a probe that fails,
    and one that reports configuration is an unauthenticated information
    leak."""
    return {"ok": True}


@app.middleware("http")
async def require_session(request: Request, call_next):
    # Looked up once and kept on the request, because the handler needs the
    # same session and resolving it twice means two database opens per call.
    session = auth.get(request.cookies.get(auth.COOKIE))
    request.state.session = session
    path = request.url.path
    if path.startswith("/api/") and path not in OPEN_PATHS and session is None:
        return JSONResponse({"detail": "Please sign in."}, status_code=401)
    return await call_next(request)


@app.post("/api/auth/login")
async def sign_in(request: Request, body: LoginRequest,
                  response: Response) -> dict[str, Any]:
    try:
        session = await asyncio.to_thread(
            auth.sign_in, body.username, body.password)
    except navidrome.LoginFailed as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except navidrome.NotConfigured as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Navidrome is not configured: {exc}") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach Navidrome: {exc}"[:200]) from exc

    # Secure only when the request actually arrived over TLS. Setting it
    # unconditionally would stop the cookie being stored at all on the plain
    # HTTP this is normally served over on a LAN.
    response.set_cookie(
        auth.COOKIE, session.id, httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
        max_age=auth.LIFETIME_SECONDS,
    )
    return session.as_dict()


@app.post("/api/auth/logout")
async def sign_out(request: Request, response: Response) -> dict[str, bool]:
    auth.sign_out(request.cookies.get(auth.COOKIE) or "")
    response.delete_cookie(auth.COOKIE)
    return {"signed_out": True}


@app.get("/api/auth/me")
async def whoami(request: Request) -> dict[str, Any]:
    session = auth.get(request.cookies.get(auth.COOKIE))
    if session is None:
        return {"signed_in": False,
                "navidrome_configured": bool(settings.navidrome_url)}
    return {"signed_in": True, **session.as_dict()}


class JobRequest(BaseModel):
    url: str
    # Only meaningful for an account with more than one library; everybody
    # else never sees the choice.
    library_id: int | None = None


# A resolved job is held in memory, pushed over the websocket on every tick
# and re-serialised on every GET /api/jobs. Nothing bounded it, so a link to
# a ten-thousand-track playlist was ten thousand dicts going over the socket
# twice a second to a phone. Refused with a number rather than truncated
# silently: quietly downloading the first few hundred of somebody's playlist
# and calling it complete is worse than saying no.
MAX_TRACKS_PER_JOB = 500


def _resolve(url: str) -> tuple[str, str, list[dict[str, Any]]]:
    """Dispatch a link to whichever resolver handles it."""
    if generic.looks_like_url(url) and "spotify.com" not in url:
        title, items = generic.resolve(url)
        return "generic", title, items
    return spotify.resolve_link(url)


def validate(url: str) -> None:
    """Reject obviously unusable input before a job row is created."""
    if generic.looks_like_url(url) and "spotify.com" not in url:
        return  # yt-dlp decides; there are too many sites to pre-check
    spotify.parse_link(url)


async def _resolve_job(job: dict[str, Any], url: str,
                       space: workspace.Workspace) -> None:
    """Resolve a link off the event loop, then announce the result."""
    try:
        kind, title, tracks = await asyncio.to_thread(_resolve, url)
    except asyncio.CancelledError:
        # Cancelled while resolving. Without this the job sat at "resolving"
        # for the life of the process, with no task behind it and no way to
        # tell it apart from one still working.
        job.update(status="cancelled", error=None)
        RUNNING.pop(job["id"], None)
        await push_job(job)
        raise
    except (spotify.ResolveError, generic.ResolveError) as exc:
        job.update(status="failed", error=str(exc))
        log.warning("resolve failed for %s: %s", url, exc)
    except Exception as exc:
        job.update(status="failed", error=f"Resolve error: {exc}")
        log.exception("unexpected resolve failure for %s", url)
    else:
        if len(tracks) > MAX_TRACKS_PER_JOB:
            job.update(
                status="failed",
                title=title,
                error=f"That resolved to {len(tracks)} tracks, more than the "
                      f"{MAX_TRACKS_PER_JOB} this can queue at once. Queue it "
                      "in parts - by album, say.")
            log.warning("refused %s: %d tracks", title, len(tracks))
            await push_job(job)
            return
        job.update(
            kind=kind,
            title=title,
            status="queued",
            items=[new_item(track) for track in tracks],
        )
        log.info("resolved %s -> %d track(s)", title, len(tracks))
        await push_job(job)
        await _run(job, space)
        return
    # Only reached when resolving failed: _run owns the entry from here on,
    # and clears it in its own finally.
    RUNNING.pop(job["id"], None)
    await push_job(job)


async def _run(job: dict[str, Any], space: workspace.Workspace) -> None:
    """Drive a job to completion, tracking the task so it can be cancelled."""
    job_id = job["id"]
    RUNNING[job_id] = asyncio.current_task()
    try:
        await worker.run_job(job, push_job, space, push_progress)
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        for item in job["items"]:
            if item["status"] not in ("complete", "failed"):
                item["status"] = "cancelled"
        await asyncio.to_thread(inbox.discard, space, job_id)
        log.info("job %s cancelled", job_id)
        await push_job(job)
    except Exception as exc:
        job.update(status="failed", error=f"Download failed: {exc}"[:300])
        log.exception("job %s failed", job_id)
        await push_job(job)
    finally:
        RUNNING.pop(job_id, None)


@app.post("/api/jobs")
async def create_job(
    request: JobRequest,
    session: auth.Session = Depends(current_session),
) -> dict[str, str]:
    url = request.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="No link provided.")
    # Validate before creating the job, so a typo does not litter the list.
    try:
        validate(url)
    except (spotify.ResolveError, generic.ResolveError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        space = workspace.for_session(session.identity, request.library_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    active = sum(1 for job in JOBS.values()
                 if job.get("owner") == session.identity.username
                 and job.get("status") not in FINISHED)
    if active >= MAX_ACTIVE_JOBS:
        raise HTTPException(
            status_code=429,
            detail=f"You already have {active} downloads going. Let some "
                   "finish before queuing more.")
    _evict_old_jobs(session.identity.username)

    def prepare() -> None:
        beets_runner.ensure_config(space)

    await asyncio.to_thread(prepare)

    job = new_job(url, space)
    await push_job(job)
    # Tracked from the moment it exists, not from when downloading starts.
    # Resolving a large playlist takes a while, and cancelling during it used
    # to report 409 "that job is not running" because RUNNING was only
    # populated once _run was reached.
    RUNNING[job["id"]] = asyncio.create_task(_resolve_job(job, url, space))
    return {"id": job["id"]}


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


@app.get("/api/jobs")
async def list_jobs(
    session: auth.Session = Depends(current_session),
) -> list[dict[str, Any]]:
    return _visible_jobs(session)


@app.get("/api/jobs/{job_id}")
async def get_job(
    job_id: str, session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    return _owned_job(job_id, session)


@app.delete("/api/jobs/{job_id}")
async def delete_job(
    job_id: str, session: auth.Session = Depends(current_session),
) -> dict[str, bool]:
    job = _owned_job(job_id, session)
    # Resolved before anything is removed: if the workspace cannot be built
    # the job would otherwise be gone from memory with its scratch files
    # still on disk and no event telling any browser it went.
    try:
        # The library need not be mounted: this only removes scratch files
        # under the staging root. Refusing would strand the job in memory
        # with its files on disk.
        space = workspace.for_session(session.identity, job.get("library_id"),
                                      require_library=False)
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
    await broker.publish({"type": "job_deleted", "id": job_id},
                         owner=session.identity.username)
    return {"ok": True}


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


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str, session: auth.Session = Depends(current_session),
) -> dict[str, bool]:
    _owned_job(job_id, session)
    task = RUNNING.get(job_id)
    if task is None:
        raise HTTPException(status_code=409, detail="That job is not running.")
    task.cancel()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
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
    for item in retryable:
        item.update(status="pending", error=None, progress=0, attempts=0)

    try:
        space = workspace.for_session(session.identity, job.get("library_id"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job.update(status="queued", error=None)
    await push_job(job)
    asyncio.create_task(_run(job, space))
    return {"retrying": len(retryable)}


class SettingsUpdate(BaseModel):
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None
    concurrency: int | None = None
    audio_bitrate: str | None = None
    max_attempts: int | None = None
    rate_limit_sleep: float | None = None
    navidrome_url: str | None = None
    navidrome_user: str | None = None
    navidrome_password: str | None = None
    acoustid_key: str | None = None
    # Listed in config.EDITABLE and returned by GET, so it has to be settable
    # or the two disagree about what "editable" means.
    beets_enabled: bool | None = None


# Values the browser must never be sent back. Reported as a boolean instead,
# so a form can show whether one is set without ever holding it.
#
# The AcoustID key is here with the passwords rather than with the Spotify
# client id. It identifies an application to a service that rate-limits and
# can ban by key, so handing it to every admin's browser session is a way to
# lose it - and unlike the client id, nothing in the page needs to read it
# back.
SECRETS = ("spotify_client_secret", "navidrome_password", "acoustid_key")


@app.get("/api/settings")
async def get_settings(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """These settings belong to the installation, so only an admin sees them.

    Secrets were already masked, but the rest was not: any signed-in account
    got the Navidrome service URL and username and the Spotify client id.
    The form is disabled for them anyway, so there was nothing to show and
    something to leak.
    """
    if not session.identity.is_admin:
        return {"editable": False}

    values = {key: getattr(settings, key) for key in config.EDITABLE}
    values["editable"] = True
    for key in SECRETS:
        values[key] = ""
        values[f"{key}_set"] = bool(getattr(settings, key))
    return values


@app.put("/api/settings")
async def put_settings(
    update: SettingsUpdate,
    session: auth.Session = Depends(admin_session),
) -> dict[str, Any]:
    changes = {k: v for k, v in update.model_dump().items() if v is not None}
    # A blank secret means "leave it alone", since the form never receives it.
    for key in SECRETS:
        if not changes.get(key):
            changes.pop(key, None)
    if not changes:
        return await get_settings(session)

    try:
        # Validate against the model before touching the live settings.
        settings.__class__(**{**settings.model_dump(), **changes})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc).split(chr(10))[0]) from exc

    await asyncio.to_thread(config.save, changes)
    if "spotify_client_id" in changes or "spotify_client_secret" in changes:
        spotify.reset_client()
    log.info("settings updated: %s", ", ".join(sorted(changes)))
    return await get_settings(session)


# --- browsing -------------------------------------------------------------

def _mark_held(cards: list[dict[str, Any]],
               library_id: int) -> list[dict[str, Any]]:
    """Flag tracks the library already holds.

    Read from Navidrome rather than from a record of what was downloaded.
    The two disagree the moment a file is deleted by hand or arrives any
    other way, and only one of them is answering the question the badge
    asks. It is now a hint rather than a gate - nothing refuses a download
    because of it - so being approximate about "the same song" is the right
    trade.

    Scoped to the library the queue would file into. Reporting what somebody
    else holds would say "you have this" about a record in a collection you
    cannot see.
    """
    held = navidrome.held_in(library_id)
    for card in cards:
        title = card.get("name") or ""
        # Both the full credit Spotify shows and the primary artist this
        # application writes into the file. They differ for a collaboration -
        # the card says "Michael Jackson, Paul McCartney" and the file says
        # "Michael Jackson" - so keying on one of them missed every track
        # with a guest on it, which is exactly when a second copy gets
        # queued.
        credits = {card.get("artist") or "", card.get("primary_artist") or ""}
        card["held"] = any(registry.recording_key(credit, title) in held
                           for credit in credits if credit)
    return cards


def _browsing_library(session: auth.Session) -> int | None:
    """Which library the Browse tab is reporting against.

    None when the account has none, in which case nothing is marked held
    rather than everything being marked held against library zero.
    """
    try:
        return workspace.for_session(session.identity,
                                    require_library=False).library_id
    except ValueError:
        return None


@app.get("/api/search")
async def search(q: str, type: str = "album", limit: int = 24,
                 session: auth.Session = Depends(current_session),
                 ) -> dict[str, Any]:
    query = q.strip()
    if not query:
        return {"type": type, "results": []}
    # Spotify rejects anything over fifty, which arrived as an unexplained
    # 502. Clamped here so a hand-edited URL is answered rather than blamed
    # on Spotify.
    limit = max(1, min(limit, 50))
    try:
        results = await asyncio.to_thread(spotify.browse, query, type, limit)
    except spotify.ResolveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Spotify error: {exc}") from exc

    library_id = _browsing_library(session)
    if type == "track" and library_id is not None:
        await asyncio.to_thread(_mark_held, results, library_id)
    return {"type": type, "results": results}


@app.get("/api/albums/{album_id}")
async def album(album_id: str,
                session: auth.Session = Depends(current_session),
                ) -> dict[str, Any]:
    try:
        detail = await asyncio.to_thread(spotify.album_detail, album_id)
    except spotify.ResolveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Album not found: {exc}") from exc
    library_id = _browsing_library(session)
    if library_id is not None:
        await asyncio.to_thread(_mark_held, detail["tracks"], library_id)
    else:
        for track in detail["tracks"]:
            track["held"] = False
    detail["held_count"] = sum(1 for t in detail["tracks"] if t["held"])
    return detail


@app.get("/api/artists/{artist_id}/albums")
async def artist(artist_id: str) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(spotify.artist_albums, artist_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Artist not found: {exc}") from exc


# --- health ---------------------------------------------------------------

@app.get("/api/health")
async def health(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    # Reads Navidrome's database and stats a few directories, so it is quick
    # but blocking; a thread keeps it off the event loop.
    return await asyncio.to_thread(
        health_checks.report, STARTED_AT, session.identity.libraries,
        session.identity)


@app.post("/api/health/audit")
async def health_audit(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Re-read identity tags from every file.

    Started, not awaited. It reads every file in the library, which is
    minutes on a Pi - far longer than a browser will hold a request open, so
    awaiting it made the button look broken while the work went on unseen.
    The result arrives over the websocket and is readable from
    /api/operations.
    """
    libraries = list(session.identity.libraries)

    def run() -> dict[str, Any]:
        return {library["name"]: diskaudit.refresh(Path(library["path"])).as_dict()
                for library in libraries}

    operation, started = operations.start(
        "audit", session.identity.username, run)
    return {"started": started, "operation": operation.as_dict()}


# --- duplicates -----------------------------------------------------------

class ResolveRequest(BaseModel):
    key: str
    keeper: str


class DismissRequest(BaseModel):
    key: str
    note: str = ""


def _duplicate_groups(identity: navidrome.Identity) -> list[duplicates.Group]:
    connection = navidrome.open_db()
    with connection:
        return duplicates.find(connection, identity)


@app.get("/api/duplicates")
async def list_duplicates(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    try:
        groups = await asyncio.to_thread(_duplicate_groups, session.identity)
    except navidrome.Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "groups": [g.as_dict() for g in groups],
        "confident": sum(1 for g in groups if g.confident),
    }


@app.post("/api/duplicates/resolve")
async def resolve_duplicate(
    request: ResolveRequest,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    groups = await asyncio.to_thread(_duplicate_groups, session.identity)
    # Matched on what the browser was shown, which is the file-derived key.
    group = next((g for g in groups if g.dismiss_key == request.key), None)
    if group is None:
        raise HTTPException(status_code=404, detail="No such duplicate group.")
    try:
        outcome = await asyncio.to_thread(
            duplicates.resolve, group, request.keeper, session.identity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # The file has moved; Navidrome still has it at the old path until it
    # looks again. The page does not wait for that - it filters resolved
    # copies out on its own - but without this the stale rows sit in
    # Navidrome's index until whenever it next scans on its own.
    if outcome.get("quarantined"):
        await asyncio.to_thread(navidrome.notify)
    return outcome


@app.post("/api/duplicates/dismiss")
async def dismiss_duplicate(
    request: DismissRequest,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    await asyncio.to_thread(store.dismiss_duplicate, request.key, request.note)
    return {"dismissed": request.key}


@app.get("/api/duplicates/quarantined")
async def list_quarantined(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """What has been set aside. Read-only, deliberately.

    Walks the quarantine directory of each library this person can see and
    joins the ledger onto it, so a file with no record and a record with no
    file both show up rather than neither.
    """
    return await asyncio.to_thread(
        duplicates.quarantine_survey, session.identity)


@app.post("/api/duplicates/auto")
async def auto_resolve_duplicates(
    apply: bool = False,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        connection = navidrome.open_db()
        with connection:
            return duplicates.auto_resolve(connection, session.identity,
                                           apply=apply)

    try:
        outcome = await asyncio.to_thread(run)
    except navidrome.Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # Once for the whole run rather than once per group: a scan per
    # resolved duplicate would be a hundred scans of the same library.
    if outcome.get("resolved"):
        await asyncio.to_thread(navidrome.notify)
    return outcome


# --- smart playlists -----------------------------------------------------

class PlaylistRequest(BaseModel):
    name: str
    # The flat form the browser works in; translated to Navidrome's nested
    # rule shape in one place, in app/playlists.py.
    form: dict[str, Any]
    comment: str = ""
    public: bool = False


@app.get("/api/playlists")
async def list_playlists(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """This person's smart playlists, plus what a rule can be made of.

    The vocabulary rides along with the list so the form has everything it
    needs from one request, and cannot render a field the server would then
    refuse.
    """
    try:
        found = await asyncio.to_thread(smart_playlists.mine, session.identity)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_navidrome_error(exc)) from exc
    return {
        "playlists": found,
        "vocabulary": smart_playlists.vocabulary(session.identity),
    }


@app.post("/api/playlists")
async def create_playlist(
    request: PlaylistRequest,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    return await _save_playlist(request, session, None)


@app.put("/api/playlists/{playlist_id}")
async def update_playlist(
    playlist_id: str,
    request: PlaylistRequest,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    # Ownership is checked by asking Navidrome what this person has rather
    # than by trusting the id in the path: the API would update somebody
    # else's playlist just as willingly.
    owned = await asyncio.to_thread(smart_playlists.mine, session.identity)
    if not any(p["id"] == playlist_id for p in owned):
        raise HTTPException(status_code=404,
                            detail="That is not one of your smart playlists.")
    return await _save_playlist(request, session, playlist_id)


@app.delete("/api/playlists/{playlist_id}")
async def remove_playlist(
    playlist_id: str,
    session: auth.Session = Depends(current_session),
) -> dict[str, str]:
    try:
        await asyncio.to_thread(smart_playlists.remove, session.identity,
                                playlist_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_navidrome_error(exc)) from exc
    return {"deleted": playlist_id}


async def _save_playlist(request: PlaylistRequest, session: auth.Session,
                         playlist_id: str | None) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            smart_playlists.save, session.identity, request.name,
            request.form, request.comment, request.public, playlist_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_navidrome_error(exc)) from exc


def _navidrome_error(exc: Exception) -> str:
    """Navidrome's own words where it gave any.

    A rejected rule is the interesting case: this app offers a vocabulary it
    believes the server accepts, and if that belief is wrong the server's
    complaint says which field, where a generic message would not.
    """
    response = getattr(exc, "response", None)
    detail = ""
    if response is not None:
        detail = (response.text or "").strip()[:300]
    return f"Navidrome refused that: {detail}" if detail else f"Navidrome is unreachable: {exc}"


# --- library ---------------------------------------------------------------


@app.get("/api/overview")
async def overview_page(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """What the landing page shows, in one request.

    One request rather than four, because the panels it summarises each walk
    Navidrome's whole index and a landing page calling all of them would be
    the slowest screen in the application.
    """
    return await asyncio.to_thread(overview.overview, session.identity)


@app.get("/api/library")
async def library_list(
    limit: int = library.PAGE,
    offset: int = 0,
    unmatched: bool = False,
    q: str = "",
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Every album this person owns, newest first.

    `unmatched` narrows to albums MusicBrainz has not confirmed, which is
    derived from the files every time rather than stored. It is a filter,
    not the definition of the list - an album can be tagged perfectly by
    hand and still never have a MusicBrainz ID, and hiding the rest is what
    made a matched album unreachable once it had been matched.
    """
    return await asyncio.to_thread(
        library.listing, session.identity, limit, offset, unmatched, q)


@app.get("/api/library/album")
async def library_album(
    library_id: int,
    folder: str,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """One album's tracks, for a row that has been opened."""
    try:
        return await asyncio.to_thread(
            library.tracks, session.identity, library_id, folder)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class AlbumEdit(BaseModel):
    library_id: int
    folder: str
    album_artist: str
    album: str


class TrackEdit(BaseModel):
    library_id: int
    path: str
    title: str | None = None
    artist: str | None = None
    track_no: int | None = None
    disc_no: int | None = None
    album_artist: str | None = None
    album: str | None = None


def _named(*values: str | None) -> None:
    """Refuse a blank where a name is wanted.

    An empty artist or album is not a correction, it is how a file ends up
    in `Unknown Artist/Unknown Album` - which is usually the thing somebody
    opened this editor to escape.
    """
    for value in values:
        if value is not None and not value.strip():
            raise HTTPException(
                status_code=400,
                detail="An artist and an album cannot be blank. Clearing "
                       "them files the track under Unknown Artist.")


@app.post("/api/library/album/edit")
async def library_album_edit(
    body: AlbumEdit,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Rename a whole album, and move its files to match.

    Album-level because one album is one UUID: the artist and the title are
    properties of the folder, and editing them on a single track is how a
    record becomes two. The album keeps its identity through the change, so
    album-level stars and play counts survive it - unless the new name is
    already taken, in which case these files join the record that is there.
    """
    _named(body.album_artist, body.album)
    try:
        space = workspace.for_session(session.identity, body.library_id)
        folder = library.album_dir(
            session.identity, body.library_id, body.folder)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not inbox.settled(folder):
        raise HTTPException(
            status_code=409,
            detail=f"{folder.name} is still arriving; try again shortly.")

    def run() -> dict[str, Any]:
        filed = filer.retag_album(space, folder,
                                  albumartist=body.album_artist,
                                  album=body.album)
        navidrome.notify()
        return {"ran": True, "moved": len(filed),
                "folder": str(filed[0].path.parent.relative_to(
                    space.library_path)) if filed else body.folder,
                "album_uuid": filed[0].album_uuid if filed else None}

    try:
        return await asyncio.to_thread(run)
    except filer.NotEditable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/library/track/edit")
async def library_track_edit(
    body: TrackEdit,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Change one track, and move it if its tags now say it belongs elsewhere.

    Title and numbers rename it inside its own folder. Artist or album take
    it *out* of its album and into another - which is what rescues a track
    filed under the wrong record, and what splits one if it is done by
    mistake. The browser confirms that before asking.
    """
    _named(body.album_artist, body.album)
    if all(value is None for value in
           (body.title, body.artist, body.track_no, body.disc_no,
            body.album_artist, body.album)):
        raise HTTPException(status_code=400, detail="Nothing to change.")
    try:
        space = workspace.for_session(session.identity, body.library_id)
        path = library.track_path(session.identity, body.library_id, body.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not inbox.settled(path):
        raise HTTPException(
            status_code=409,
            detail=f"{path.name} is still arriving; try again shortly.")

    def run() -> dict[str, Any]:
        filed = filer.retag_track(
            space, path, albumartist=body.album_artist, album=body.album,
            artist=body.artist, title=body.title,
            track_no=body.track_no, disc_no=body.disc_no)
        navidrome.notify()
        return {"ran": True,
                "path": str(filed.path.relative_to(space.library_path)),
                "album_uuid": filed.album_uuid,
                "identified": filed.identified}

    try:
        return await asyncio.to_thread(run)
    except filer.NotEditable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# Long, because a cover does not change without the file changing, and the
# id is derived from the file. A page of fifty of these is otherwise fifty
# round trips every time somebody scrolls back up.
ART_CACHE = "public, max-age=604800"


@app.get("/api/library/art")
async def library_art(
    id: str,
    size: int = 96,
    session: auth.Session = Depends(current_session),
) -> Response:
    """One track's cover, proxied from Navidrome at the size asked for."""
    size = max(32, min(int(size), 600))
    if not await asyncio.to_thread(library.owns_track, session.identity, id):
        raise HTTPException(status_code=404, detail="No such track.")
    try:
        body, kind = await asyncio.to_thread(navidrome.cover_art, id, size)
    except navidrome.NotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        # A missing cover is the ordinary case, not a fault. The browser
        # hides the image and the row reads fine without it.
        raise HTTPException(status_code=404, detail=str(exc)[:200]) from exc
    return Response(content=body, media_type=kind,
                    headers={"Cache-Control": ART_CACHE})


@app.post("/api/library/rescan")
async def library_rescan(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Ask Navidrome to look again, now.

    The list is read from Navidrome's database, which refreshes on scan, so
    it can be a few minutes behind what is on disk. That is fine for a page
    somebody opens deliberately and not fine when they have just changed
    something and want to see it.
    """
    if not navidrome.service_configured():
        raise HTTPException(
            status_code=400,
            detail="Navidrome's address and service credentials are not set, "
                   "so a scan cannot be requested from here.")
    # Asked separately from whether it worked. `notify` reports both as
    # False, and telling somebody to fix credentials that are already right
    # sends them to the one place the problem is not.
    if not await asyncio.to_thread(navidrome.notify):
        raise HTTPException(
            status_code=502,
            detail="Navidrome did not answer, so it has not been asked to "
                   "scan. The list is still correct as of its last one.")
    return {"scanning": True}


class AlbumTarget(BaseModel):
    library_id: int
    folder: str


@app.post("/api/library/match")
async def library_match(
    body: AlbumTarget,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """What this album would match against, asked on demand.

    Matching is manual now, one item at a time, from this page. Nothing here
    writes anything: the caller gets the candidate list that `quiet_fallback:
    skip` used to throw away, and a person picks from it. Applying a choice
    belongs to the tagging page, which is designed separately.

    An operation rather than a plain request: a candidate lookup is several
    MusicBrainz round trips and took up to seventy seconds against the real
    backlog, which is far longer than a request should be held open. The
    answer arrives over the websocket.
    """
    try:
        space = workspace.for_session(session.identity, body.library_id)
        path = library.album_dir(session.identity, body.library_id, body.folder)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def run() -> dict[str, Any]:
        return beets_runner.candidates(space, path)

    operation, started = operations.start(
        "candidates", session.identity.username, run)
    return {"started": started, "operation": operation.as_dict()}


class AlbumChoice(BaseModel):
    library_id: int
    folder: str
    release_id: str


@app.post("/api/library/match/apply")
async def library_match_apply(
    body: AlbumChoice,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Tag an album as the release somebody picked from the candidate list.

    The one thing allowed to move a file after it is written. It is
    deliberate, rare and watched: a person looked at a list and pointed at a
    row. The album UUID is re-pointed rather than reissued, so the record
    keeps its Navidrome identity and album-level stars and play counts
    survive the retag.
    """
    try:
        space = workspace.for_session(session.identity, body.library_id)
        path = library.album_dir(session.identity, body.library_id, body.folder)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not inbox.settled(path):
        # A download of this album is still filing tracks into it.
        # Retagging mid-flight re-points the registry, and the tracks that
        # land afterwards still carry the old tags, miss the key that has
        # just moved, and found a second album beside the first.
        raise HTTPException(
            status_code=409,
            detail=f"{path.name} is still arriving; try again shortly.")

    def run() -> dict[str, Any]:
        # Read before beets touches anything: once the tags are rewritten
        # there is nothing left to say which album this used to be.
        was = filer.album_key_of(path)
        result = beets_runner.import_chosen(space, path, body.release_id)
        if not result.get("imported"):
            return result

        # Beets retagged in place, so the files are still where they were
        # and there is no need to ask its database where they went. Settle
        # the album UUID first, then let the filer move each one - it is the
        # only thing that decides where a track lives, so a retag that
        # changes the artist or album puts them under the new name in the
        # layout everything else uses.
        retagged = filer.audio_in(path)
        result["album_uuid"] = filer.after_retag(space, retagged, was)
        result["filed"] = [str(filer.file_track(space, one).path)
                           for one in retagged]
        navidrome.notify()
        return result

    operation, started = operations.start(
        "import", session.identity.username, run)
    return {"started": started, "operation": operation.as_dict()}


@app.get("/api/operations")
async def list_operations(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """What long-running work is in flight, and how the last run went."""
    return {"operations": operations.all_operations()}


@app.get("/api/playcounts")
async def playcount_status(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Whether snapshots are actually being taken.

    The thing that must not fail quietly is the collecting, and that is
    checkable tonight - long before there is enough history to say anything
    interesting with.
    """
    return await asyncio.to_thread(playcounts.status)


# How far back the Listening panel will look. Bounded because the window
# reaches straight into a query: an unbounded one asks for every snapshot
# ever taken, on a Raspberry Pi, from a button.
MAX_LISTENING_DAYS = 3650
MAX_LISTENING_TRACKS = 200


@app.get("/api/playcounts/top")
async def playcount_top(
    days: int = 30,
    limit: int = 25,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """The signed-in person's most played tracks over a window.

    Theirs alone. Play counts are per Navidrome account, and one person's
    listening is not another's to read - the same rule the rest of this
    application follows.
    """
    days = max(1, min(days, MAX_LISTENING_DAYS))
    limit = max(1, min(limit, MAX_LISTENING_TRACKS))

    def collect() -> dict[str, Any]:
        # Today, not yesterday. The window stopped at the last *complete*
        # day because a nightly reading could not describe a day still
        # going on; reading every few minutes can, and the panel was
        # otherwise unable to show anything played since midnight.
        end = playcounts.today()
        start = (datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC)
                 - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        tracks = playcounts.top_tracks(start, end, session.identity.user_id,
                                       limit)
        return {
            "start": start, "end": end, "days": days,
            "tracks": tracks,
            "plays": sum(track["plays"] for track in tracks),
            # Theirs, not the installation's. status() counts every account's
            # imported history together, which shown to someone who has never
            # played anything is both baffling and none of their business.
            "coverage": playcounts.coverage(session.identity.user_id),
        }

    return await asyncio.to_thread(collect)


@app.post("/api/playcounts/snapshot")
async def playcount_snapshot(
    session: auth.Session = Depends(admin_session),
) -> dict[str, Any]:
    """Take one now rather than waiting for the timer. Admin only: it reads
    every account's listening, not just the caller's."""
    return await asyncio.to_thread(playcounts.take)


@app.get("/api/status")
async def status(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    return {
        "spotify_configured": settings.spotify_configured,
        "output_dir": str(settings.output_dir),
        "concurrency": settings.concurrency,
    }


@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    session = auth.get(ws.cookies.get(auth.COOKIE))
    if session is None:
        await ws.close(code=4401)
        return
    await broker.register(ws, session.identity.username)
    try:
        await ws.send_json({"type": "snapshot", "jobs": _visible_jobs(session)})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("websocket closed unexpectedly", exc_info=True)
    finally:
        await broker.unregister(ws)


ASSETS = ("app.js", "style.css")


@functools.lru_cache(maxsize=1)
def asset_version() -> str:
    """A token that changes when the assets do.

    Appended to their URLs, so a new deploy asks for a URL the browser has
    never seen and cannot have a stale copy of. The headers below say to
    revalidate, but a browser already holding a heuristically-fresh copy does
    not ask - it has no reason to - so headers alone cannot rescue a browser
    that is already wrong. A new URL can.

    Computed once: the files cannot change inside a running container.
    """
    digest = hashlib.sha256()
    for name in ASSETS:
        digest.update((STATIC_DIR / name).read_bytes())
    return digest.hexdigest()[:12]


@app.get("/")
async def index() -> HTMLResponse:
    # Never cached. The shell decides whether to show the sign-in form, so a
    # browser holding yesterday's copy carries on as though the application
    # still had no accounts - and never asks for the new one, because it has
    # no reason to. It is also what carries the asset version, so it has to
    # be the one document that is always fetched fresh.
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ASSETS:
        html = html.replace(f"/static/{name}", f"/static/{name}?v={asset_version()}")
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


class RevalidatedStatic(StaticFiles):
    """Assets a browser must check with us before reusing.

    Starlette sends an ETag and a Last-Modified but no Cache-Control, and a
    response carrying no Cache-Control is *heuristically* cacheable: the
    browser invents a freshness lifetime of its own, conventionally a
    fraction of the file's age, and does not ask again until it expires.
    Safari's is long enough to matter.

    So a deploy served a fresh index.html - which is `no-store` - beside an
    app.js the browser saw no reason to re-fetch. The shell said one thing
    and the code behind it did another: the *Import as-is* button was in the
    file the container served and absent from the page in front of the user.
    This is why "hard-refresh and try again" kept appearing in the notes.

    `no-cache` does not mean "do not store" - it means "ask first". An
    unchanged file still answers 304 against the ETag and costs a round trip
    on a LAN, which is the right price for never shipping half a deploy.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        # Set on whatever comes back, because a 304 is built inside the call
        # above and carries its own copy of these headers.
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatedStatic(directory=STATIC_DIR), name="static")
