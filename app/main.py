"""HTTP API, WebSocket event stream, and static file serving."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import (Depends, FastAPI, HTTPException, Request, Response,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, beets_library, beets_runner, diskaudit, duplicates
from . import generic, ledger, navidrome
from . import spotify, staging, worker
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_job(url: str) -> dict[str, Any]:
    job = {
        "id": uuid.uuid4().hex[:12],
        "source_url": url,
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
    """Pushes state changes to every connected browser.

    Clients are receive-only: all mutations go through the REST API, so a
    dropped socket costs nothing beyond a fresh snapshot on reconnect.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def publish(self, message: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._clients)
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:
                await self.unregister(ws)


broker = Broker()


async def push_job(job: dict[str, Any]) -> None:
    await broker.publish({"type": "job", "job": job})


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    ledger.connect(settings.ledger_path)
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
    session = auth.get(request.cookies.get(auth.COOKIE))
    if session is None:
        raise HTTPException(status_code=401, detail="Please sign in.")
    return session


# Signing in is the only thing you can do without being signed in. Everything
# else is gated here rather than endpoint by endpoint: this tool queues
# downloads, edits settings and quarantines files, and an authorisation check
# that has to be remembered per route is one that will eventually be missed.
OPEN_PATHS = {"/api/auth/login", "/api/auth/logout", "/api/auth/me"}


@app.middleware("http")
async def require_session(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in OPEN_PATHS:
        if auth.get(request.cookies.get(auth.COOKIE)) is None:
            return JSONResponse({"detail": "Please sign in."}, status_code=401)
    return await call_next(request)


@app.post("/api/auth/login")
async def sign_in(body: LoginRequest, response: Response) -> dict[str, Any]:
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

    response.set_cookie(
        auth.COOKIE, session.id, httponly=True, samesite="lax",
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


async def _resolve_job(job: dict[str, Any], url: str) -> None:
    """Resolve a link off the event loop, then announce the result."""
    try:
        kind, title, tracks = await asyncio.to_thread(_resolve, url)
    except (spotify.ResolveError, generic.ResolveError) as exc:
        job.update(status="failed", error=str(exc))
        log.warning("resolve failed for %s: %s", url, exc)
    except Exception as exc:
        job.update(status="failed", error=f"Resolve error: {exc}")
        log.exception("unexpected resolve failure for %s", url)
    else:
        job.update(
            kind=kind,
            title=title,
            status="queued",
            items=[new_item(track) for track in tracks],
        )
        log.info("resolved %s -> %d track(s)", title, len(tracks))
        await push_job(job)
        await _run(job)
        return
    await push_job(job)


async def _run(job: dict[str, Any]) -> None:
    """Drive a job to completion, tracking the task so it can be cancelled."""
    job_id = job["id"]
    RUNNING[job_id] = asyncio.current_task()
    try:
        await worker.run_job(job, push_job)
    except asyncio.CancelledError:
        job["status"] = "cancelled"
        for item in job["items"]:
            if item["status"] not in ("complete", "skipped", "failed"):
                item["status"] = "cancelled"
        await asyncio.to_thread(staging.discard, job_id)
        log.info("job %s cancelled", job_id)
        await push_job(job)
    except Exception as exc:
        job.update(status="failed", error=f"Download failed: {exc}"[:300])
        log.exception("job %s failed", job_id)
        await push_job(job)
    finally:
        RUNNING.pop(job_id, None)


@app.post("/api/jobs")
async def create_job(request: JobRequest) -> dict[str, str]:
    url = request.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="No link provided.")
    # Validate before creating the job, so a typo does not litter the list.
    try:
        validate(url)
    except (spotify.ResolveError, generic.ResolveError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = new_job(url)
    await push_job(job)
    asyncio.create_task(_resolve_job(job, url))
    return {"id": job["id"]}


@app.get("/api/jobs")
async def list_jobs() -> list[dict[str, Any]]:
    return sorted_jobs()


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    return job


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict[str, bool]:
    JOBS.pop(job_id, None)
    await asyncio.to_thread(staging.discard, job_id)
    await broker.publish({"type": "job_deleted", "id": job_id})
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, bool]:
    task = RUNNING.get(job_id)
    if task is None:
        raise HTTPException(status_code=409, detail="That job is not running.")
    task.cancel()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str) -> dict[str, int]:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    if job_id in RUNNING:
        raise HTTPException(status_code=409, detail="That job is still running.")

    # Tracks that already succeeded are in the ledger, so the worker skips
    # them on its own; only the failures need resetting.
    retryable = [i for i in job["items"] if i["status"] in ("failed", "cancelled")]
    if not retryable:
        raise HTTPException(status_code=409, detail="Nothing to retry.")
    for item in retryable:
        item.update(status="pending", error=None, progress=0, attempts=0)

    job.update(status="queued", error=None)
    await push_job(job)
    asyncio.create_task(_run(job))
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


