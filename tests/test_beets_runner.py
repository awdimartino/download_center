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
import time
from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3

from app import beets_runner, ledger

SILENCE = Path(__file__).parent / "fixtures" / "silence.mp3"


@pytest.fixture(autouse=True)
def refusals(state_db):
    """Refusals live in state.db now rather than in memory.

    They had to: the sweep needs to know across a restart what beets already
    turned down, or it walks the whole backlog again on every run and holds
    the import lock while it does."""
    yield


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


# --- choosing a match by hand -----------------------------------------------
# Beets refuses whenever it cannot separate two releases, which for a popular
# record means five near-identical pressings. It knows what the candidates
# are; quiet_fallback throws the list away. These cover getting it back.

def _fake_subprocess(monkeypatch, stdout, returncode=0):
    seen = {}

    class Result:
        pass

    def fake_run(command, **kwargs):
        seen["command"] = command
        result = Result()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = ""
        return result

    monkeypatch.setattr(beets_runner.subprocess, "run", fake_run)
    monkeypatch.setattr(beets_runner, "ensure_config", lambda space: None)
    return seen


def _space(tmp_path):
    return type("Space", (), {"beets_dir": tmp_path,
                              "beets_library": tmp_path / "library.db"})()


def test_candidates_are_read_from_the_last_line(monkeypatch, tmp_path):
    """Beets and its plugins write to stdout as they load - fetchart alone
    prints three lines about missing API keys. Taking the whole of stdout as
    JSON would fail on every real invocation."""
    _fake_subprocess(monkeypatch, stdout=(
        "fetchart: lastfm: Disabling art source due to missing key\n"
        '{"kind": "album", "candidates": [{"id": "mb-1", "title": "Thriller"}]}\n'
    ))
    answer = beets_runner.candidates(_space(tmp_path), tmp_path / "Thriller")

    assert [c["title"] for c in answer["candidates"]] == ["Thriller"]
    assert answer["name"] == "Thriller"


def test_unreadable_match_output_is_reported_not_guessed(monkeypatch, tmp_path):
    """An empty candidate list and a broken subprocess must not look the
    same: one means "MusicBrainz has nothing", the other means "this is
    broken", and only one of them is worth acting on."""
    _fake_subprocess(monkeypatch, stdout="Traceback (most recent call last):")
    answer = beets_runner.candidates(_space(tmp_path), tmp_path / "x")

    assert answer["candidates"] == []
    assert "could not read" in answer["error"] or "form this" in answer["error"]


def test_choosing_a_release_asks_beets_rather_than_overruling_it(
        monkeypatch, tmp_path):
    """The chosen release is applied through beets' own choose_match, the
    extension point its test suite uses, which applies a match whatever the
    confidence numbers say.

    Loosening the thresholds was tried first and measured: `--search-id`
    with `strong_rec_thresh` raised *and* the `max_rec` caps lifted still
    filed nothing, because quiet mode applies only on a strong
    recommendation and missing tracks cap it at medium regardless."""
    monkeypatch.setattr(beets_runner.settings, "beets_enabled", True)
    monkeypatch.setattr(beets_runner, "_library_size", lambda space: 0)
    seen = _fake_subprocess(monkeypatch, stdout="")

    beets_runner.import_chosen(_space(tmp_path), tmp_path / "album", "mb-1")

    command = seen["command"]
    assert command[1:4] == ["-m", "app.beets_match", "--apply"]
    assert command[4] == "mb-1"


def test_the_sweep_never_loosens_the_thresholds(monkeypatch, tmp_path):
    """Whatever a person is allowed to decide, the unattended sweep is not:
    it has nobody to ask, and must keep refusing."""
    # The words appear in the config, in a comment saying why not to touch
    # them. What must not appear is a setting.
    settings_only = "\n".join(
        line for line in beets_runner.DEFAULT_CONFIG.splitlines()
        if not line.lstrip().startswith("#"))
    assert "strong_rec_thresh" not in settings_only

    command = _command(monkeypatch, singleton=False, as_is=False)
    assert "-c" not in command and "--search-id" not in command


def test_a_chosen_release_that_files_nothing_says_so(monkeypatch, tmp_path):
    """Reported rather than called success. The folder is still sitting
    there either way, and "it worked" followed by an unchanged panel is the
    silence this codebase keeps producing."""
    monkeypatch.setattr(beets_runner.settings, "beets_enabled", True)
    monkeypatch.setattr(beets_runner, "_library_size", lambda space: 7)
    _fake_subprocess(monkeypatch, stdout="")

    result = beets_runner.import_chosen(_space(tmp_path), tmp_path / "a", "mb-1")

    assert result["imported"] == 0
    assert result["skipped"] == 1


