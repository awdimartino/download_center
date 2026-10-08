"""An expired session over the WebSocket (CODE_REVIEW M24).

Closing before accepting is turned into an HTTP 403 by the server, and the
browser sees code 1006 - so after a restart the page reported "offline" and
reconnected every 15 seconds instead of asking you to sign in. The socket is
faked: there is no HTTP client in the test dependencies.
"""

from __future__ import annotations

import pytest

from app import main


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

    await main.websocket(socket)

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
    from app import main

    broker = main.Broker()
    mine, other_tab = _Socket(), _Socket()
    await broker.register(mine, "alex", "session-1")
    await broker.register(other_tab, "alex", "session-2")

    await broker.close_session("session-1")

    assert mine.closed_with == 4401
    assert other_tab.closed_with is None
    await broker.publish({"type": "job"}, owner="alex")
    assert list(broker._clients) == [other_tab]
