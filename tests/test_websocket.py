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
