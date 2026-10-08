"""Home and the Listening panel."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .. import auth, overview, playcounts, threads
from .deps import admin_session, current_session

# Every route here is for a signed-in person. Declared on the router as
# well as gated by the session middleware, so a route added here without
# its own Depends is still guarded, and the guard does not rest on a
# path prefix alone.
router = APIRouter(dependencies=[Depends(current_session)])


@router.get("/api/overview")
async def overview_page(
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """What the landing page shows, in one request.

    One request rather than four, because the panels it summarises each walk
    Navidrome's whole index and a landing page calling all of them would be
    the slowest screen in the application.
    """
    return await asyncio.to_thread(overview.overview, session.identity)


@router.get("/api/playcounts")
async def playcount_status(
    session: auth.Session = Depends(admin_session),
) -> dict[str, Any]:
    """Whether snapshots are actually being taken.

    The thing that must not fail quietly is the collecting, and that is
    checkable tonight - long before there is enough history to say anything
    interesting with.

    An administrator's: the collector is the installation's, and its totals
    sum every account's imported plays - on a two-person install, the other
    person's listening.
    """
    return await asyncio.to_thread(playcounts.status)


# How far back the Listening panel will look. Bounded because the window
# reaches straight into a query: an unbounded one asks for every snapshot
# ever taken, on a Raspberry Pi, from a button.
MAX_LISTENING_DAYS = 3650


MAX_LISTENING_TRACKS = 200


@router.get("/api/playcounts/top")
async def playcount_top(
    days: int = 30,
    limit: int = 25,
    start: str | None = None,
    end: str | None = None,
    session: auth.Session = Depends(current_session),
) -> dict[str, Any]:
    """Everything the Listening panel shows for one window: the signed-in
    person's most played tracks, albums and genres, their listening by hour
    of day, and their longest session - all over the same range.

    Theirs alone. Play counts are per Navidrome account, and one person's
    listening is not another's to read - the same rule the rest of this
    application follows.
    """
    days = max(1, min(days, MAX_LISTENING_DAYS))
    limit = max(1, min(limit, MAX_LISTENING_TRACKS))
    # A chosen range, both ends inclusive, instead of "the last N days".
    # Both or neither: half a range is a typo, not a request.
    if (start is None) != (end is None):
        raise HTTPException(status_code=422,
                            detail="Give both a start and an end date.")
    if start is not None:
        try:
            first = datetime.strptime(start, "%Y-%m-%d")
            last = datetime.strptime(end, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(status_code=422,
                                detail="Dates must be YYYY-MM-DD.") from exc
        if first > last:
            raise HTTPException(status_code=422,
                                detail="The start date is after the end date.")
        days = (last - first).days + 1
        # Written back the way the history is compared: as text, against
        # padded dates. strptime takes "2026-9-1", and the raw string then
        # sorted after every September day and the range came back empty.
        start, end = first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d")

    def collect() -> dict[str, Any]:
        if start is not None:
            window_start, window_end = start, end
        else:
            # Today, not yesterday. The window stopped at the last *complete*
            # day because a nightly reading could not describe a day still
            # going on; reading every few minutes can, and the panel was
            # otherwise unable to show anything played since midnight.
            window_end = playcounts.today()
            window_start = overview.days_back(window_end, days)
        return _listening_window(window_start, window_end, days)

    def _listening_window(start: str, end: str, days: int) -> dict[str, Any]:
        user_id = session.identity.user_id
        # The statistics are kept until a new play arrives or the track
        # index changes; the coverage is not, because "is it collecting"
        # and "last read at" are about the clock rather than the history.
        return {
            **overview.window(user_id, start, end, days, limit),
            # Theirs, not the installation's. status() counts every account's
            # imported history together, which shown to someone who has never
            # played anything is both baffling and none of their business.
            "coverage": playcounts.coverage(user_id),
        }

    return await asyncio.to_thread(collect)


@router.post("/api/playcounts/snapshot")
async def playcount_snapshot(
    session: auth.Session = Depends(admin_session),
) -> dict[str, Any]:
    """Take one now rather than waiting for the timer. Admin only: it reads
    every account's listening, not just the caller's.

    Warms Home for whoever it found new plays for, as the timed reading
    does - otherwise their next visit pays for the statistics this just
    made stale.
    """
    taken = await threads.run(playcounts.take)
    if taken.get("users"):
        await threads.run(overview.warm, taken["users"])
    return taken
