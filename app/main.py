"""HTTP API, WebSocket event stream, and static file serving.

The application, its middleware and the page. The routes live in app/api/,
one router per panel; the work they start lives beside them in app/."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from . import auth, background, inbox, operations, store, threads
from .api import auth as auth_routes
from .api import browse as browse_routes
from .api import deps
from .api import duplicates as duplicates_routes
from .api import health as health_routes
from .api import inbox as inbox_routes
from .api import jobs as jobs_routes
from .api import library_edit as library_edit_routes
from .api import library_read as library_read_routes
from .api import listening as listening_routes
from .api import playlists as playlists_routes
from .api import settings as settings_routes
from .api.deps import CHANGING, DOC_PATHS, OPEN_PATHS, _send_cookie
from .api.inbox import UPLOAD_PATH, _upload_refusal
from .config import settings
from .events import push_operation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


log = logging.getLogger("navidrome_companion")


STATIC_DIR = Path(__file__).parent / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Sessions, jobs, operations, the folder locks and the socket broker all
    # live in this process's memory. A second worker would have its own of
    # each: signed-in people signed out at random, two downloads of one album
    # holding two different locks. Refused, rather than assumed.
    workers = os.environ.get("WEB_CONCURRENCY", "1").strip() or "1"
    if workers != "1":
        raise RuntimeError(
            f"WEB_CONCURRENCY is {workers}; this application keeps its state "
            "in memory and must run as a single worker.")
    store.connect(settings.state_db)
    operations.subscribe(push_operation)
    log.info("workspace root: %s", settings.output_dir)
    if not settings.spotify_configured:
        log.warning("Spotify credentials missing - add them to config/config.toml")
    try:
        await threads.run(inbox.clear_scratch)
    except Exception:
        log.exception("clearing unfinished downloads failed")

    loops = [asyncio.create_task(background._audit_loop()),
             asyncio.create_task(background._inbox_loop()),
             asyncio.create_task(background._snapshot_loop())]
    try:
        yield
    finally:
        for task in loops:
            task.cancel()
        for task in loops:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await background._wind_down()


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
    # In a thread: every few minutes per session it re-reads the account
    # from Navidrome's database, and that read sat on the event loop,
    # holding every other request while it ran.
    # No cookie, nothing to look up: /healthz and the sign-in page should not
    # wait for a thread to learn that.
    cookie = request.cookies.get(auth.COOKIE)
    session = await asyncio.to_thread(auth.get, cookie) if cookie else None
    request.state.session = session
    path = request.url.path
    if request.method in CHANGING and not deps.same_origin(request.headers):
        return JSONResponse({"detail": "That came from another page."},
                            status_code=403)
    gated = (path.startswith("/api/") and path not in OPEN_PATHS) or path in DOC_PATHS
    if gated and session is None:
        return JSONResponse({"detail": "Please sign in."}, status_code=401)
    if path == UPLOAD_PATH and request.method == "POST":
        refused = _upload_refusal(request.headers)
        if refused is not None:
            return refused
    response = await call_next(request)
    if session is not None and auth.cookie_due(session) and path != "/api/auth/logout":
        _send_cookie(response, request, session)
    return response


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


# The routes, one router per panel. Each declares its own guard, and the
# session middleware above gates every /api/ path besides.
app.include_router(auth_routes.router)
app.include_router(jobs_routes.router)
app.include_router(jobs_routes.socket)
app.include_router(inbox_routes.router)
app.include_router(settings_routes.router)
app.include_router(browse_routes.router)
app.include_router(health_routes.router)
app.include_router(duplicates_routes.router)
app.include_router(playlists_routes.router)
app.include_router(listening_routes.router)
app.include_router(library_read_routes.router)
app.include_router(library_edit_routes.router)


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