# Values the browser must never be sent back. Reported as a boolean instead,
# so a form can show whether one is set without ever holding it.
SECRETS = ("spotify_client_secret", "navidrome_password")


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    values = {key: getattr(settings, key) for key in config.EDITABLE}
    for key in SECRETS:
        values[key] = ""
        values[f"{key}_set"] = bool(getattr(settings, key))
    return values


@app.put("/api/settings")
async def put_settings(update: SettingsUpdate) -> dict[str, Any]:
    changes = {k: v for k, v in update.model_dump().items() if v is not None}
    # A blank secret means "leave it alone", since the form never receives it.
    for key in SECRETS:
        if not changes.get(key):
            changes.pop(key, None)
    if not changes:
        return await get_settings()

    try:
        # Validate against the model before touching the live settings.
        settings.__class__(**{**settings.model_dump(), **changes})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc).split(chr(10))[0]) from exc

    await asyncio.to_thread(config.save, changes)
    if "spotify_client_id" in changes or "spotify_client_secret" in changes:
        spotify.reset_client()
    log.info("settings updated: %s", ", ".join(sorted(changes)))
    return await get_settings()


# --- browsing -------------------------------------------------------------

def _mark_held(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag tracks already in the ledger.

    Only Spotify ids are checked, not ISRCs: search results do not reliably
    carry one, and a per-card lookup would be the wrong place to pay for it.
    The worker still does the full check before downloading anything.
    """
    for card in cards:
        card["held"] = ledger.already_downloaded(card["id"], None)
    return cards


@app.get("/api/search")
async def search(q: str, type: str = "album", limit: int = 24) -> dict[str, Any]:
    query = q.strip()
    if not query:
        return {"type": type, "results": []}
    try:
        results = await asyncio.to_thread(spotify.browse, query, type, limit)
    except spotify.ResolveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Spotify error: {exc}") from exc

    if type == "track":
        await asyncio.to_thread(_mark_held, results)
    return {"type": type, "results": results}


@app.get("/api/albums/{album_id}")
async def album(album_id: str) -> dict[str, Any]:
    try:
        detail = await asyncio.to_thread(spotify.album_detail, album_id)
    except spotify.ResolveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Album not found: {exc}") from exc
    await asyncio.to_thread(_mark_held, detail["tracks"])
    detail["held_count"] = sum(1 for t in detail["tracks"] if t["held"])
    return detail


@app.get("/api/artists/{artist_id}/albums")
async def artist(artist_id: str) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(spotify.artist_albums, artist_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Artist not found: {exc}") from exc


# --- beets library --------------------------------------------------------

class ApplyRequest(BaseModel):
    path: str
    album_id: str | None = None
    as_is: bool = False


class PathRequest(BaseModel):
    path: str


@app.get("/api/library/stats")
async def library_stats() -> dict[str, Any]:
    return await asyncio.to_thread(beets_library.stats)


@app.get("/api/library/albums")
async def library_albums(q: str = "") -> list[dict[str, Any]]:
    try:
        return await asyncio.to_thread(beets_library.albums, q)
    except Exception as exc:
        # An invalid beets query raises rather than returning nothing.
        raise HTTPException(status_code=400, detail=f"{exc}"[:200]) from exc


@app.get("/api/library/albums/{album_id}")
async def library_album(album_id: int) -> dict[str, Any]:
    detail = await asyncio.to_thread(beets_library.album_detail, album_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="No such album.")
    return detail


@app.get("/api/inbox")
async def inbox() -> list[dict[str, Any]]:
    return await asyncio.to_thread(beets_library.inbox)


@app.get("/api/inbox/candidates")
async def inbox_candidates(path: str) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(beets_library.candidates, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{exc}"[:200]) from exc


@app.post("/api/inbox/apply")
async def inbox_apply(request: ApplyRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            beets_library.apply, request.path, request.album_id, request.as_is
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/inbox/discard")
async def inbox_discard(request: PathRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(beets_library.discard, request.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- health ---------------------------------------------------------------

@app.get("/api/health")
async def health(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    # Reads Navidrome's database and stats a few directories, so it is quick
    # but blocking; a thread keeps it off the event loop.
    return await asyncio.to_thread(
        health_checks.report, STARTED_AT, session.identity.libraries)


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
    group = next((g for g in groups if g.key == request.key), None)
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


@app.post("/api/health/audit")
async def health_audit(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Re-read identity tags from every file. Slow, hence explicit."""
    def run() -> dict[str, Any]:
        return {library["name"]: diskaudit.refresh(Path(library["path"])).as_dict()
                for library in session.identity.libraries}

    return await asyncio.to_thread(run)


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return {
        "spotify_configured": settings.spotify_configured,
        "output_dir": str(settings.output_dir),
        "concurrency": settings.concurrency,
        "ledger_count": ledger.count(),
    }


@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    if auth.get(ws.cookies.get(auth.COOKIE)) is None:
        await ws.close(code=4401)
        return
    await broker.register(ws)
    try:
        await ws.send_json({"type": "snapshot", "jobs": sorted_jobs()})
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
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
