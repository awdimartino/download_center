"""What the routers share: who is asking, and the checks every change makes."""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

from fastapi import APIRouter, HTTPException, Request, Response

from .. import auth, filer, folderlock, inbox, library, navidrome, store, workspace

log = logging.getLogger("navidrome_companion")
router = APIRouter()


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


async def _prepare(space: workspace.Workspace) -> None:
    """Make a workspace's folders, or say why not. Two usernames that reduce
    to one folder name make the second person's prepare refuse, and that
    reached them as a bare 500."""
    try:
        await asyncio.to_thread(space.prepare)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
