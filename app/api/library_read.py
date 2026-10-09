"""Reading the Library: the list, its tabs, one album, the art."""

from __future__ import annotations

import asyncio
import functools
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response

from .. import albumcheck, auth, library, navidrome, operations
from .deps import current_session

# Every route here is for a signed-in person. Declared on the router as
# well as gated by the session middleware, so a route added here without
# its own Depends is still guarded, and the guard does not rest on a
# path prefix alone.
router = APIRouter(dependencies=[Depends(current_session)])


@router.get("/api/library")
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


@router.get("/api/library/artists")
async def library_artists(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Every album artist this person owns, and how much of each."""
    return await asyncio.to_thread(library.artists, session.identity)


@router.get("/api/library/attention")
async def library_attention(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """What wants a person: singles to combine, albums to review or measure."""
    return await asyncio.to_thread(library.attention, session.identity)


@router.get("/api/library/attention/covers")
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


@router.get("/api/library/genres")
async def library_genres(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """How many tracks carry each genre string, exactly as tagged."""
    return await asyncio.to_thread(library.genre_tally, session.identity)


@router.get("/api/library/album")
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


@router.get("/api/library/album/by-id")
async def library_album_by_id(
    album_id: str,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """The album row for a Navidrome album id, for /library/album/<id>."""
    try:
        found = await asyncio.to_thread(library.find_album, session.identity, album_id)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if found is None:
        raise HTTPException(status_code=404, detail="There is no such album in your library.")
    return found


@router.get("/api/library/album/missing")
async def library_album_missing(
    library_id: int,
    folder: str,
    edition: str | None = None,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """The album's full tracklist from MusicBrainz or Spotify, each track
    marked held or missing (albumcheck.py). `edition` picks another release
    from an earlier answer's `editions`."""
    if edition and not edition.startswith(("mb:", "sp:")):
        raise HTTPException(status_code=400, detail="Unknown edition.")
    try:
        return await asyncio.to_thread(
            albumcheck.missing, session.identity, library_id, folder, edition)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# Long, because a cover does not change without the file changing, and the
# id is derived from the file. A page of fifty of these is otherwise fifty
# round trips every time somebody scrolls back up. Private: each cover is
# checked against who is asking, and a shared cache in between - a proxy -
# would hand one person's art to anybody asking for the same URL.
ART_CACHE = "private, max-age=604800"


@router.get("/api/library/art")
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


@router.get("/api/operations")
async def list_operations(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Your long-running work in flight, and how your last runs went.

    Filtered here rather than in the browser: results carry folder names,
    paths and candidate lists, which are as private as the library."""
    return {"operations": operations.all_operations(session.identity.username)}
