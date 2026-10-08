"""Smart playlists."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, navidrome
from .. import playlists as smart_playlists
from .deps import _end_session, _navidrome_error, current_session

# Every route here is for a signed-in person. Declared on the router as
# well as gated by the session middleware, so a route added here without
# its own Depends is still guarded, and the guard does not rest on a
# path prefix alone.
router = APIRouter(dependencies=[Depends(current_session)])


class PlaylistRequest(BaseModel):
    name: str
    # The flat form the browser works in; translated to Navidrome's nested
    # rule shape in one place, in app/playlists.py.
    form: dict[str, Any]
    comment: str = ""
    public: bool = False


@router.get("/api/playlists")
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


@router.post("/api/playlists")
async def create_playlist(
    request: PlaylistRequest,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    return await _save_playlist(request, session, None)


@router.put("/api/playlists/{playlist_id}")
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


@router.delete("/api/playlists/{playlist_id}")
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
