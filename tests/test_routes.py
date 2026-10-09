"""Every route, and what guards it (R1).

Written before main.py was split into routers, so the split cannot quietly
change either. A router mounted without its guard would pass every other
test in the suite: they call the handlers as functions, and never go through
FastAPI's dependencies or the session middleware. These two tests do.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute

from app import main

# (method, path, guard). "session" and "admin" are the dependencies a route
# declares; "open" routes are reachable signed out on purpose.
ROUTES = {
    ('GET', '/', 'open'),
    ('GET', '/api/albums/{album_id}', 'session'),
    ('GET', '/api/artists/{artist_id}/albums', 'session'),
    ('POST', '/api/auth/login', 'open'),
    ('POST', '/api/auth/logout', 'open'),
    ('GET', '/api/auth/me', 'open'),
    ('GET', '/api/duplicates', 'session'),
    ('POST', '/api/duplicates/auto', 'session'),
    ('POST', '/api/duplicates/auto/apply', 'session'),
    ('POST', '/api/duplicates/dismiss', 'session'),
    ('GET', '/api/duplicates/quarantined', 'session'),
    ('POST', '/api/duplicates/resolve', 'session'),
    ('GET', '/api/health', 'session'),
    ('POST', '/api/health/audit', 'session'),
    ('POST', '/api/inbox/upload', 'session'),
    ('POST', '/api/inbox/upload/finish', 'session'),
    ('GET', '/api/jobs', 'session'),
    ('POST', '/api/jobs', 'session'),
    ('POST', '/api/library/album/missing/download', 'session'),
    ('DELETE', '/api/jobs/{job_id}', 'session'),
    ('GET', '/api/jobs/{job_id}', 'session'),
    ('POST', '/api/jobs/{job_id}/cancel', 'session'),
    ('POST', '/api/jobs/{job_id}/retry', 'session'),
    ('GET', '/api/library', 'session'),
    ('GET', '/api/library/album', 'session'),
    ('GET', '/api/library/album/missing', 'session'),
    ('POST', '/api/library/album/edit', 'session'),
    ('GET', '/api/library/art', 'session'),
    ('GET', '/api/library/artists', 'session'),
    ('GET', '/api/library/attention', 'session'),
    ('GET', '/api/library/attention/covers', 'session'),
    ('POST', '/api/library/combine', 'session'),
    ('POST', '/api/library/combine/guess', 'session'),
    ('POST', '/api/library/cover/apply', 'session'),
    ('POST', '/api/library/cover/candidates', 'session'),
    ('GET', '/api/library/genres', 'session'),
    ('POST', '/api/library/match', 'session'),
    ('POST', '/api/library/match/apply', 'session'),
    ('POST', '/api/library/quarantine', 'session'),
    ('POST', '/api/library/replaygain', 'session'),
    ('POST', '/api/library/replaygain/stop', 'session'),
    ('POST', '/api/library/rescan', 'session'),
    ('POST', '/api/library/reviewed', 'session'),
    ('POST', '/api/library/track/edit', 'session'),
    ('POST', '/api/library/track/quarantine', 'session'),
    ('GET', '/api/operations', 'session'),
    ('GET', '/api/overview', 'session'),
    ('GET', '/api/playcounts', 'admin'),
    ('POST', '/api/playcounts/snapshot', 'admin'),
    ('GET', '/api/playcounts/top', 'session'),
    ('GET', '/api/playlists', 'session'),
    ('POST', '/api/playlists', 'session'),
    ('DELETE', '/api/playlists/{playlist_id}', 'session'),
    ('PUT', '/api/playlists/{playlist_id}', 'session'),
    ('GET', '/api/recommendations', 'session'),
    ('POST', '/api/recommendations/dismiss', 'session'),
    ('POST', '/api/recommendations/refresh', 'session'),
    ('GET', '/api/search', 'session'),
    ('GET', '/api/settings', 'session'),
    ('PUT', '/api/settings', 'admin'),
    ('GET', '/api/status', 'session'),
    ('GET', '/healthz', 'open'),
    ('WS', '/ws', 'own-check'),
}


def _guards(dependant) -> set[str]:
    found = set()
    for dep in dependant.dependencies:
        name = getattr(dep.call, "__name__", "")
        if name in ("current_session", "admin_session"):
            found.add(name)
        found |= _guards(dep)
    return found


def _table() -> set[tuple[str, str, str]]:
    rows = set()
    for route in main.app.routes:
        if isinstance(route, APIRoute):
            guards = _guards(route.dependant)
            guard = ("admin" if "admin_session" in guards
                     else "session" if "current_session" in guards else "open")
            rows |= {(method, route.path, guard) for method in route.methods}
        elif isinstance(route, APIWebSocketRoute):
            rows.add(("WS", route.path, "own-check"))
    return rows


def test_every_route_keeps_its_path_and_its_guard():
    assert _table() == ROUTES


def _call(method: str, path: str) -> int:
    """One request straight into the application, signed out, the way a
    browser from this page would send it. The status it answered."""
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "scheme": "http",
        "server": ("app.test", 80), "client": ("127.0.0.1", 5000),
        "headers": [(b"host", b"app.test"), (b"sec-fetch-site", b"same-origin"),
                    (b"content-length", b"0")],
    }
    asyncio.run(main.app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


@pytest.mark.parametrize("method, path, guard",
                         sorted(r for r in ROUTES if r[2] in ("session", "admin")))
def test_every_guarded_route_refuses_a_signed_out_request(method, path, guard):
    concrete = re.sub(r"\{[^}]+\}", "x", path)
    assert _call(method, concrete) == 401


def test_every_router_but_sign_in_declares_the_session_guard_itself():
    """Not resting on the middleware's path prefix alone: a route added to
    one of these without its own Depends is still guarded."""
    import importlib
    import pkgutil

    import app.api
    from app.api import deps

    for info in pkgutil.iter_modules(app.api.__path__):
        if info.name in ("deps", "auth"):
            continue
        module = importlib.import_module(f"app.api.{info.name}")
        calls = [d.dependency for d in module.router.dependencies]
        assert deps.current_session in calls, info.name
