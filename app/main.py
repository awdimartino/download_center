"""HTTP API, WebSocket event stream, and static file serving."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import logging
import os
import time
import urllib.parse
import uuid
from datetime import datetime, UTC
from pathlib import Path
from collections.abc import Callable
from typing import Any, NoReturn

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Request,
                     Response, UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware

from . import auth, beets_runner, combine, covers, diskaudit, duplicates
from . import generic, navidrome, operations, playcounts, store
from . import playlists as smart_playlists
from . import filer, inbox, library, netguard, overview, registry, replaygain, spotify
from . import folderlock, heartbeat, uuidtags
from . import threads, worker, workspace
from . import config
from . import health as health_checks
from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("navidrome_companion")

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
    try:
        await threads.run(inbox.clear_scratch)
    except Exception:
        log.exception("clearing unfinished downloads failed")

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
        await _wind_down()


# How long a shutdown waits for operations to reach a stopping point. Docker
# kills the process when its own grace period runs out, so the compose file
# gives it longer than this.
SHUTDOWN_GRACE = 60


async def _wind_down() -> None:
    """Stop in-flight work before the process exits.

    A shutdown used to cancel the three loops and nothing else. Downloads
    and operations went on in their threads, which kept the process alive
    until Docker killed it - possibly part way through rewriting a file in
    the library. Jobs are cancelled the way the Cancel button does it, which
    stops each download and waits for anything being filed; operations are
    asked to stop after the album or file they are on.
    """
    running = list(RUNNING)
    operation_tasks = operations.stop_all()
    if running or operation_tasks:
        log.info("shutting down: stopping %d job(s) and %d operation(s)",
                 len(running), len(operation_tasks))
    await asyncio.gather(*(_stop_job(job_id) for job_id in running))
    if operation_tasks:
        _, late = await asyncio.wait(operation_tasks, timeout=SHUTDOWN_GRACE)
        if late:
            log.warning("%d operation(s) still running at shutdown", len(late))


def _inbox_problem(results: dict[str, inbox.Result]) -> str | None:
    """What a completed pass still has to say, without naming anybody: Health
    is shown to every account."""
    unreadable = workspace.unreadable()
    broken = sum(1 for result in results.values() if result.broken)
    parts = []
    if unreadable:
        parts.append(f"{unreadable} workspace(s) could not be read")
    if broken:
        parts.append(f"{broken} inbox(es) could not be looked at")
    return "; ".join(parts) + ". See the log." if parts else None


async def _inbox_loop() -> None:
    """File whatever has been dropped into an inbox, as soon as it settles.

    It replaced a nightly sweep. The sweep ran beets, which does a
    MusicBrainz lookup per item and moves files about, and on a machine
    serving music over one link that was felt as stuttering playback - so it
    was pushed to once a night and everything waited hours. Filing reads tags
    and renames, so it can run whenever something appears.
    """
    while True:
        try:
            results = await threads.run(inbox.drain_all)
            # A download asks Navidrome to scan when it finishes; a drop
            # used to wait for Navidrome's own schedule instead.
            if any(result.changed for result in results.values()):
                await threads.run(navidrome.notify)
            heartbeat.ok("inbox", _inbox_problem(results))
        except Exception as exc:
            log.exception("draining the inbox failed")
            heartbeat.failed("inbox", exc)
        await asyncio.sleep(inbox.POLL_SECONDS)


# How often the play counts are read: every five minutes, not nightly.
# Navidrome records the moment of a track's most recent play beside its
# running total, so a reading that
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
    # Everyone on the first pass, so the first visit after a restart or a
    # deploy is not the one that computes; after that, whoever just played
    # something - a reading with new plays is what makes their cached
    # statistics stale.
    first = True
    while True:
        try:
            taken = await threads.run(playcounts.take)
            if first or taken.get("users"):
                await threads.run(
                    overview.warm, None if first else taken["users"])
            first = False
            heartbeat.ok("snapshot")
        except Exception as exc:
            log.exception("play-count snapshot failed")
            heartbeat.failed("snapshot", exc)
        await asyncio.sleep(SNAPSHOT_MINUTES * 60)


async def _audit_loop() -> None:
    """Keep the on-disk identity audit reasonably fresh.

    It reads tags from every file in the library, so it cannot run inside a
    request. Refreshing on a slow timer means the health panel always has an
    answer, even if it is a few hours old - and a stale answer is only old,
    never wrong, because nothing here writes anything.
    """
    while True:
        # The whole pass, not only each audit: reading the library list can
        # fail too (a locked database), and an exception escaping here ended
        # the task for the life of the process while Health's audit aged.
        try:
            failed = 0
            for root in await threads.run(_library_roots):
                if await threads.run(diskaudit.stale, root):
                    try:
                        await threads.run(diskaudit.refresh, root)
                    except Exception:
                        failed += 1
                        log.exception("disk audit failed for %s", root)
            heartbeat.ok("audit", f"{failed} library audit(s) failed. See the "
                                  "log." if failed else None)
        except Exception as exc:
            log.exception("disk audit pass failed")
            heartbeat.failed("audit", exc)
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


app = FastAPI(title="Navidrome Companion", lifespan=lifespan)


class TextGZip:
    """Compresses what is worth compressing: the page, its scripts and
    stylesheet, and the JSON behind it - a few hundred kilobytes a load,
    shrinking to about a third. Cover art is already JPEG or PNG, and
    gzipping it again would spend the Pi's CPU to save nothing.
    """

    def __init__(self, inner):
        self.inner = inner
        self.zipped = GZipMiddleware(inner, minimum_size=1024, compresslevel=6)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not path or path.startswith("/api/library/art") or path.endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff2")):
            await self.inner(scope, receive, send)
        else:
            await self.zipped(scope, receive, send)


app.add_middleware(TextGZip)


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

# FastAPI's generated API description and its two viewers. Outside /api, so
# the prefix rule never covered them, and they listed every route and its
# parameters to anyone who could reach the port. Kept for a signed-in
# person, who can already call all of it.
DOC_PATHS = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    """Liveness, for the container probe. Deliberately outside /api and
    deliberately empty: a probe that needs credentials is a probe that fails,
    and one that reports configuration is an unauthenticated information
    leak."""
    return {"ok": True}


# Methods that change something, and so must come from this app's own page.
CHANGING = {"POST", "PUT", "PATCH", "DELETE"}


def same_origin(headers: Any) -> bool:
    """Whether a browser sent this from a page of this app's own origin.

    The session cookie is SameSite=Lax, and "site" ignores the port: a page
    served by Navidrome or Calibre on another port of the same host is the
    same site, so it could fire POSTs here that carried the cookie - the
    body-less ones (an auto-resolve, an audit, a rescan) need nothing else.

    Sec-Fetch-Site is the browser's own answer and is right behind a proxy
    that rewrites Host. Without it, Origin is compared with the host asked
    for. A request with neither did not come from a browser page - curl, a
    script - and carries no cookie it did not mean to.
    """
    site = headers.get("sec-fetch-site")
    if site:
        return site in ("same-origin", "none")
    origin = headers.get("origin")
    if not origin:
        return True
    asked = {headers.get("host", "")} | {
        h.strip() for h in headers.get("x-forwarded-host", "").split(",") if h.strip()}
    return urllib.parse.urlsplit(origin).netloc in asked


@app.middleware("http")
async def require_session(request: Request, call_next):
    # Looked up once and kept on the request, because the handler needs the
    # same session and resolving it twice means two database opens per call.
    # In a thread: every few minutes per session it re-reads the account
    # from Navidrome's database, and that read sat on the event loop,
    # holding every other request while it ran.
    # No cookie, nothing to look up: /healthz and the sign-in page should not
    # wait for a thread to learn that.
    cookie = request.cookies.get(auth.COOKIE)
    session = await asyncio.to_thread(auth.get, cookie) if cookie else None
    request.state.session = session
    path = request.url.path
    if request.method in CHANGING and not same_origin(request.headers):
        return JSONResponse({"detail": "That came from another page."},
                            status_code=403)
    gated = (path.startswith("/api/") and path not in OPEN_PATHS) or path in DOC_PATHS
    if gated and session is None:
        return JSONResponse({"detail": "Please sign in."}, status_code=401)
    response = await call_next(request)
    if session is not None and auth.cookie_due(session) and path != "/api/auth/logout":
        _send_cookie(response, request, session)
    return response


def _send_cookie(response: Response, request: Request,
                 session: auth.Session) -> None:
    # Secure only when the request actually arrived over TLS. Setting it
    # unconditionally would stop the cookie being stored at all on the plain
    # HTTP this is normally served over on a LAN.
    response.set_cookie(
        auth.COOKIE, session.id, httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
        max_age=auth.cookie_age(session),
    )
    session.cookie_sent_at = time.time()


# Failed sign-ins allowed per address in a window. Navidrome has its own
# limits, but this endpoint is open to anyone who can reach the port, and
# each attempt is a request to Navidrome on this person's behalf.
SIGN_IN_FAILURES = 10
SIGN_IN_WINDOW = 10 * 60
_sign_in_failures: dict[str, list[float]] = {}


def _recent_failures(address: str) -> list[float]:
    cutoff = time.time() - SIGN_IN_WINDOW
    kept = [t for t in _sign_in_failures.get(address, []) if t > cutoff]
    if kept:
        _sign_in_failures[address] = kept
    else:
        _sign_in_failures.pop(address, None)
    return kept


@app.post("/api/auth/login")
async def sign_in(request: Request, body: LoginRequest,
                  response: Response) -> dict[str, Any]:
    address = request.client.host if request.client else "?"
    if len(_recent_failures(address)) >= SIGN_IN_FAILURES:
        raise HTTPException(
            status_code=429,
            detail="Too many failed sign-ins. Wait a few minutes and try again.")
    try:
        session = await asyncio.to_thread(
            auth.sign_in, body.username, body.password)
    except navidrome.LoginFailed as exc:
        _sign_in_failures.setdefault(address, []).append(time.time())
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except navidrome.NotConfigured as exc:
        log.warning("sign-in refused: Navidrome is not configured: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Navidrome is not configured. An administrator needs to "
                   "set its address.") from exc
    except Exception as exc:
        # The reason stays in the log. It names internal hosts and ports,
        # and this answer goes to anyone, signed in or not.
        log.warning("sign-in could not reach Navidrome: %s", exc)
        raise HTTPException(
            status_code=502, detail="Could not reach Navidrome.") from exc
    _sign_in_failures.pop(address, None)

    _send_cookie(response, request, session)
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
                       space: workspace.Workspace) -> None:
    """Resolve a link off the event loop, then announce the result."""
    try:
        kind, title, tracks = await threads.run(_resolve, url)
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


@app.post("/api/jobs")
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
    await asyncio.to_thread(space.prepare)

    # From the count to the new job with no await between them, so two
    # requests at once cannot both see room for one more.
    _check_room(session.identity.username)
    _evict_old_jobs(session.identity.username)
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
    # Everything that can refuse comes before anything is reset, so a
    # refused retry leaves the job as it was, failures and all.
    try:
        space = await asyncio.to_thread(
            workspace.for_session, session.identity, job.get("library_id"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _check_room(session.identity.username)

    for item in retryable:
        item.update(status="pending", error=None, progress=0, attempts=0)
    job.update(status="queued", error=None)
    await push_job(job)
    # Tracked now, not when _run first runs, so a cancel pressed straight
    # away finds it.
    RUNNING[job_id] = asyncio.create_task(_run(job, space))
    return {"retrying": len(retryable)}


# --- inbox uploads ----------------------------------------------------------
# The third way music gets into the library, beside a download and a folder
# dragged onto the network share: picked or dropped in a browser. It reuses
# the inbox's own filing rather than adding a second one - see
# `inbox.upload_root` and `inbox.backdate` for the only two things that are
# actually new here.

# A FLAC track comfortably clears 40MB; refused well past that rather than
# guessed from a bitrate.
# Read and written a megabyte at a time, never whole.
UPLOAD_CHUNK = 1 << 20
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def _relpath_segments(relpath: str, filename: str | None) -> list[str]:
    """A browser-supplied path, broken into filesystem-safe components.

    `relpath` carries a dragged folder's structure when there is one (the
    browser has no equivalent for a plain file picker, so `filename` is the
    fallback). Sanitising component by component, after splitting, is what
    keeps a ``..`` segment from walking out of its upload folder - sanitizing
    the joined string would only turn its slashes into underscores and leave
    the dots untouched.

    A folder whose name starts with a dot loses the dot: the poller walks
    past hidden folders, so nothing uploaded under one was ever filed. The
    file's own name keeps it, for the caller to refuse.
    """
    raw = [part for part in (relpath or filename or "").replace("\\", "/").split("/")
           if part.strip()]
    if not raw:
        return []
    folders = [part.lstrip(".") for part in raw[:-1]]
    name = raw[-1]
    # sanitize() would turn the dot into an underscore and file the junk.
    hidden = name.startswith(".") and not name.startswith("..")
    last = f".{filer.sanitize(name[1:])}" if hidden else filer.sanitize(name)
    return [filer.sanitize(part) for part in folders if part.strip()] + [last]


@app.post("/api/inbox/upload")
async def upload_to_inbox(
    file: UploadFile = File(...),
    relpath: str = Form(""),
    batch: str | None = Form(None),
    library_id: int | None = Form(None),
    session: auth.Session = Depends(current_session),
) -> dict[str, str]:
    try:
        space = await asyncio.to_thread(
            workspace.for_session, session.identity, library_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    segments = _relpath_segments(relpath, file.filename)
    if not segments:
        raise HTTPException(status_code=400, detail="No filename given.")
    name = segments[-1]
    if name.startswith(".") and not name.startswith(".."):
        # macOS's ._ companions, mostly. Hidden from the poller, so it
        # would sit in the inbox for ever.
        raise HTTPException(
            status_code=400, detail=f"{name}: a hidden file, not filed.")
    suffix = Path(name).suffix.lower()
    if not (uuidtags.is_audio(Path(name)) or suffix in filer.COVER_SUFFIXES):
        raise HTTPException(
            status_code=400, detail=f"{name}: not something this can file.")

    # Checked before anything is read, and again while copying: the whole
    # upload used to be read into memory first - 200 MB a file, more since
    # the check came after, on a Raspberry Pi, several at once for a folder.
    too_big = HTTPException(
        status_code=413, detail=f"{name} is larger than this accepts.")
    if (getattr(file, "size", None) or 0) > MAX_UPLOAD_BYTES:
        await file.close()
        raise too_big

    await asyncio.to_thread(space.prepare)

    # Server-generated on the first file of a drop and echoed back by every
    # later one in the same drop, rather than trusted from the browser - it
    # ends up as a path component, and a made-up value is cheap to sanitise
    # but has no reason to be trusted in the first place.
    batch = filer.sanitize(batch or uuid.uuid4().hex)
    target = inbox.upload_root(space, batch).joinpath(*segments)

    def write() -> int:
        """Copied in chunks to a hidden part file, then renamed into place,
        so the poller never sees half of it under its real name."""
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.part")
        written = 0
        try:
            with open(partial, "wb") as out:
                while chunk := file.file.read(UPLOAD_CHUNK):
                    written += len(chunk)
                    if written > MAX_UPLOAD_BYTES:
                        break
                    out.write(chunk)
            if 0 < written <= MAX_UPLOAD_BYTES:
                # Not backdated here: a track backdated as it landed was
                # filed by the poller mid-upload, before the album's cover
                # (which often comes last) had arrived. `finish` releases
                # the whole drop at once.
                os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return written

    try:
        written = await asyncio.to_thread(write)
    finally:
        await file.close()
    if not written:
        raise HTTPException(status_code=400, detail=f"{name} is empty.")
    if written > MAX_UPLOAD_BYTES:
        raise too_big
    return {"batch": batch}


@app.post("/api/inbox/upload/finish")
async def finish_upload(
    library_id: int | None = None,
    batch: str | None = None,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """File whatever has landed, instead of waiting for the next poll.

    Drains the whole inbox, not just the drop that just finished - anything
    else sitting there is this same person's, and there is no reason to make
    it wait. The regular poller would file it anyway within `POLL_SECONDS`;
    this only saves the browser watching a batch sit at "filing" for it.
    """
    try:
        space = await asyncio.to_thread(
            workspace.for_session, session.identity, library_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if batch:
        # Every file of this drop has arrived, so it can be filed now, all
        # together, rather than after the quiet period.
        await asyncio.to_thread(inbox.release, space, filer.sanitize(batch))
    result = await threads.run(inbox.drain, space)
    if result.changed:
        await asyncio.to_thread(navidrome.notify)
    return {
        "filed": [str(path.relative_to(space.library_path))
                  for path in result.filed],
        "failures": result.failures,
        "waiting": result.waiting,
    }


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
    # Set by the container's environment: shown, but not editable here.
    values["locked"] = {key: config.env_var(key) for key in config.EDITABLE
                        if key in config.FROM_ENV}
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
    locked = sorted(key for key in changes if key in config.FROM_ENV)
    if locked:
        raise HTTPException(
            status_code=400,
            detail=f"{locked[0]} is set by {config.env_var(locked[0])} in the "
                   "container's environment; change it there.")

    try:
        # Validate against the model before touching the live settings.
        validated = settings.__class__(**{**settings.model_dump(), **changes})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc).split(chr(10))[0]) from exc
    # What is stored is what the model made of it, not what was typed: "320k"
    # validates because the model drops the k, and the raw string then failed
    # every download until a restart read it back through the model.
    changes = {key: getattr(validated, key) for key in changes}

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


def _mark_albums_held(cards: list[dict[str, Any]],
                      library_id: int) -> list[dict[str, Any]]:
    """Say how many of each album's tracks the library already holds.

    `held_tracks` on every card, capped at the album's own length: a deluxe
    edition filed under the plain title would otherwise claim fourteen of
    eleven. Both credits are tried, as for a track, and the larger count
    wins - they are the same album filed two ways, not two albums.
    """
    counts = navidrome.albums_held_in(library_id)
    for card in cards:
        name = card.get("name") or ""
        credits = {card.get("artist") or "", card.get("primary_artist") or ""}
        held = max((counts.get(registry.album_key(credit, name), 0)
                    for credit in credits if credit), default=0)
        total = card.get("total")
        card["held_tracks"] = min(held, total) if total else held
    return cards


def _browsing_library(session: auth.Session) -> int | None:
    """Which library the Browse tab is reporting against.

    None when the account has none, in which case nothing is marked held
    rather than everything being marked held against library zero.
    """
    # The first library, as a workspace would choose - without building one,
    # which reads its marker off disk on the event loop.
    libraries = session.identity.libraries
    return libraries[0]["id"] if libraries else None


@app.get("/api/search")
async def search(q: str, type: str = "album", limit: int = 24,
                 session: auth.Session = Depends(current_session),
                 ) -> dict[str, Any]:
    query = q.strip()
    if not query:
        if type == "all":
            return {"type": type, "albums": [], "tracks": [], "artists": []}
        return {"type": type, "results": []}
    # Spotify rejects anything over fifty, which arrived as an unexplained
    # 502. Clamped here so a hand-edited URL is answered rather than blamed
    # on Spotify.
    limit = max(1, min(limit, 50))
    library_id = _browsing_library(session)

    def mark(albums: list, tracks: list) -> None:
        if library_id is None:
            return
        if albums:
            _mark_albums_held(albums, library_id)
        if tracks:
            _mark_held(tracks, library_id)

    try:
        if type == "all":
            found = await asyncio.to_thread(spotify.browse_all, query, limit)
        else:
            results = await asyncio.to_thread(spotify.browse, query, type, limit)
    except spotify.ResolveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Spotify error: {exc}") from exc

    if type == "all":
        await asyncio.to_thread(mark, found["albums"], found["tracks"])
        return {"type": type, **found}
    await asyncio.to_thread(mark, results if type == "album" else [],
                            results if type == "track" else [])
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
async def artist(artist_id: str,
                 session: auth.Session = Depends(current_session),
                 ) -> dict[str, Any]:
    try:
        data = await asyncio.to_thread(spotify.artist_albums, artist_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Artist not found: {exc}") from exc
    library_id = _browsing_library(session)
    if library_id is not None:
        await asyncio.to_thread(_mark_albums_held, data["albums"], library_id)
    return data


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
    key: str = Field(max_length=64)
    note: str = Field(default="", max_length=500)


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
    # Only a group this person can see: any key at all used to be accepted,
    # from anyone, with a note of any size.
    groups = await asyncio.to_thread(_duplicate_groups, session.identity)
    if not any(g.dismiss_key == request.key for g in groups):
        raise HTTPException(status_code=404, detail="No such duplicate group.")
    await asyncio.to_thread(store.dismiss_duplicate, request.key, request.note,
                            session.identity.user_id)
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
async def auto_resolve_preview(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """What resolving the confident groups would do: their keys and keepers,
    which /auto/apply then acts on exactly."""
    def run() -> dict[str, Any]:
        connection = navidrome.open_db()
        with connection:
            return duplicates.auto_resolve(connection, session.identity)

    try:
        return await asyncio.to_thread(run)
    except navidrome.Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class AutoResolve(BaseModel):
    # The preview's groups, as it returned them: {"key": ..., "keeper": ...}.
    groups: list[dict[str, str]]


@app.post("/api/duplicates/auto/apply")
async def auto_resolve_apply(
    body: AutoResolve,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Resolve exactly the previewed groups, as an operation.

    Refused while music is still arriving: importing is what creates
    duplicates, so a list taken mid-import is stale by the end. An operation
    because it is up to two Navidrome calls per group, which for a few
    hundred groups is far longer than a request should be held open.
    """
    identity = session.identity
    busy = [job for job in JOBS.values()
            if job.get("owner") == identity.username and job["id"] in RUNNING]
    arriving = await asyncio.to_thread(
        lambda: any(inbox.receiving(Path(lib["path"]))
                    for lib in identity.libraries))
    if busy or arriving:
        raise HTTPException(
            status_code=409,
            detail="Music is still being filed into your library. Resolve "
                   "duplicates once it has finished - importing is what "
                   "creates them.")
    chosen = {g["key"]: g["keeper"] for g in body.groups
              if g.get("key") and g.get("keeper")}

    def run() -> dict[str, Any]:
        connection = navidrome.open_db()
        with connection:
            outcome = duplicates.auto_resolve(connection, identity, chosen)
        # Once for the whole run rather than once per group: a scan per
        # resolved duplicate would be a hundred scans of the same library.
        if outcome.get("resolved"):
            navidrome.notify()
        return outcome

    operation, started = operations.start("dupes-auto", identity.username, run)
    return {"started": started, "operation": operation.as_dict()}


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
    except navidrome.SessionExpired as exc:
        _end_session(session, exc)
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
    try:
        owned = await asyncio.to_thread(smart_playlists.mine, session.identity)
    except navidrome.SessionExpired as exc:
        _end_session(session, exc)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_navidrome_error(exc)) from exc
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
    except navidrome.SessionExpired as exc:
        _end_session(session, exc)
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
    except navidrome.SessionExpired as exc:
        _end_session(session, exc)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_navidrome_error(exc)) from exc