# --- not asking beets the same question for ever ----------------------------
# The sweep used to retry every stuck item on every run. With a backlog of
# things that will never match, it therefore ran continuously - and one beets
# process holds the import lock, so every button in the staging tab answered
# "an import is already running". This is the fix.

def test_a_fresh_item_is_worth_trying(tmp_path):
    assert beets_runner.worth_trying(track(tmp_path, "new.mp3")) is True


def test_something_beets_already_refused_is_not_asked_again(tmp_path):
    """Beets is deterministic. The same file, unchanged, gets the same
    answer, and the asking is what was making staging unusable."""
    path = track(tmp_path, "stuck.mp3")
    beets_runner._remember_refusal(path, "no confident match")

    assert beets_runner.worth_trying(path) is False


def test_a_changed_file_is_a_different_question(tmp_path):
    """Retagged by hand, or normalised by a script - whatever beets thought
    about the old bytes does not apply."""
    path = track(tmp_path, "stuck.mp3")
    beets_runner._remember_refusal(path, "no confident match")

    import os
    later = path.stat().st_mtime + 60
    os.utime(path, (later, later))

    assert beets_runner.worth_trying(path) is True


def test_a_refusal_expires_eventually(tmp_path):
    """MusicBrainz gains releases. An album nobody had entered in March may
    be there in June, and never asking again would be its own trap."""
    path = track(tmp_path, "stuck.mp3")
    beets_runner._remember_refusal(path, "no confident match")
    ledger.connection().execute(
        "UPDATE import_refusal SET at = ?",
        (time.time() - beets_runner.RETRY_REFUSED_AFTER - 1,))
    ledger.connection().commit()

    assert beets_runner.worth_trying(path) is True


def test_a_refusal_survives_a_restart(tmp_path):
    """The whole point of moving this out of memory: a restart used to mean
    the next sweep re-walked the entire backlog."""
    path = track(tmp_path, "stuck.mp3")
    beets_runner._remember_refusal(path, "no confident match")

    stored = ledger.connection().execute(
        "SELECT reason FROM import_refusal WHERE path = ?",
        (str(path.resolve()),)).fetchone()
    assert stored[0] == "no confident match"


def test_only_the_sweep_second_guesses_beets():
    """`worth_trying` gates the unattended sweep and nothing else. Import
    as-is, Choose match and Try importing now go straight to beets: the
    person can see the item and is allowed to disagree about it."""
    import inspect

    source = inspect.getsource(beets_runner)
    sweep = source[source.index("def sweep_staging"):
                   source.index("def _prune_empty")]
    importer = source[source.index("def _import_paths"):
                      source.index("def settled")]

    assert "worth_trying" in sweep
    assert "worth_trying" not in importer


# --- why an import filed nothing --------------------------------------------
# Four different failures used to arrive as one sentence, and each of them
# sends you somewhere else. A whole staging backlog was read as unmatchable
# music when the container simply had no DNS.

def test_a_broken_resolver_is_not_a_tagging_failure():
    reason = beets_runner._why_nothing_filed(
        "ConnectionError: Failed to resolve 'musicbrainz.org'", as_is=False)
    assert "network" in reason
    assert "no confident match" not in reason


def test_a_duplicate_is_not_a_failure_at_all():
    reason = beets_runner._why_nothing_filed(
        "duplicate-keep /downloads/alex/albums/Xscape", as_is=False)
    assert "already in the library" in reason


def test_no_release_in_musicbrainz_is_not_low_confidence():
    """A doujin release nobody has entered is a different problem from a
    release that was found and doubted; only one of them is worth retrying."""
    reason = beets_runner._why_nothing_filed(
        "No matching release found for 6 tracks.", as_is=False)
    assert "no release" in reason.lower()


def test_an_as_is_import_never_reports_a_match_problem():
    reason = beets_runner._why_nothing_filed("something odd", as_is=True)
    assert "match" not in reason


def test_a_genuine_refusal_still_says_so():
    reason = beets_runner._why_nothing_filed(
        "Evaluating 5 candidates.", as_is=False)
    assert reason == "beets found no confident match"


# --- filing a fragment as the release its siblings already use --------------

