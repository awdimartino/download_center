"""Pushing state changes to the browsers entitled to see them."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import WebSocket

from . import operations


class Broker:
    """Pushes state changes to the browsers entitled to see them.

    Clients are receive-only: all mutations go through the REST API, so a
    dropped socket costs nothing beyond a fresh snapshot on reconnect. Each
    is remembered with whose session opened it, because a job belongs to the
    person who queued it and broadcasting every job to every browser would
    hand one account a live feed of another's downloads.
    """

    def __init__(self) -> None:
        self._clients: dict[WebSocket, str] = {}
        # Which session opened each socket, so signing out can close it.
        self._sessions: dict[WebSocket, str] = {}
        # Closes in flight, held so they are not collected before they run.
        self._closing: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket, username: str,
                       session_id: str | None = None) -> None:
        await ws.accept()
        async with self._lock:
            self._clients[ws] = username
            if session_id:
                self._sessions[ws] = session_id

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.pop(ws, None)
            self._sessions.pop(ws, None)

    async def close_session(self, session_id: str) -> None:
        """Close every socket a session opened, saying it has ended.

        An open socket outlived its sign-out and went on receiving that
        person's events, and the page behind it never learned it was signed
        out. 4401 is what the page reads as "show the sign-in form".
        """
        async with self._lock:
            sockets = [ws for ws, sid in self._sessions.items()
                       if sid == session_id]
            for ws in sockets:
                self._clients.pop(ws, None)
                self._sessions.pop(ws, None)
        for ws in sockets:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(code=4401),
                                       timeout=self.SEND_TIMEOUT)

    # A client that has stopped reading must not hold up the others. Sends
    # were sequential and unbounded, so one phone on bad wifi with a full TCP
    # window stalled the publish loop - and with it the pusher driving every
    # active job - for everybody.
    SEND_TIMEOUT = 5.0

    async def publish(self, message: dict[str, Any],
                      owner: str | None = None) -> None:
        async with self._lock:
            targets = [(ws, who) for ws, who in self._clients.items()
                       if owner is None or who == owner]
        if not targets:
            return

        async def send(ws: WebSocket) -> WebSocket | None:
            try:
                await asyncio.wait_for(ws.send_json(message),
                                       timeout=self.SEND_TIMEOUT)
                return None
            except Exception:
                # Including the timeout. A socket that cannot take five
                # seconds of slack is gone; it will reconnect and get a fresh
                # snapshot, which is cheaper than holding everyone else up.
                return ws

        # Concurrently, so the slowest client costs the slowest client's time
        # rather than the sum of everybody's.
        stalled = await asyncio.gather(*(send(ws) for ws, _ in targets))
        for ws in stalled:
            if ws is not None:
                await self.unregister(ws)
                # Closed, not just forgotten. A socket dropped from the list
                # but left open went on looking "live" to its page - a phone
                # waking up still holding the TCP connection - and received
                # nothing ever again. Closing it is what makes it reconnect.
                # In the background, so a dead peer cannot hold this up.
                task = asyncio.create_task(self._close(ws))
                self._closing.add(task)
                task.add_done_callback(self._closing.discard)

    async def _close(self, ws: WebSocket) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(code=1011),
                                   timeout=self.SEND_TIMEOUT)


broker = Broker()


async def push_job(job: dict[str, Any]) -> None:
    await broker.publish({"type": "job", "job": job}, owner=job.get("owner"))


async def push_progress(job: dict[str, Any], changed: list) -> None:
    """Only what moved.

    The full job goes out on every phase change; between those, a 200-track
    playlist would otherwise re-send every track twice a second to a phone,
    almost all of it identical to the last one.
    """
    await broker.publish({
        "type": "job_progress",
        "id": job["id"],
        "status": job["status"],
        "error": job.get("error"),
        "items": [{"id": i["id"], "status": i["status"],
                   "progress": i.get("progress"), "error": i.get("error")}
                  for i in changed],
    }, owner=job.get("owner"))


async def push_operation(operation: operations.Operation) -> None:
    await broker.publish({"type": "operation", "operation": operation.as_dict()},
                         owner=operation.owner)