def _end_session(session: auth.Session, exc: Exception) -> NoReturn:
    """Navidrome has ended this person's sign-in, so this one ends too:
    keeping it would go on answering "Navidrome refused that" for a
    fortnight. A 401, so the page treats it as signed out."""
    auth.sign_out(session.id)
    raise HTTPException(status_code=401, detail=str(exc)) from exc


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
    show: str = "all",
    q: str = "",
    kind: str = "all",
    sort: str = "recent",
    artist: str = "",
    library_id: int | None = None,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Every album this person owns, newest first unless `sort` says not.

    `show` narrows it: `unmatched` to albums MusicBrainz has not confirmed,
    `review` to the ones of those nobody has dealt with yet, `nogain` to
    albums with a track lacking ReplayGain. A filter, not the definition of
    the list - hiding the rest is what made a matched album unreachable
    once it had been matched. `kind` is `album` or `single`, and `artist`
    one album artist exactly, for that artist's page.
    """
    try:
        return await asyncio.to_thread(
            functools.partial(library.listing, session.identity, limit,
                              offset, show, q, kind=kind, sort=sort,
                              artist=artist, library_id=library_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/library/artists")
async def library_artists(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Every album artist this person owns, and how much of each."""
    return await asyncio.to_thread(library.artists, session.identity)


