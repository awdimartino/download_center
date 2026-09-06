"""Handing staged files to beets, and getting out of it when beets refuses.

Beets is configured never to guess, so anything it cannot place stays in
staging. That is correct and was also a dead end - nothing in the
application could file it, and the folder gave no sign that anything had
been tried. These cover the way out and the note that says why it is needed.

The audio here is a real 0.65s MP3 (tests/fixtures/silence.mp3), tagged with
mutagen. A stub will not do: mutagen returns None for a file with an ID3
header and no audio frames, so an album tag written onto one reads back as
absent - which is the exact answer the code under test is asking for.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3

from app import beets_runner

SILENCE = Path(__file__).parent / "fixtures" / "silence.mp3"


@pytest.fixture(autouse=True)
def forget_refusals():
    beets_runner._refused.clear()
    yield
    beets_runner._refused.clear()


def track(directory: Path, name: str, album: str | None = None) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SILENCE, path)
    tags = EasyID3(path)
    tags["title"] = "Let Down"
    tags["artist"] = "Radiohead"
    if album:
        tags["album"] = album
    tags.save()
    return path


# --- which beets mode a path gets -------------------------------------------

def test_a_loose_file_is_matched_as_a_singleton(tmp_path):
    """Album-matching a fragment of a release is what makes an import stop
    and ask - the thing the whole pipeline is arranged to avoid."""
    assert beets_runner._singleton_mode(
        track(tmp_path, "loose.mp3", "OK Computer"), as_is=False) is True


def test_a_directory_is_always_an_album(tmp_path):
    (tmp_path / "album").mkdir()
    for as_is in (False, True):
        assert beets_runner._singleton_mode(tmp_path / "album", as_is) is False


def test_as_is_files_a_track_with_an_album_into_that_album(tmp_path):
    """With --noautotag nothing is matched, so the flag only picks a path
    template. `$albumartist/$album/` is where this track's siblings land, so
    a later arrival joins it instead of founding a second copy of the record.
    Verified against beets itself: it files to Radiohead/OK Computer/."""
    assert beets_runner._singleton_mode(
        track(tmp_path, "with-album.mp3", "OK Computer"), as_is=True) is False


def test_as_is_files_a_track_with_no_album_under_non_album(tmp_path):
    assert beets_runner._singleton_mode(
        track(tmp_path, "loose.mp3"), as_is=True) is True


def test_an_unreadable_file_falls_back_to_a_singleton(tmp_path):
    """album_name() answers "" for anything it cannot read. Non-Album/ is the
    safe end of that: it cannot invent an album directory out of nothing."""
    broken = tmp_path / "truncated.mp3"
    broken.write_bytes(b"not audio")
    assert beets_runner._singleton_mode(broken, as_is=True) is True


# --- the flags that actually reach beets ------------------------------------

def _command(monkeypatch, singleton, as_is):
    seen = {}

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        seen["command"] = command
        return Result()

    monkeypatch.setattr(beets_runner.subprocess, "run", fake_run)
    space = type("Space", (), {"beets_dir": Path("/beets")})()
    beets_runner._run(space, Path("/staging/x.mp3"), singleton, as_is)
    return seen["command"]


def test_as_is_passes_noautotag(monkeypatch):
    assert "-A" in _command(monkeypatch, singleton=False, as_is=True)


def test_an_ordinary_import_never_passes_noautotag(monkeypatch):
    """The escape hatch is only ever reached by someone asking for it. If
    this leaks into the sweep, every doubtful thing in staging gets filed
    under whatever tags it happens to carry, unattended."""
    assert "-A" not in _command(monkeypatch, singleton=True, as_is=False)


def test_every_flag_precedes_the_path(monkeypatch):
    """optparse takes them interspersed, but a path beginning with a dash
    would then be read as an option."""
    command = _command(monkeypatch, singleton=True, as_is=True)
    assert command[-1] == str(Path("/staging/x.mp3"))
    assert set(command[-4:-1]) == {"-q", "-s", "-A"}


# --- saying why something is still sitting there ----------------------------

def test_a_refusal_is_remembered_against_the_path(tmp_path):
    path = track(tmp_path, "stuck.mp3")
    beets_runner._remember_refusal(path, "beets found no confident match")

    assert beets_runner.refusal(path)["reason"] == \
        "beets found no confident match"


def test_nothing_is_claimed_about_a_path_never_tried(tmp_path):
    assert beets_runner.refusal(tmp_path / "fresh.mp3") is None


def test_the_same_file_reached_two_ways_finds_one_note(tmp_path):
    """The sweep walks iterdir(); an item imported by name comes through
    Workspace.staged(), which resolves. Keyed on the raw string, the note
    written by one would never be found by the other - and the row would show
    no reason at all, which is the exact silence this note exists to break."""
    path = track(tmp_path, "stuck.mp3")
    beets_runner._remember_refusal(tmp_path / "." / "stuck.mp3", "no match")

    assert beets_runner.refusal(path)["reason"] == "no match"


def test_filing_something_clears_its_refusal(tmp_path):
    path = track(tmp_path, "stuck.mp3")
    beets_runner._remember_refusal(path, "beets found no confident match")
    beets_runner._forget_refusals([path])

    assert beets_runner.refusal(path) is None


def test_a_path_that_has_gone_stops_being_remembered(tmp_path):
    """Beets moves the file out when it files it, and staging names get
    reused. A note left behind would be attached to the next thing to arrive
    under that name."""
    path = track(tmp_path, "stuck.mp3")
    beets_runner._remember_refusal(path, "beets found no confident match")
    path.unlink()
    beets_runner._forget_refusals([])

    assert beets_runner.refusal(path) is None


# --- the escape hatch, wired up ---------------------------------------------
# There are no HTTP-level tests in this suite, so these check the wiring the
# browser depends on: a route that is not registered, or one that takes the
# item by a different name than the page sends, fails only in a browser.

def test_the_as_is_route_is_registered():
    from app.main import app

    routes = {route.path: route for route in app.routes
              if hasattr(route, "methods")}
    assert "/api/staging/import-as-is" in routes
    assert "POST" in routes["/api/staging/import-as-is"].methods


def test_the_as_is_route_takes_a_kind_and_a_name():
    """What app.js sends. `staged()` uses both - the kind picks the folder
    and the beets mode - so neither is optional."""
    from pydantic import ValidationError

    from app.main import ImportAsIs

    assert set(ImportAsIs.model_fields) == {"kind", "name"}
    with pytest.raises(ValidationError):
        ImportAsIs(kind="single")
