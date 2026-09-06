"""HTTP API, WebSocket event stream, and static file serving."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from fastapi import (Depends, FastAPI, HTTPException, Request, Response,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, beets_runner, diskaudit, duplicates
from . import generic, ledger, navidrome, operations
from . import playlists as smart_playlists
from . import spotify, staging, worker, workspace
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
# straight to JSON for both the REST API and the socket, and everything that
# needs to survive a restart lives in the ledger instead.

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
        # job so every later phase - staging, tagging, filing - agrees.
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
        "beets": None,
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
    ledger.connect(settings.ledger_path)
    operations.subscribe(push_operation)
    log.info("staging directory: %s", settings.output_dir)
    log.info("ledger holds %d previously downloaded track(s)", ledger.count())
    if not settings.spotify_configured:
        log.warning("Spotify credentials missing - add them to config/config.toml")

    background = [asyncio.create_task(_audit_loop()),
                  asyncio.create_task(_sweep_loop())]
    try:
        yield
    finally:
        for task in background:
            task.cancel()
        for task in background:
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _sweep_loop() -> None:
    """Import anything that arrives in staging without a download job.

    Files reach staging by other routes - dropped in by hand, copied from
    elsewhere, or left behind by a job that finished while beets was busy.
    Without this they would sit there forever, which was survivable only while
    something outside this process was also running beets.
    """
    while True:
        minutes = settings.staging_sweep_minutes
        if minutes <= 0:
            await asyncio.sleep(300)
            continue
        await asyncio.sleep(minutes * 60)
        try:
            result = await asyncio.to_thread(beets_runner.sweep_staging)
            if result.get("ran"):
                log.info("staging sweep: %s", result)
        except Exception:
            log.exception("staging sweep failed")


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
            if item["status"] not in ("complete", "skipped", "failed"):
                item["status"] = "cancelled"
        await asyncio.to_thread(staging.discard, space, job_id)
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
        # A single-user installation predates accounts; the first person to
        # queue something inherits it rather than starting an empty index
        # beside a full one.
        workspace.adopt_legacy(space)
        workspace.adopt_legacy_staging(space)
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
    JOBS.pop(job_id, None)
    await asyncio.to_thread(staging.discard, space, job_id)
    await broker.publish({"type": "job_deleted", "id": job_id},
                         owner=session.identity.username)
    return {"ok": True}


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

    # Tracks that already succeeded are in the ledger, so the worker skips
    # them on its own; only the failures need resetting.
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
    staging_sweep_minutes: int | None = None
    # Listed in config.EDITABLE and returned by GET, so it has to be settable
    # or the two disagree about what "editable" means.
    beets_enabled: bool | None = None


# Values the browser must never be sent back. Reported as a boolean instead,
# so a form can show whether one is set without ever holding it.
SECRETS = ("spotify_client_secret", "navidrome_password")


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
    """Flag tracks already in this person's library.

    Only Spotify ids are checked, not ISRCs: search results do not reliably
    carry one, and a per-card lookup would be the wrong place to pay for it.
    The worker still does the full check before downloading anything.

    Scoped to the same library the queue would file into, so the badge means
    the same thing as the skip. Reporting what somebody else holds would say
    "you have this" about a record in a collection you cannot see.
    """
    for card in cards:
        card["held"] = ledger.already_downloaded(card["id"], None, library_id)
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


class ForgetRequest(BaseModel):
    source_id: str


@app.post("/api/ledger/forget")
async def forget_track(
    request: ForgetRequest,
    session: auth.Session = Depends(current_session),
) -> dict[str, bool]:
    """Let a track be downloaded again.

    The ledger is the only record that a track was ever fetched, because
    beets moved the file out of staging. So a file that leaves the library -
    deleted, lost, replaced by hand - leaves the track permanently
    unfetchable with nothing to say why. Scoped to this person's own
    library, like every other answer the ledger gives.
    """
    try:
        # A ledger row, not a file. Nothing here needs the library on disk.
        space = workspace.for_session(session.identity, require_library=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    forgotten = await asyncio.to_thread(
        ledger.forget, request.source_id, space.library_id)
    if not forgotten:
        raise HTTPException(
            status_code=404,
            detail="That track is not recorded as downloaded here.")
    log.info("%s forgot %s in library %s", session.identity.username,
             request.source_id, space.library_id)
    return {"forgotten": True}


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
        return await asyncio.to_thread(
            duplicates.resolve, group, request.keeper, session.identity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/duplicates/dismiss")
async def dismiss_duplicate(
    request: DismissRequest,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    await asyncio.to_thread(ledger.dismiss_duplicate, request.key, request.note)
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
        return await asyncio.to_thread(run)
    except navidrome.Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


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


# --- staging -------------------------------------------------------------

@app.get("/api/staging")
async def staging_contents(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """What is sitting in this person's staging area.

    There is no separate list of work to do. Beets moves out everything it
    can match, so whatever remains here is by definition something it
    refused - a queue that cannot fall out of step with reality, because it
    is the reality.
    """
    def collect() -> dict[str, Any]:
        space = workspace.for_session(session.identity)
        # Written together, always. A staging folder with an owner marker but
        # no beets config is one the sweep would later adopt with a guessed
        # destination.
        beets_runner.ensure_config(space)
        entries = []
        for kind, parent in (("album", space.albums_dir),
                             ("single", space.singles_dir)):
            if not parent.is_dir():
                continue
            for entry in sorted(parent.iterdir()):
                if entry.name.startswith("."):
                    continue
                files = ([f for f in entry.rglob("*") if f.is_file()]
                         if entry.is_dir() else [entry])
                entries.append({
                    "kind": kind,
                    "name": entry.name,
                    "tracks": len(files),
                    "bytes": sum(f.stat().st_size for f in files),
                    "age_days": round(
                        (time.time() - entry.stat().st_mtime) / 86400, 1),
                    "settled": beets_runner.settled(entry),
                })
        return {"library": space.library_name,
                "staging": str(space.staging), "entries": entries}

    try:
        return await asyncio.to_thread(collect)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/staging/import")
async def staging_import(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Try to import everything waiting, now rather than on the timer.

    Started, not awaited: beets gets 900 seconds *per path*, so a staging
    area with a dozen items could hold a request open for hours. The result
    arrives over the websocket and is readable from /api/operations.
    """
    try:
        space = workspace.for_session(session.identity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def run() -> dict[str, Any]:
        waiting = beets_runner.waiting_in(space)
        if not waiting:
            return {"ran": False, "reason": "nothing waiting"}
        return beets_runner.import_paths(space, waiting)

    operation, started = operations.start(
        "import", session.identity.username, run)
    return {"started": started, "operation": operation.as_dict()}


@app.get("/api/operations")
async def list_operations(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """What long-running work is in flight, and how the last run went."""
    return {"operations": operations.all_operations()}


@app.get("/api/status")
async def status(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    # Scoped like everything else the ledger answers: a count of what this
    # person's library holds, not of the whole installation.
    library_id = _browsing_library(session)
    return {
        "spotify_configured": settings.spotify_configured,
        "output_dir": str(settings.output_dir),
        "concurrency": settings.concurrency,
        "ledger_count": ledger.count(library_id),
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


@app.get("/")
async def index() -> FileResponse:
    # Never cached. The shell decides whether to show the sign-in form, so a
    # browser holding yesterday's copy carries on as though the application
    # still had no accounts - and never asks for the new one, because it has
    # no reason to. The assets it references are revalidated normally.
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
