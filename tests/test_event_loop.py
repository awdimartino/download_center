"""Disk and database work stays off the event loop (CODE_REVIEW L41).

Resolving an album's folder, checking a library is mounted, reading every
track's tags to see it is one album, re-reading an account from Navidrome:
each ran inside an `async` route or the session middleware, so on the Pi's
disk it held every other request - the page, the socket, the progress of a
download - until it finished.

Each helper is replaced by one that notes whether it was called on the
loop's own thread, and then refuses, so the route stops there.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import auth, filer, inbox, library, main, workspace

on_loop: list[str] = []


def _watch(monkeypatch, module, name, refuse=ValueError("stop here")):
    def watched(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            on_loop.append(name)
        except RuntimeError:
            pass                       # in a worker thread, as it should be
        if refuse is None:
            return None
        raise refuse
    monkeypatch.setattr(module, name, watched)


@pytest.fixture(autouse=True)
def watched(monkeypatch):
    on_loop.clear()
    _watch(monkeypatch, workspace, "for_session")
    _watch(monkeypatch, library, "album_dir")
    _watch(monkeypatch, library, "track_path")
    _watch(monkeypatch, filer, "audio_in")
    _watch(monkeypatch, filer, "require_one_album")
    _watch(monkeypatch, inbox, "receiving")


SESSION = SimpleNamespace(
    id="s1", identity=SimpleNamespace(username="alex", is_admin=True,
                                      libraries=[{"id": 1, "name": "Music",
                                                  "path": "/music"}]))


def _call(route, *args):
    try:
        asyncio.run(route(*args))
    except (HTTPException, ValueError):
        pass


@pytest.mark.parametrize("route, body", [
    (main.library_album_edit, main.AlbumEdit(library_id=1, folder="A/B",
                                             album_artist="A", album="B")),
    (main.library_track_edit, main.TrackEdit(library_id=1, path="A/B/01.mp3",
                                             title="x")),
    (main.library_match, main.AlbumTarget(library_id=1, folder="A/B")),
    (main.library_match_apply, main.AlbumChoice(library_id=1, folder="A/B",
                                                release_id="r")),
    (main.library_cover_candidates, main.AlbumTarget(library_id=1, folder="A/B")),
    (main.library_cover_apply, main.CoverChoice(library_id=1, folder="A/B")),
    (main.library_combine, main.CombineRequest(library_id=1, albumartist="A",
                                               album="B", albums=["A/B", "A/C"])),
])
def test_an_album_route_checks_its_folder_off_the_loop(route, body):
    _call(route, body, SESSION)
    assert on_loop == []


def test_queuing_a_download_checks_the_library_off_the_loop():
    _call(main.create_job,
          main.JobRequest(url="https://open.spotify.com/album/abc"), SESSION)
    assert on_loop == []


def test_the_session_lookup_runs_off_the_loop(monkeypatch):
    from starlette.requests import Request

    _watch(monkeypatch, auth, "get", refuse=None)
    request = Request({"type": "http", "method": "GET", "path": "/api/library",
                       "headers": [(b"cookie", b"dc_session=s1")],
                       "query_string": b""})

    async def call_next(request):
        return "served"

    asyncio.run(main.require_session(request, call_next))
    assert on_loop == []
