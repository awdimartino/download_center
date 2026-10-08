"""The Health panel."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from .. import auth, diskaudit, operations
from .. import health as health_checks
from ..background import STARTED_AT
from .deps import current_session

# Every route here is for a signed-in person. Declared on the router as
# well as gated by the session middleware, so a route added here without
# its own Depends is still guarded, and the guard does not rest on a
# path prefix alone.
router = APIRouter(dependencies=[Depends(current_session)])


@router.get("/api/health")
async def health(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    # Reads Navidrome's database and stats a few directories, so it is quick
    # but blocking; a thread keeps it off the event loop.
    return await asyncio.to_thread(
        health_checks.report, STARTED_AT, session.identity.libraries,
        session.identity)


@router.post("/api/health/audit")
async def health_audit(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Re-read identity tags from every file.

    Started, not awaited. It reads every file in the library, which is
    minutes on a Pi - far longer than a browser will hold a request open, so
    awaiting it made the button look broken while the work went on unseen.
    The result arrives over the websocket and is readable from
    /api/operations.
    """
    libraries = list(session.identity.libraries)

    def run() -> dict[str, Any]:
        return {library["name"]: diskaudit.refresh(Path(library["path"])).as_dict()
                for library in libraries}

    operation, started = operations.start(
        "audit", session.identity.username, run)
    return {"started": started, "operation": operation.as_dict()}
