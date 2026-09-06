"""The module that moves files. Tested first, and hardest.

Nothing here is deleted, but "nothing is deleted" is a claim about where a
file ends up, and that is exactly what these check: on the right volume,
under a path that says where it came from, with a row in the ledger saying
what happened. The old code satisfied none of the three.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import duplicates, ledger, navidrome
from conftest import add_track


@pytest.fixture(autouse=True)
def no_navidrome_calls(monkeypatch):
    """Annotation migration succeeds unless a test says otherwise."""
    monkeypatch.setattr(navidrome, "star", lambda *a, **k: True)
    monkeypatch.setattr(navidrome, "set_rating", lambda *a, **k: True)


def _copy(track_id="a", path="Artist/Album/01 Song.mp3", **fields):
    defaults = dict(
        id=track_id, path=path, title="Song", album="Album", artist="Artist",
        suffix="mp3", bit_rate=320, duration=200.0, size=8_000_000, mbid="",
        track_artist="Artist", starred=False, rating=0, library_id=1,
        library="Music", starred_by_others="",
    )
    defaults.update(fields)
    return duplicates.Copy(**defaults)


def _group(copies, keeper=None):
    return duplicates.Group(
        key="k", reason="musicbrainz", copies=copies,
        keeper=keeper or copies[0], confident=False)


# --- where a quarantined file goes ----------------------------------------

def test_quarantine_lives_inside_the_library_root(tmp_path):
    """The old path was music_dir.parent, which in the deployed layout was
    not a mounted volume at all - so a "quarantined" file was written into
    the container and lost on the next deploy, while the original had been
    unlinked because the move crossed a device boundary."""
    root = tmp_path / "music"
    root.mkdir()
    quarantine = duplicates._quarantine_root(root)

    assert quarantine.is_relative_to(root), (
        "quarantine must be inside the library, on the same filesystem")
    assert quarantine.is_dir()


def test_quarantine_is_hidden_from_navidromes_scanner(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    quarantine = duplicates._quarantine_root(root)
    marker = quarantine / duplicates.NDIGNORE

    assert marker.is_file(), (
        "without .ndignore Navidrome re-indexes the quarantine and every "
        "resolved duplicate comes straight back")


def test_each_library_quarantines_into_its_own_root(tmp_path):
    alex, kelly = tmp_path / "music", tmp_path / "kelly"
    alex.mkdir()
    kelly.mkdir()

    assert duplicates._quarantine_root(alex) != duplicates._quarantine_root(kelly)
    assert duplicates._quarantine_root(kelly).is_relative_to(kelly)


# --- how a path is preserved ----------------------------------------------

@pytest.mark.parametrize("stored, expected", [
    ("Artist/Album/01 Song.mp3", "Artist/Album/01 Song.mp3"),
    ("/music/Artist/Album/01 Song.mp3", "Artist/Album/01 Song.mp3"),
])
def test_relative_accepts_both_shapes_navidrome_has_stored(stored, expected):
    """The column has held absolute and library-relative paths across
    versions. Getting this wrong silently resolves against the wrong root."""
    assert duplicates._relative(_copy(path=stored), Path("/music")) == Path(expected)


def test_relative_falls_back_to_the_basename_when_outside_the_root():
    got = duplicates._relative(_copy(path="/elsewhere/x.mp3"), Path("/music"))
    assert got == Path("x.mp3")


def test_resolve_keeps_the_path_the_file_came_from(tmp_path, state_db, identity):
    root = tmp_path / "music"
    source = root / "Artist" / "Album" / "01 Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"audio")

    keeper = _copy("keep", "Artist/Album/01 Song.flac", suffix="flac")
    loser = _copy("drop", "Artist/Album/01 Song.mp3")
    duplicates.resolve(_group([keeper, loser]), "keep", identity)

    landed = root / duplicates.QUARANTINE_NAME / "Artist" / "Album" / "01 Song.mp3"
    assert landed.is_file(), (
        "flattening to the basename threw away which record it came from")
    assert not source.exists()


def test_same_filename_from_two_albums_does_not_collide(tmp_path, state_db,
                                                        identity):
    """Every "01 Intro.mp3" in the collection used to land in one folder and
    be renamed "(2)", "(3)"... which is how a quarantine stops being a way
    back."""
    root = tmp_path / "music"
    for album in ("A", "B"):
        path = root / "Artist" / album / "01 Intro.mp3"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"audio")

    for album in ("A", "B"):
        loser = _copy("drop" + album, f"Artist/{album}/01 Intro.mp3")
        keeper = _copy("keep" + album, f"Artist/{album}/01 Intro.flac",
                       suffix="flac")
        duplicates.resolve(_group([keeper, loser]), "keep" + album, identity)

    quarantine = root / duplicates.QUARANTINE_NAME / "Artist"
    assert (quarantine / "A" / "01 Intro.mp3").is_file()
    assert (quarantine / "B" / "01 Intro.mp3").is_file()


def test_a_real_collision_still_does_not_overwrite(tmp_path, state_db, identity):
    root = tmp_path / "music"
    landing = root / duplicates.QUARANTINE_NAME / "Artist" / "Album"
    landing.mkdir(parents=True)
    (landing / "01 Song.mp3").write_bytes(b"first")

    source = root / "Artist" / "Album" / "01 Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"second")

    duplicates.resolve(
        _group([_copy("keep", "Artist/Album/01 Song.flac", suffix="flac"),
                _copy("drop", "Artist/Album/01 Song.mp3")]),
        "keep", identity)

    assert (landing / "01 Song.mp3").read_bytes() == b"first"
    assert (landing / "01 Song (2).mp3").read_bytes() == b"second"


# --- the undo trail --------------------------------------------------------

def test_resolve_records_what_it_moved(tmp_path, state_db, identity):
    root = tmp_path / "music"
    source = root / "Artist" / "Album" / "01 Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"audio")

    duplicates.resolve(
        _group([_copy("keep", "Artist/Album/01 Song.flac", suffix="flac"),
                _copy("drop", "Artist/Album/01 Song.mp3", title="Song")]),
        "keep", identity)

    rows = ledger.quarantined()
    assert len(rows) == 1
    row = rows[0]
    assert row["track_id"] == "drop"
    assert row["keeper_id"] == "keep"
    assert row["decided_by"] == "alex"
    assert Path(row["target_path"]).is_file(), (
        "the recorded target must be where the file actually is")
    assert row["restored_at"] is None


def test_resolve_reports_what_it_did(tmp_path, state_db, identity):
    """The browser used to discard this, so a failed move and a successful
    one looked identical: the group vanished from the list either way."""
    root = tmp_path / "music"
    source = root / "Artist" / "Album" / "01 Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"audio")

    outcome = duplicates.resolve(
        _group([_copy("keep", "Artist/Album/01 Song.flac", suffix="flac"),
                _copy("drop", "Artist/Album/01 Song.mp3", starred=True)]),
        "keep", identity)

    assert outcome["failed"] == []
    assert len(outcome["quarantined"]) == 1
    assert outcome["quarantined"][0]["path"] == "Artist/Album/01 Song.mp3"
    assert "starred" in outcome["migrated"]


def test_a_missing_file_is_reported_not_swallowed(tmp_path, state_db, identity):
    outcome = duplicates.resolve(
        _group([_copy("keep", "Artist/Album/01 Song.flac", suffix="flac"),
                _copy("drop", "Artist/Album/gone.mp3")]),
        "keep", identity)

    assert outcome["quarantined"] == []
    assert any("already gone" in f for f in outcome["failed"])
    assert ledger.quarantined() == [], "nothing moved, so nothing recorded"


# --- refusing ---------------------------------------------------------------

def test_refuses_before_moving_anything_when_a_star_cannot_migrate(
        tmp_path, state_db, identity):
    """Somebody else's star cannot be re-created through the API, so
    quarantining the file it hangs off would take it with it."""
    root = tmp_path / "music"
    source = root / "Artist" / "Album" / "01 Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"audio")

    with pytest.raises(ValueError, match="cannot move"):
        duplicates.resolve(
            _group([_copy("keep", "Artist/Album/01 Song.flac", suffix="flac"),
                    _copy("drop", "Artist/Album/01 Song.mp3",
                          starred_by_others="kelly")]),
            "keep", identity)

    assert source.is_file(), "refusing must leave the file where it was"
    assert ledger.quarantined() == []


def test_refuses_when_the_star_could_not_be_written_to_the_keeper(
        tmp_path, state_db, identity, monkeypatch):
    monkeypatch.setattr(navidrome, "star", lambda *a, **k: False)
    root = tmp_path / "music"
    source = root / "Artist" / "Album" / "01 Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"audio")

    with pytest.raises(ValueError, match="Could not move the star"):
        duplicates.resolve(
            _group([_copy("keep", "Artist/Album/01 Song.flac", suffix="flac"),
                    _copy("drop", "Artist/Album/01 Song.mp3", starred=True)]),
            "keep", identity)

    assert source.is_file()


def test_refuses_a_keeper_that_is_not_in_the_group(tmp_path, state_db, identity):
    with pytest.raises(ValueError, match="not in this group"):
        duplicates.resolve(_group([_copy("a"), _copy("b")]), "c", identity)


# --- grouping ---------------------------------------------------------------

def test_grouping_never_crosses_a_library(tmp_path, navidrome_db, identity,
                                          state_db, monkeypatch):
    """An early version grouped across libraries; a bulk resolve would have
    deleted Kelly's music to keep alex's higher-bitrate copy."""
    import sqlite3
    add_track(navidrome_db, "mine", library_id=1, mbz_recording_id="mb-1")
    add_track(navidrome_db, "hers", library_id=2, mbz_recording_id="mb-1")

    connection = sqlite3.connect(navidrome_db)
    connection.row_factory = sqlite3.Row
    groups = duplicates.find(connection, identity)
    connection.close()

    assert groups == [], "one track in each of two libraries is not a duplicate"


def test_a_dismissed_group_stays_dismissed(tmp_path, navidrome_db, identity,
                                           state_db):
    import sqlite3
    add_track(navidrome_db, "one", library_id=1, mbz_recording_id="mb-1",
              path="a.mp3")
    add_track(navidrome_db, "two", library_id=1, mbz_recording_id="mb-1",
              path="b.mp3")

    connection = sqlite3.connect(navidrome_db)
    connection.row_factory = sqlite3.Row
    found = duplicates.find(connection, identity)
    assert len(found) == 1

    ledger.dismiss_duplicate(found[0].dismiss_key)
    again = duplicates.find(connection, identity)
    connection.close()
    assert again == []


def test_a_copy_someone_else_starred_outranks_a_better_file():
    """Quality decides, but only among copies whose annotations can move."""
    theirs = _copy("theirs", suffix="mp3", bit_rate=128,
                   starred_by_others="kelly")
    better = _copy("better", suffix="flac", bit_rate=1000)
    ordered = sorted([better, theirs],
                     key=lambda c: duplicates._rank(c, None), reverse=True)
    assert ordered[0].id == "theirs"


# --- the read-only view of what was set aside ------------------------------

def test_survey_lists_files_with_the_record_joined_on(tmp_path, state_db,
                                                      identity):
    root = tmp_path / "music"
    source = root / "Artist" / "Album" / "01 Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"audio")
    duplicates.resolve(
        _group([_copy("keep", "Artist/Album/01 Song.flac", suffix="flac"),
                _copy("drop", "Artist/Album/01 Song.mp3", title="Airbag",
                      artist="Radiohead")]),
        "keep", identity)

    survey = duplicates.quarantine_survey(identity)

    assert survey["total"] == 1
    entry = survey["entries"][0]
    assert entry["title"] == "Airbag"
    assert entry["artist"] == "Radiohead"
    assert entry["was"] == str(Path("Artist/Album/01 Song.mp3"))
    assert entry["present"] is True
    assert entry["recorded"] is True
    assert entry["decided_by"] == "alex"


def test_survey_reports_a_file_nobody_recorded(tmp_path, state_db, identity):
    """Set aside by an older version, or moved here by hand. Showing it as an
    unexplained file beats not showing it at all."""
    stray = tmp_path / "music" / duplicates.QUARANTINE_NAME / "loose.mp3"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"audio")

    survey = duplicates.quarantine_survey(identity)

    assert survey["unrecorded"] == 1
    assert survey["entries"][0]["recorded"] is False


def test_survey_reports_a_record_whose_file_has_gone(tmp_path, state_db,
                                                     identity):
    """The difference between "set aside" and "actually gone"."""
    root = tmp_path / "music"
    source = root / "Artist" / "Album" / "01 Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"audio")
    duplicates.resolve(
        _group([_copy("keep", "Artist/Album/01 Song.flac", suffix="flac"),
                _copy("drop", "Artist/Album/01 Song.mp3")]),
        "keep", identity)

    moved = ledger.quarantined()[0]["target_path"]
    Path(moved).unlink()

    survey = duplicates.quarantine_survey(identity)
    assert survey["missing"] == 1
    assert survey["entries"][0]["present"] is False


def test_survey_ignores_the_ndignore_marker(tmp_path, state_db, identity):
    duplicates._quarantine_root(tmp_path / "music")
    assert duplicates.quarantine_survey(identity)["total"] == 0


def test_survey_is_empty_when_nothing_has_been_set_aside(tmp_path, state_db,
                                                         identity):
    survey = duplicates.quarantine_survey(identity)
    assert survey == {"entries": [], "total": 0, "bytes": 0, "missing": 0,
                      "unrecorded": 0, "truncated": False}


def test_survey_does_not_show_another_librarys_quarantine(tmp_path, state_db,
                                                          identity):
    hers = tmp_path / "kelly" / duplicates.QUARANTINE_NAME / "song.mp3"
    hers.parent.mkdir(parents=True)
    hers.write_bytes(b"audio")

    # alex can only see library 1, whose root is tmp_path/music.
    assert duplicates.quarantine_survey(identity)["total"] == 0
