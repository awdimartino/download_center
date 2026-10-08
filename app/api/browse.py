"""Browsing Spotify, and marking what is already in the library."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .. import auth, navidrome, registry, spotify
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
