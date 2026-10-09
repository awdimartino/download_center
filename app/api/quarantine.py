"""The Quarantine page: listing, restoring and deleting set-aside tracks."""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import auth, navidrome, quarantine
from .deps import _locked_request, current_session

# Every route here is for a signed-in person. Declared on the router as
# well as gated by the session middleware, so a route added here without
# its own Depends is still guarded, and the guard does not rest on a
# path prefix alone.
router = APIRouter(dependencies=[Depends(current_session)])

# One album's tracks, or a page's worth; never the whole quarantine in one
# request by accident.
MAX_KEYS = 2000


class Keys(BaseModel):
    keys: list[str] = Field(max_length=MAX_KEYS)


class Empty(BaseModel):
    older_than_days: int = Field(ge=0, le=3650)


def _roots(session: auth.Session) -> list[Path]:
    """Every library this person can see. Held while files move in or out,
    so no edit, match or ReplayGain run meets a file mid-move."""
    return [Path(lib["path"]) for lib in session.identity.libraries]


@router.get("/api/quarantine")
async def list_quarantine(session: auth.Session = Depends(current_session),
                          ) -> dict[str, Any]:
    return await asyncio.to_thread(quarantine.listing, session.identity)


@router.post("/api/quarantine/restore")
async def restore(body: Keys, session: auth.Session = Depends(current_session),
                  ) -> dict[str, Any]:
    if not body.keys:
        raise HTTPException(status_code=400, detail="Nothing to restore.")
    outcome = await _locked_request(
        _roots(session),
        functools.partial(quarantine.restore, session.identity, body.keys))
    if outcome["restored"]:
        # Back where Navidrome can see it; its old row returns with its
        # stars and plays on the next scan.
        await asyncio.to_thread(navidrome.notify)
    return outcome


@router.post("/api/quarantine/delete")
async def delete(body: Keys, session: auth.Session = Depends(current_session),
                 ) -> dict[str, Any]:
    if not body.keys:
        raise HTTPException(status_code=400, detail="Nothing to delete.")
    return await _locked_request(
        _roots(session),
        functools.partial(quarantine.delete, session.identity, body.keys))


@router.post("/api/quarantine/empty")
async def empty(body: Empty, session: auth.Session = Depends(current_session),
                ) -> dict[str, Any]:
    return await _locked_request(
        _roots(session),
        functools.partial(quarantine.empty, session.identity, body.older_than_days))
