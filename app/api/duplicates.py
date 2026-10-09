"""The Duplicates panel."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import auth, duplicates, inbox, navidrome, operations, store
from ..jobs import JOBS, RUNNING
from .deps import current_session

# Every route here is for a signed-in person. Declared on the router as
# well as gated by the session middleware, so a route added here without
# its own Depends is still guarded, and the guard does not rest on a
# path prefix alone.
router = APIRouter(dependencies=[Depends(current_session)])


class ResolveRequest(BaseModel):
    key: str
    keeper: str


class DismissRequest(BaseModel):
    key: str = Field(max_length=64)
    note: str = Field(default="", max_length=500)


def _duplicate_groups(identity: navidrome.Identity) -> list[duplicates.Group]:
    connection = navidrome.open_db()
    with connection:
        return duplicates.find(connection, identity)


async def _groups_for(identity: navidrome.Identity) -> list[duplicates.Group]:
    """The duplicate groups, or a 503 saying Navidrome's database cannot be
    read. Resolve and dismiss let that through as a bare 500."""
    try:
        return await asyncio.to_thread(_duplicate_groups, identity)
    except navidrome.Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/api/duplicates")
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


@router.post("/api/duplicates/resolve")
async def resolve_duplicate(
    request: ResolveRequest,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    groups = await _groups_for(session.identity)
    # Matched on what the browser was shown, which is the file-derived key.
    group = next((g for g in groups if g.dismiss_key == request.key), None)
    if group is None:
        raise HTTPException(status_code=404, detail="No such duplicate group.")
    try:
        outcome = await asyncio.to_thread(
            duplicates.resolve, group, request.keeper, session.identity)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # The file has moved; Navidrome still has it at the old path until it
    # looks again. The page does not wait for that - it filters resolved
    # copies out on its own - but without this the stale rows sit in
    # Navidrome's index until whenever it next scans on its own.
    if outcome.get("quarantined"):
        await asyncio.to_thread(navidrome.notify)
    return outcome


@router.post("/api/duplicates/dismiss")
async def dismiss_duplicate(
    request: DismissRequest,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    # Only a group this person can see: any key at all used to be accepted,
    # from anyone, with a note of any size.
    groups = await _groups_for(session.identity)
    if not any(g.dismiss_key == request.key for g in groups):
        raise HTTPException(status_code=404, detail="No such duplicate group.")
    await asyncio.to_thread(store.dismiss_duplicate, request.key, request.note,
                            session.identity.user_id)
    return {"dismissed": request.key}


@router.post("/api/duplicates/auto")
async def auto_resolve_preview(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """What resolving the confident groups would do: their keys and keepers,
    which /auto/apply then acts on exactly."""
    def run() -> dict[str, Any]:
        connection = navidrome.open_db()
        with connection:
            return duplicates.auto_resolve(connection, session.identity)

    try:
        return await asyncio.to_thread(run)
    except navidrome.Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class AutoResolve(BaseModel):
    # The preview's groups, as it returned them: {"key": ..., "keeper": ...}.
    groups: list[dict[str, str]]


@router.post("/api/duplicates/auto/apply")
async def auto_resolve_apply(
    body: AutoResolve,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Resolve exactly the previewed groups, as an operation.

    Refused while music is still arriving: importing is what creates
    duplicates, so a list taken mid-import is stale by the end. An operation
    because it is up to two Navidrome calls per group, which for a few
    hundred groups is far longer than a request should be held open.
    """
    identity = session.identity
    busy = [job for job in JOBS.values()
            if job.get("owner") == identity.username and job["id"] in RUNNING]
    arriving = await asyncio.to_thread(
        lambda: any(inbox.receiving(Path(lib["path"]))
                    for lib in identity.libraries))
    if busy or arriving:
        raise HTTPException(
            status_code=409,
            detail="Music is still being filed into your library. Resolve "
                   "duplicates once it has finished - importing is what "
                   "creates them.")
    chosen = {g["key"]: g["keeper"] for g in body.groups
              if g.get("key") and g.get("keeper")}

    def run() -> dict[str, Any]:
        connection = navidrome.open_db()
        with connection:
            outcome = duplicates.auto_resolve(connection, identity, chosen)
        # Once for the whole run rather than once per group: a scan per
        # resolved duplicate would be a hundred scans of the same library.
        if outcome.get("resolved"):
            navidrome.notify()
        return outcome

    operation, started = operations.start("dupes-auto", identity.username, run)
    return {"started": started, "operation": operation.as_dict()}