@app.get("/api/library/attention")
async def library_attention(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """What wants a person: singles to combine, albums to review or measure."""
    return await asyncio.to_thread(library.attention, session.identity)


@app.get("/api/library/attention/covers")
async def library_attention_covers(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Every album whose cover is the wrong shape.

    Apart from the rest of the attention list because it opens a file per
    album, which is slow the first time on the Pi - the page shows the
    other sections while this one is still reading.
    """
    try:
        return await asyncio.to_thread(library.cover_survey, session.identity)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/library/genres")
async def library_genres(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """How many tracks carry each genre string, exactly as tagged."""
    return await asyncio.to_thread(library.genre_tally, session.identity)


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


def _library_folder(identity: navidrome.Identity, library_id: int,
                    folder: str) -> Path:
    """A folder of a library this account can see, for locking only - the
    caller has already validated it through `library.tracks`."""
    root = next(Path(lib["path"]) for lib in identity.libraries
                if str(lib["id"]) == str(library_id))
    return root / folder


def _not_arriving(folder: Path) -> None:
    """409 while the inbox is still filing into this album: setting it aside
    now would leave the tracks still on their way behind."""
    if inbox.receiving(folder):
        raise HTTPException(
            status_code=409,
            detail=f"{folder.name} is still arriving; try again shortly.")


def _copy_from_track_row(row: dict[str, Any], library_id: int,
                         album: str, artist: str) -> duplicates.Copy:
    """Enough of duplicates.Copy to quarantine a track by hand.

    The fields duplicates.py ranks copies by - bit rate, duration, a
    MusicBrainz id - do not matter here: nothing is being compared against
    anything else, only moved.
    """
    return duplicates.Copy(
        id=row["id"], path=row["path"], title=row.get("title", ""),
        album=album, artist=artist, suffix="", bit_rate=0, duration=0.0,
        size=0, mbid="", track_artist=artist, starred=False, rating=0,
        library_id=library_id, library="")


class LibraryQuarantine(BaseModel):
    library_id: int
    folder: str
    album: str = ""
    artist: str = ""


@app.post("/api/library/quarantine")
async def api_library_quarantine(
    body: LibraryQuarantine,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Set aside every track in one album row - the wrong record entirely,
    reached from the row itself.

    `library.tracks` is what validates the folder belongs to this account;
    every path quarantined below comes from that trusted read, never from
    the request body.
    """
    try:
        found = await asyncio.to_thread(
            library.tracks, session.identity, body.library_id, body.folder)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    folder = _library_folder(session.identity, body.library_id, body.folder)
    await asyncio.to_thread(_not_arriving, folder)
    copies = [_copy_from_track_row(t, body.library_id, body.album,
                                   t["artist"] or body.artist)
              for t in found["items"]]
    outcome = await _locked_request(
        [folder],
        functools.partial(duplicates.quarantine_many, copies, session.identity))

    if outcome.get("quarantined"):
        await asyncio.to_thread(navidrome.notify)
    return outcome


class TrackQuarantine(BaseModel):
    library_id: int
    folder: str
    track_id: str
    album: str = ""
    artist: str = ""


@app.post("/api/library/track/quarantine")
async def api_track_quarantine(
    body: TrackQuarantine,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Set aside one track by hand - the wrong file entirely, not a worse
    copy of a right one.

    Scoped to the album it claims to be in, the same way
    `/api/library/quarantine` is scoped to a folder: `library.tracks`
    validates ownership, and the track has to be one it actually reports
    there, so a path can only ever come from that trusted read.
    """
    try:
        found = await asyncio.to_thread(
            library.tracks, session.identity, body.library_id, body.folder)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    track = next((t for t in found["items"] if t["id"] == body.track_id), None)
    if track is None:
        raise HTTPException(status_code=404,
                            detail="That track is not in this album.")

    folder = _library_folder(session.identity, body.library_id, body.folder)
    await asyncio.to_thread(_not_arriving, folder)
    copy = _copy_from_track_row(track, body.library_id, body.album,
                                track["artist"] or body.artist)
    try:
        moved = await _locked_request(
            [folder],
            functools.partial(duplicates.quarantine_one, copy, session.identity))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await asyncio.to_thread(navidrome.notify)
    return {"quarantined": [moved], "failed": []}


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


def _before_edit(identity: navidrome.Identity, library_id: int,
                 folder: str) -> set[str]:
    """The album ids to mark reviewed once an edit succeeds.

    Read first, because afterwards the folder may have moved and Navidrome
    has not rescanned. An empty answer does not stop the edit - the edit is
    what was asked for - but it is logged, because the album then stays on
    the review list and somebody will wonder why.
    """
    try:
        return library.album_ids(identity, library_id, folder)
    except ValueError as exc:
        log.warning("cannot mark %s reviewed: %s", folder, exc)
        return set()


def _reviewed(identity: navidrome.Identity, library_id: int,
              ids: set[str], how: str) -> None:
    if ids:
        store.mark_reviewed(library_id, ids, how, identity.username)


class ReviewMark(BaseModel):
    library_id: int
    folder: str
    reviewed: bool = True


@app.post("/api/library/reviewed")
async def library_reviewed(
    body: ReviewMark,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Say an album has been dealt with, or put it back on the list.

    For the albums nothing else will ever mark: tagged correctly by hand,
    and never going to be in MusicBrainz.
    """
    try:
        ids = await asyncio.to_thread(
            library.album_ids, session.identity, body.library_id, body.folder)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.reviewed:
        store.mark_reviewed(body.library_id, ids, "marked",
                            session.identity.username)
    else:
        store.unmark_reviewed(body.library_id, ids)
    return {"reviewed": body.reviewed}


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

    def check() -> tuple[workspace.Workspace, Path]:
        try:
            space = workspace.for_session(session.identity, body.library_id)
            folder = library.album_dir(
                session.identity, body.library_id, body.folder)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _not_arriving(folder)
        return space, folder

    space, folder = await asyncio.to_thread(check)

    def run() -> dict[str, Any]:
        ids = _before_edit(session.identity, body.library_id, body.folder)
        filed = filer.retag_album(space, folder,
                                  albumartist=body.album_artist,
                                  album=body.album)
        _reviewed(session.identity, body.library_id, ids, "edited")
        navidrome.notify()
        return {"ran": True, "moved": len(filed),
                "folder": str(filed[0].path.parent.relative_to(
                    space.library_path)) if filed else body.folder,
                "album_uuid": filed[0].album_uuid if filed else None}

    return await _locked_request([folder], run)


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
    def check() -> tuple[workspace.Workspace, Path]:
        try:
            space = workspace.for_session(session.identity, body.library_id)
            path = library.track_path(session.identity, body.library_id, body.path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _not_arriving(path)
        return space, path

    space, path = await asyncio.to_thread(check)

    def run() -> dict[str, Any]:
        ids = _before_edit(session.identity, body.library_id,
                           library.folder_of(body.path))
        filed = filer.retag_track(
            space, path, albumartist=body.album_artist, album=body.album,
            artist=body.artist, title=body.title,
            track_no=body.track_no, disc_no=body.disc_no)
        # The album it was in: that is the one somebody was going through.
        _reviewed(session.identity, body.library_id, ids, "edited")
        navidrome.notify()
        return {"ran": True,
                "path": str(filed.path.relative_to(space.library_path)),
                # Left this album's folder, so the panel stops listing it.
                "moved": filed.path.parent != path.parent,
                "album_uuid": filed.album_uuid,
                "identified": filed.identified}

    return await _locked_request([path.parent], run)


# Long, because a cover does not change without the file changing, and the
# id is derived from the file. A page of fifty of these is otherwise fifty
# round trips every time somebody scrolls back up. Private: each cover is
# checked against who is asking, and a shared cache in between - a proxy -
# would hand one person's art to anybody asking for the same URL.
ART_CACHE = "private, max-age=604800"


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


def _locked(folders: list[Path], work: Callable[[], Any]) -> Any:
    """Run `work` holding these folders against any other change to them
    (folderlock), or raise folderlock.Busy."""
    with folderlock.holding(*folders):
        return work()


async def _locked_request(folders: list[Path], work: Callable[[], Any]) -> Any:
    """The same, for a change made inside the request: Busy is a 409, and a
    file that cannot be edited a 422."""
    try:
        return await asyncio.to_thread(_locked, folders, work)
    except folderlock.Busy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except filer.NotEditable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _one_album(path: Path) -> None:
    """409 for a folder-wide action on a folder holding several albums."""
    try:
        filer.require_one_album(path)
    except filer.NotEditable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    def check() -> tuple[workspace.Workspace, Path]:
        try:
            space = workspace.for_session(session.identity, body.library_id)
            path = library.album_dir(session.identity, body.library_id, body.folder)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _one_album(path)
        return space, path

    space, path = await asyncio.to_thread(check)

    target = {"library_id": body.library_id, "folder": body.folder}

    def run() -> dict[str, Any]:
        result = beets_runner.candidates(space, path)
        # Named in the answer, because the answer arrives over the websocket
        # long after the request: the page drops a list that is not about
        # the album it is showing, and applying checks the same record.
        result.update(target)
        _offered[(session.identity.username, body.library_id, str(path))] = {
            c["id"] for c in result.get("candidates", []) if c.get("id")}
        return result

    operation, started = operations.start(
        "candidates", session.identity.username, run, target=target)
    return {"started": started, "operation": operation.as_dict()}


# Which releases each album was offered, by (user, library, album folder).
# Applying refuses anything else. A lookup started while another was in
# flight used to be handed that one's answer, drawn under the wrong album,
# and *Use this* then retagged one album as another's release - fusing them.
# In memory: after a restart, finding matches again is the cost.
_offered: dict[tuple[str, int, str], set[str]] = {}


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
    def check() -> tuple[workspace.Workspace, Path]:
        try:
            space = workspace.for_session(session.identity, body.library_id)
            path = library.album_dir(session.identity, body.library_id, body.folder)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        offered = _offered.get(
            (session.identity.username, body.library_id, str(path)), set())
        if body.release_id not in offered:
            raise HTTPException(
                status_code=409,
                detail=f"That release was not offered for {path.name}; "
                       "find matches for it again.")
        _one_album(path)
        # A download of this album still filing tracks into it: retagging
        # mid-flight re-points the registry, and the tracks that land
        # afterwards still carry the old tags, miss the key that has just
        # moved, and found a second album beside the first.
        _not_arriving(path)
        return space, path

    space, path = await asyncio.to_thread(check)

    def run() -> dict[str, Any]:
        # Read before beets touches anything: once the tags are rewritten
        # there is nothing left to say which album this used to be.
        was = filer.album_key_of(path)
        ids = _before_edit(session.identity, body.library_id, body.folder)
        # Before beets writes anything: a file it cannot tag would otherwise
        # leave the album half retagged.
        filer.check_writable(filer.audio_in(path))
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
        # Raises if beets left the files disagreeing about their album; the
        # operation then fails with that message and nothing moves.
        result["album_uuid"] = filer.after_retag(space, retagged, was)
        filed = [filer.file_track(space, one).path for one in retagged]
        result["filed"] = [str(one) for one in filed]
        filer.leave_folder(path, {one.parent for one in filed},
                           space.library_path)
        # Last, so an album whose retag did not go through stays in review.
        _reviewed(session.identity, body.library_id, ids, "matched")
        navidrome.notify()
        return result

    operation, started = operations.start(
        "import", session.identity.username,
        functools.partial(_locked, [path], run))
    return {"started": started, "operation": operation.as_dict()}


class CoverChoice(BaseModel):
    library_id: int
    folder: str
    # One of the URLs `library_cover_candidates` offered, or none at all to
    # square the cover the album already has.
    url: str | None = None


def _cover_target(session: auth.Session, library_id: int,
                  folder: str) -> tuple[Path, list[Path]]:
    try:
        path = library.album_dir(session.identity, library_id, folder)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tracks = filer.audio_in(path)
    if not tracks:
        raise HTTPException(status_code=404, detail="That album has no tracks on disk.")
    return path, tracks


@app.post("/api/library/cover/candidates")
async def library_cover_candidates(
    body: AlbumTarget,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Covers this album could have, to choose between - and nothing else.

    "Find matches" brings art too, but it also rewrites every tag from
    MusicBrainz and can move the files. This is for when the picture is the
    only thing wrong, which is every YouTube download that arrived with bars.
    """
    path, tracks = await asyncio.to_thread(
        _cover_target, session, body.library_id, body.folder)

    def run() -> list[dict[str, Any]]:
        meta = filer.read_meta(tracks[0])
        album = meta.album if meta.names_album else meta.title
        return covers.candidates(path, tracks, meta.albumartist, album)

    return {"candidates": await threads.run(run)}


@app.post("/api/library/cover/apply")
async def library_cover_apply(
    body: CoverChoice,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Put the chosen cover on every track of the album, and nothing else."""
    if body.url is not None and not covers.choosable(body.url):
        raise HTTPException(status_code=400,
                            detail="That cover is not one this offered.")
    def check() -> tuple[Path, list[Path]]:
        path, tracks = _cover_target(session, body.library_id, body.folder)
        _not_arriving(path)
        _one_album(path)
        return path, tracks

    path, tracks = await asyncio.to_thread(check)

    def run() -> dict[str, Any]:
        data = (covers.fetch(body.url, covers.CHOOSABLE_HOSTS) if body.url
                else covers.current(path, tracks))
        if not data:
            raise HTTPException(
                status_code=502 if body.url else 404,
                detail="Could not fetch that cover." if body.url
                else "This album has no cover to square.")
        if not body.url and covers.is_square(data):
            # Squaring a square is every file rewritten for nothing - and a
            # bulk "square these" reaches plenty of covers that already are.
            return {"written": 0, "failed": [], "already_square": True}
        result = covers.apply(path, tracks, data)
        navidrome.notify()
        return result

    return await _locked_request([path], run)


class CombineRequest(BaseModel):
    library_id: int
    albumartist: str
    album: str
    # Whole album folders, and single tracks picked out of other albums -
    # both as the library listing names them.
    albums: list[str] = []
    tracks: list[str] = []
    # The selected album whose identity survives. None lets the first carry
    # it, or an album already called `albumartist`/`album` win if there is one.
    keep: str | None = None
    # Every file, in the order to number them. Empty leaves numbers alone.
    order: list[str] = []
    # One of the selected album folders to take the cover from, or a URL
    # `combine/guess` offered.
    cover_folder: str | None = None
    cover_url: str | None = None


@app.post("/api/library/combine")
async def library_combine(
    body: CombineRequest,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Make several albums and loose tracks into one album.

    An operation, because a combine of three albums is dozens of files each
    retagged and moved, which on the Pi takes longer than a request should
    be held open. Progress and the outcome arrive over the websocket.
    """
    _named(body.albumartist, body.album)
    identity = session.identity

    def resolve() -> tuple[workspace.Workspace, dict[str, Path], dict[str, Path]]:
        try:
            return (workspace.for_session(identity, body.library_id),
                    {name: library.album_dir(identity, body.library_id, name)
                     for name in dict.fromkeys(body.albums)},
                    {name: library.track_path(identity, body.library_id, name)
                     for name in dict.fromkeys(body.tracks + body.order)})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    space, folders, files = await asyncio.to_thread(resolve)

    if body.keep is not None and body.keep not in folders:
        raise HTTPException(status_code=400,
                            detail="The album to keep has to be one of those selected.")
    if body.cover_folder is not None and body.cover_folder not in folders:
        raise HTTPException(status_code=400,
                            detail="The cover has to come from one of those selected.")
    if body.cover_url is not None and not covers.choosable(body.cover_url):
        raise HTTPException(status_code=400,
                            detail="That cover is not one this offered.")

    def check() -> None:
        # Distinct files: a track picked out of a folder that is itself
        # selected, or named twice, is still one track.
        chosen = {one.resolve() for path in folders.values()
                  for one in filer.audio_in(path)}
        chosen.update(files[name].resolve() for name in body.tracks)
        if len(chosen) < 2:
            raise HTTPException(status_code=400,
                                detail="Choose at least two tracks to combine.")
        for path in [*folders.values(), *(files[name] for name in body.tracks)]:
            _not_arriving(path)
        for path in folders.values():
            _one_album(path)

    await asyncio.to_thread(check)

    def run() -> dict[str, Any]:
        survivor = body.keep or next(iter(folders), None)
        ids = (_before_edit(identity, body.library_id, survivor)
               if survivor else set())
        # Read before anything moves: the folder it lives in may not
        # survive the combine.
        cover = None
        if body.cover_url:
            cover = covers.fetch(body.cover_url, covers.CHOOSABLE_HOSTS)
        elif body.cover_folder:
            path = folders[body.cover_folder]
            cover = covers.current(path, filer.audio_in(path))
        result = combine.combine(
            space, albumartist=body.albumartist.strip(),
            album=body.album.strip(),
            albums=list(folders.values()),
            tracks=[files[name] for name in body.tracks],
            keep=folders.get(body.keep) if body.keep else None,
            order=[files[name] for name in body.order],
            cover=cover,
            report=functools.partial(operations.report, "combine",
                                     identity.username))
        _reviewed(identity, body.library_id, ids, "edited")
        navidrome.notify()
        return result

    operation, started = operations.start(
        "combine", identity.username,
        functools.partial(_locked, [*folders.values(),
                                    *(files[name].parent for name in body.tracks)],
                          run))
    return {"started": started, "operation": operation.as_dict()}


class CombineGuess(BaseModel):
    artist: str
    titles: list[str]


@app.post("/api/library/combine/guess")
async def library_combine_guess(
    body: CombineGuess,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Which album these songs are probably from, to name the combine.

    A suggestion for the form, never applied by itself: an empty answer is
    normal, and the form just leaves the name for a person to type.
    """
    guess = await asyncio.to_thread(combine.guess_album, body.artist,
                                    body.titles)
    return {"guess": guess}


class GainTarget(BaseModel):
    # Both absent: every album in this person's libraries with a track that
    # has no ReplayGain.
    library_id: int | None = None
    folder: str | None = None


@app.post("/api/library/replaygain")
async def library_replaygain(
    body: GainTarget,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Measure ReplayGain for one album, or for every album missing some.

    An operation, reporting progress album by album: "all missing" was
    about 3,400 tracks when this was written, which is a long time on a Pi
    and indistinguishable from a hang without a count going up.
    """
    if not replaygain.available():
        raise HTTPException(
            status_code=503,
            detail="rsgain is not installed in this container, so nothing "
                   "can be measured.")
    identity = session.identity
    try:
        if body.folder is not None:
            if body.library_id is None:
                raise ValueError("Say which library the album is in.")
            await asyncio.to_thread(functools.partial(
                library.album_dir, identity, body.library_id, body.folder,
                any_depth=True))
            targets = [(body.library_id, body.folder)]
        else:
            targets = await asyncio.to_thread(library.without_gain, identity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not targets:
        raise HTTPException(status_code=400,
                            detail="Every album already has ReplayGain.")

    def run() -> dict[str, Any]:
        return replaygain.measure_all(identity, targets)

    operation, started = operations.start(
        replaygain.NAME, identity.username, run)
    return {"started": started, "operation": operation.as_dict()}


@app.post("/api/library/replaygain/stop")
async def library_replaygain_stop(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Finish the album in hand, then stop. Only your own run: there is no
    way to name anybody else's."""
    return {"operation": operations.stop(
        replaygain.NAME, session.identity.username).as_dict()}


@app.get("/api/operations")
async def list_operations(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Your long-running work in flight, and how your last runs went.

    Filtered here rather than in the browser: results carry folder names,
    paths and candidate lists, which are as private as the library."""
    return {"operations": operations.all_operations(session.identity.username)}


@app.get("/api/playcounts")
async def playcount_status(
    session: auth.Session = Depends(admin_session),
) -> dict[str, Any]:
    """Whether snapshots are actually being taken.

    The thing that must not fail quietly is the collecting, and that is
    checkable tonight - long before there is enough history to say anything
    interesting with.

    An administrator's: the collector is the installation's, and its totals
    sum every account's imported plays - on a two-person install, the other
    person's listening.
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
    start: str | None = None,
    end: str | None = None,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Everything the Listening panel shows for one window: the signed-in
    person's most played tracks, albums and genres, their listening by hour
    of day, and their longest session - all over the same range.

    Theirs alone. Play counts are per Navidrome account, and one person's
    listening is not another's to read - the same rule the rest of this
    application follows.
    """
    days = max(1, min(days, MAX_LISTENING_DAYS))
    limit = max(1, min(limit, MAX_LISTENING_TRACKS))
    # A chosen range, both ends inclusive, instead of "the last N days".
    # Both or neither: half a range is a typo, not a request.
    if (start is None) != (end is None):
        raise HTTPException(status_code=422,
                            detail="Give both a start and an end date.")
    if start is not None:
        try:
            first = datetime.strptime(start, "%Y-%m-%d")
            last = datetime.strptime(end, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(status_code=422,
                                detail="Dates must be YYYY-MM-DD.") from exc
        if first > last:
            raise HTTPException(status_code=422,
                                detail="The start date is after the end date.")
        days = (last - first).days + 1

    def collect() -> dict[str, Any]:
        if start is not None:
            window_start, window_end = start, end
        else:
            # Today, not yesterday. The window stopped at the last *complete*
            # day because a nightly reading could not describe a day still
            # going on; reading every few minutes can, and the panel was
            # otherwise unable to show anything played since midnight.
            window_end = playcounts.today()
            window_start = overview.days_back(window_end, days)
        return _listening_window(window_start, window_end, days)

    def _listening_window(start: str, end: str, days: int) -> dict[str, Any]:
        user_id = session.identity.user_id
        # The statistics are kept until a new play arrives or the track
        # index changes; the coverage is not, because "is it collecting"
        # and "last read at" are about the clock rather than the history.
        return {
            **overview.window(user_id, start, end, days, limit),
            # Theirs, not the installation's. status() counts every account's
            # imported history together, which shown to someone who has never
            # played anything is both baffling and none of their business.
            "coverage": playcounts.coverage(user_id),
        }

    return await asyncio.to_thread(collect)


@app.post("/api/playcounts/snapshot")
async def playcount_snapshot(
    session: auth.Session = Depends(admin_session),
) -> dict[str, Any]:
    """Take one now rather than waiting for the timer. Admin only: it reads
    every account's listening, not just the caller's.

    Warms Home for whoever it found new plays for, as the timed reading
    does - otherwise their next visit pays for the statistics this just
    made stale.
    """
    taken = await threads.run(playcounts.take)
    if taken.get("users"):
        await threads.run(overview.warm, taken["users"])
    return taken


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
    session = await asyncio.to_thread(auth.get, ws.cookies.get(auth.COOKIE))
    # A socket is not bound by CORS: any page could open one with this
    # person's cookie and read their downloads as they move.
    if session is not None and not same_origin(ws.headers):
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


ASSETS = ("js/main.js", "style.css")


@functools.lru_cache(maxsize=1)
def asset_version() -> str:
    """A token that changes when the assets do.

    Appended to their URLs, so a new deploy asks for a URL the browser has
    never seen and cannot have a stale copy of. The headers below say to
    revalidate, but a browser already holding a heuristically-fresh copy does
    not ask - it has no reason to - so headers alone cannot rescue a browser
    that is already wrong. A new URL can.

    Hashes every module under static/js, not just the one file ASSETS stamps
    a URL for (main.js) - main.js is the only file index.html references
    directly, but a change to any module it imports should still bump the
    token, or this claims to track "the assets" while actually tracking one
    of them.

    Computed once: the files cannot change inside a running container.
    """
    digest = hashlib.sha256()
    for path in sorted((STATIC_DIR / "js").glob("*.js")):
        digest.update(path.read_bytes())
    digest.update((STATIC_DIR / "style.css").read_bytes())
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