def _library_with(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    """rows are (album, mb_albumid) and are credited to the track() artist."""
    return _library_rows(tmp_path, [("Radiohead", a, m) for a, m in rows])


def _library_rows(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    import sqlite3
    db = tmp_path / "library.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE albums (albumartist TEXT, album TEXT, mb_albumid TEXT)")
    connection.executemany("INSERT INTO albums VALUES (?, ?, ?)", rows)
    connection.commit()
    connection.close()
    return db


class _Space:
    def __init__(self, db: Path) -> None:
        self.beets_library = db


def test_a_fragment_joins_the_release_the_library_already_holds(tmp_path):
    """Beets matches a staging folder alone and cannot know the rest of the
    record is already filed. The library does know."""
    folder = tmp_path / "staged"
    track(folder, "01.mp3", "OK Computer")
    track(folder, "02.mp3", "OK Computer")
    space = _Space(_library_with(tmp_path, [("OK Computer", "rel-123")]))
    assert beets_runner.held_release(space, folder) == "rel-123"


def test_an_album_the_library_does_not_have_is_matched_normally(tmp_path):
    folder = tmp_path / "staged"
    track(folder, "01.mp3", "Kid A")
    space = _Space(_library_with(tmp_path, [("OK Computer", "rel-123")]))
    assert beets_runner.held_release(space, folder) is None


def test_a_library_that_disagrees_with_itself_is_left_alone(tmp_path):
    """Two releases for one title means the library is already split. Adding
    a fragment to whichever is commonest entrenches that rather than fixing
    it, so this declines to choose."""
    folder = tmp_path / "staged"
    track(folder, "01.mp3", "Pet Sounds")
    space = _Space(_library_with(
        tmp_path, [("Pet Sounds", "rel-a"), ("Pet Sounds", "rel-b")]))
    assert beets_runner.held_release(space, folder) is None


def test_two_albums_in_one_folder_are_not_a_fragment_of_either(tmp_path):
    folder = tmp_path / "staged"
    track(folder, "01.mp3", "OK Computer")
    track(folder, "02.mp3", "Kid A")
    space = _Space(_library_with(tmp_path, [("OK Computer", "rel-123")]))
    assert beets_runner.held_release(space, folder) is None


# --- the config beets is actually given -------------------------------------
# It is a string constant, so a typo in it is not a syntax error anywhere -
# it surfaces as beets quietly behaving differently on the next import.

def test_the_default_config_is_valid_yaml():
    import yaml
    rendered = (beets_runner.DEFAULT_CONFIG
                .replace("__DIRECTORY__", "/music")
                .replace("__LIBRARY__", "/library.db")
                .replace("__LOG__", "/import.log"))
    assert yaml.safe_load(rendered)["directory"] == "/music"


def _config() -> dict:
    import yaml
    return yaml.safe_load(beets_runner.DEFAULT_CONFIG
                          .replace("__DIRECTORY__", "/music")
                          .replace("__LIBRARY__", "/l.db")
                          .replace("__LOG__", "/i.log"))


def test_an_album_missing_a_track_is_still_allowed_to_file():
    """The cap this lifts was discarding albums beets had identified
    correctly. Distance still gates: measured, one track of a ten track
    release scores 0.5031 and is refused, nine of ten scores 0.0011."""
    assert _config()["match"]["max_rec"]["missing_tracks"] == "strong"


def test_extra_unrelated_tracks_are_still_capped():
    """The other half of max_rec must stay put - it is what stops a folder of
    loose tracks being filed as an album."""
    assert "unmatched_tracks" not in _config()["match"]["max_rec"]


def test_the_distance_gate_is_never_loosened():
    """max_rec caps a recommendation; strong_rec_thresh moves the gate. Only
    the first is safe to touch, and the second must stay absent."""
    config = _config()
    assert "strong_rec_thresh" not in config.get("match", {})


def test_fingerprinting_is_enabled():
    """Tag matching cannot help a file whose tags are wrong, which most
    hand-dropped rips are."""
    assert "chroma" in _config()["plugins"]


def test_the_same_title_by_another_artist_is_a_different_record(tmp_path):
    """Measured on the real library: matching on title alone found a one-track
    single called "sunburn" by almost monday for a staged thirteen-track
    "Fuel - Sunburn", and would have filed the album as that single."""
    folder = tmp_path / "staged"
    track(folder, "01.mp3", "Sunburn")          # credited to Radiohead by track()
    space = _Space(_library_rows(
        tmp_path, [("almost monday", "sunburn", "rel-single")]))
    assert beets_runner.held_release(space, folder) is None


def test_the_same_record_by_the_same_artist_is_reused(tmp_path):
    folder = tmp_path / "staged"
    track(folder, "01.mp3", "OK Computer")
    space = _Space(_library_rows(
        tmp_path, [("Radiohead", "OK Computer", "rel-ok")]))
    assert beets_runner.held_release(space, folder) == "rel-ok"


def test_a_loose_file_is_never_given_a_held_release(tmp_path):
    """A single file is a singleton, matched as a recording. Applying an album
    release to one would file it as a one-track copy of that album."""
    path = track(tmp_path, "loose.mp3", "OK Computer")
    space = _Space(_library_rows(tmp_path, [("Radiohead", "OK Computer", "r")]))
    assert beets_runner.held_release(space, path) is None
