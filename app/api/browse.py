"""Browsing Spotify, and marking what is already in the library."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, navidrome, recommend, registry, spotify
from ..config import settings
from .deps import current_session

# Every route here is for a signed-in person. Declared on the router as
# well as gated by the session middleware, so a route added here without
# its own Depends is still guarded, and the guard does not rest on a
# path prefix alone.
router = APIRouter(dependencies=[Depends(current_session)])


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


@router.get("/api/search")
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


@router.get("/api/albums/{album_id}")
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


class Dismissal(BaseModel):
    kind: str
    key: str
    label: str = ""
    undo: bool = False


def _shelves_held(data: dict[str, Any], library_id: int) -> dict[str, Any]:
    """Mark the shelves the way search results are marked, and drop what is
    now wholly in the library - downloaded since the pass, most likely from
    the shelf itself."""
    shelves = data["shelves"]
    albums = shelves["new"] + shelves["missing"] + [
        card for shelf in shelves["genres"] for card in shelf["albums"]]
    tracks = [card for shelf in shelves["because"] for card in shelf["tracks"]]
    _mark_albums_held(albums, library_id)
    _mark_held(tracks, library_id)

    def unheld(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [c for c in cards if not (c.get("total") and c["held_tracks"] >= c["total"])]

    shelves["new"] = unheld(shelves["new"])
    shelves["missing"] = unheld(shelves["missing"])
    shelves["genres"] = [{**s, "albums": unheld(s["albums"])} for s in shelves["genres"]]
    shelves["because"] = [{**s, "tracks": [t for t in s["tracks"] if not t["held"]]}
                          for s in shelves["because"]]
    return data


@router.get("/api/recommendations")
async def recommendations(session: auth.Session = Depends(current_session),
                          ) -> dict[str, Any]:
    """The Download tab's shelves, from the last daily pass.

    Never waits for a pass: one that is due is started in the background
    and the stored one is answered meanwhile (see recommend.view).
    """
    library_id = _browsing_library(session)
    if library_id is None:
        return {"status": "failed",
                "reason": "Your account has no library to recommend for."}
    if not settings.spotify_configured:
        return {"status": "failed", "reason": "Spotify is not set up yet."}
    user_id = session.identity.user_id
    data = await asyncio.to_thread(recommend.view, user_id, library_id)
    if data["status"] == "ready":
        await asyncio.to_thread(_shelves_held, data, library_id)
    return data


@router.post("/api/recommendations/refresh")
async def refresh_recommendations(session: auth.Session = Depends(current_session),
                                  ) -> dict[str, Any]:
    library_id = _browsing_library(session)
    if library_id is None:
        raise HTTPException(status_code=400, detail="Your account has no library.")
    started = await asyncio.to_thread(
        recommend.refresh, session.identity.user_id, library_id)
    return {"started": started}


@router.post("/api/recommendations/dismiss")
async def dismiss_recommendation(body: Dismissal,
                                 session: auth.Session = Depends(current_session),
                                 ) -> dict[str, Any]:
    if body.kind not in recommend.KINDS or not body.key:
        raise HTTPException(status_code=400, detail="Nothing to dismiss.")
    user_id = session.identity.user_id
    if body.undo:
        await asyncio.to_thread(recommend.undismiss, user_id, body.kind, body.key)
    else:
        await asyncio.to_thread(recommend.dismiss, user_id, body.kind, body.key,
                                body.label[:200])
    return {"ok": True}


@router.get("/api/artists/{artist_id}/albums")
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
