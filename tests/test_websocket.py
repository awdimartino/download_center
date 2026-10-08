"""An expired session over the WebSocket (CODE_REVIEW M24).

Closing before accepting is turned into an HTTP 403 by the server, and the
browser sees code 1006 - so after a restart the page reported "offline" and
reconnected every 15 seconds instead of asking you to sign in. The socket is
faked: there is no HTTP client in the test dependencies.
"""

from __future__ import annotations

import pytest

from app import auth
from app import events
from app.api import jobs as jobs_routes
from app import jobs
from app.api import deps


class FakeSocket:
    def __init__(self):
        self.cookies = {}
        self.calls = []

    async def accept(self):
        self.calls.append("accept")

    async def close(self, code=1000):
        self.calls.append(("close", code))


@pytest.mark.asyncio
async def test_an_expired_session_is_told_so_with_4401():
    socket = FakeSocket()

    await jobs_routes.websocket(socket)

    assert socket.calls == ["accept", ("close", 4401)]


# --- signing out closes that session's sockets (2M22) ----------------------------

class _Socket:
    def __init__(self):
        self.closed_with = None

    async def accept(self):
        pass

    async def send_json(self, message):
        pass

    async def close(self, code=1000):
        self.closed_with = code


@pytest.mark.asyncio
async def test_signing_out_closes_that_sessions_sockets_only():
    """An open socket outlived its sign-out, went on receiving that person's
    events, and the page behind it never showed the sign-in form."""

    broker = events.Broker()
    mine, other_tab = _Socket(), _Socket()
    await broker.register(mine, "alex", "session-1")
    await broker.register(other_tab, "alex", "session-2")

    await broker.close_session("session-1")

    assert mine.closed_with == 4401
    assert other_tab.closed_with is None
    await broker.publish({"type": "job"}, owner="alex")
    assert list(broker._clients) == [other_tab]


@pytest.mark.asyncio
async def test_a_client_dropped_for_stalling_is_closed_not_just_forgotten(monkeypatch):
    """Unregistered but left open, it went on looking live to its page and
    received nothing ever again; nothing made it reconnect (2M27)."""
    import asyncio


    class Stalled(_Socket):
        async def send_json(self, message):
            await asyncio.Event().wait()

    broker = events.Broker()
    monkeypatch.setattr(events.Broker, "SEND_TIMEOUT", 0.05)
    stalled = Stalled()
    await broker.register(stalled, "alex")

    await broker.publish({"type": "job"}, owner="alex")
    await asyncio.sleep(0.05)

    assert stalled.closed_with == 1011
    assert list(broker._clients) == []


@pytest.mark.asyncio
async def test_a_socket_closes_once_its_session_is_gone(monkeypatch):
    """A socket outlived its session - expired, the account removed - and
    went on receiving that person's events (2L6)."""
    import asyncio
    from types import SimpleNamespace


    class Socket(_Socket):
        cookies = {auth.COOKIE: "session-1"}
        headers = {}

        async def receive_text(self):
            await asyncio.Event().wait()

    session = SimpleNamespace(identity=SimpleNamespace(username="alex"))
    answers = iter([session, None])
    monkeypatch.setattr(auth, "get", lambda cookie: next(answers))
    monkeypatch.setattr(deps, "same_origin", lambda headers: True)
    monkeypatch.setattr(jobs, "_visible_jobs", lambda session: [])
    monkeypatch.setattr(jobs_routes, "SOCKET_RECHECK", 0.05)
    monkeypatch.setattr(events, "broker", events.Broker())
    ws = Socket()

    await asyncio.wait_for(jobs_routes.websocket(ws), 2)

    assert ws.closed_with == 4401
