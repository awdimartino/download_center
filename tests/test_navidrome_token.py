"""Navidrome's bearer token, kept fresh (CODE_REVIEW M27).

The token from sign-in was kept for the session's fortnight while Navidrome
expires it after its session timeout, and the refreshed one Navidrome sends
back on every response was ignored. Playlists then failed from day two with
"Navidrome refused that" while everything else worked. requests is faked.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import auth, navidrome
from app.config import settings
from app.api import playlists as playlists_routes


class Response:
    def __init__(self, status=200, body=None, token=None):
        self.status_code = status
        self._body = body if body is not None else []
        self.headers = {"x-nd-authorization": f"Bearer {token}"} if token else {}
        self.text = ""

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _identity():
    return navidrome.Identity(user_id="u", username="alex", is_admin=False,
                              token="old", subsonic_token="", subsonic_salt="")


@pytest.fixture(autouse=True)
def url(monkeypatch):
    monkeypatch.setattr(settings, "navidrome_url", "http://navidrome.invalid")


def test_the_refreshed_token_is_kept(monkeypatch):
    seen = []

    def request(method, url, headers, timeout, **kw):
        seen.append(headers["x-nd-authorization"])
        return Response(token="new")

    monkeypatch.setattr(navidrome.requests, "request", request)
    identity = _identity()

    navidrome.playlists(identity)
    navidrome.playlists(identity)

    assert seen == ["Bearer old", "Bearer new"]
    assert identity.token == "new"


def test_a_rejected_token_is_an_expired_session(monkeypatch):
    monkeypatch.setattr(navidrome.requests, "request",
                        lambda *a, **k: Response(status=401))
    with pytest.raises(navidrome.SessionExpired):
        navidrome.playlists(_identity())


@pytest.mark.asyncio
async def test_an_expired_navidrome_sign_in_ends_this_one(monkeypatch):
    monkeypatch.setattr(navidrome.requests, "request",
                        lambda *a, **k: Response(status=401))
    ended = []
    monkeypatch.setattr(auth, "sign_out", ended.append)
    session = SimpleNamespace(id="sid", identity=_identity())

    with pytest.raises(HTTPException) as refused:
        await playlists_routes.list_playlists(session)

    assert refused.value.status_code == 401
    assert ended == ["sid"]
