"""HTTP API, WebSocket event stream, and static file serving."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ledger, spotify, staging, worker
from . import config
from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("download_center")

STATIC_DIR = Path(__file__).parent / "static"


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
    yield


app = FastAPI(title="Download Center", lifespan=lifespan)


class JobRequest(BaseModel):
    url: str


async def _resolve_job(job: dict[str, Any], url: str) -> None:
    """Resolve a link off the event loop, then announce the result."""
    try:
        kind, title, tracks = await asyncio.to_thread(spotify.resolve_link, url)
    except spotify.ResolveError as exc:
        job.update(status="failed", error=str(exc))
        log.warning("resolve failed for %s: %s", url, exc)
    except Exception as exc:
        job.update(status="failed", error=f"Spotify error: {exc}")
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
        spotify.parse_link(url)
    except spotify.ResolveError as exc:
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


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    values = {key: getattr(settings, key) for key in config.EDITABLE}
    # Never send the secret back to the browser; report only whether it is set.
    values["spotify_client_secret"] = ""
    values["spotify_client_secret_set"] = bool(settings.spotify_client_secret)
    return values


@app.put("/api/settings")
async def put_settings(update: SettingsUpdate) -> dict[str, Any]:
    changes = {k: v for k, v in update.model_dump().items() if v is not None}
    # A blank secret means "leave it alone", since the form never receives it.
    if not changes.get("spotify_client_secret"):
        changes.pop("spotify_client_secret", None)
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
