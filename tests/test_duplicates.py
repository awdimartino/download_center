"""The module that moves files. Tested first, and hardest.

Nothing here is deleted, but "nothing is deleted" is a claim about where a
file ends up, and that is exactly what these check: on the right volume,
under a path that says where it came from, with a row in the ledger saying
what happened. The old code satisfied none of the three.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import duplicates, navidrome, store
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
        library="Music", held_by_others="",
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

    rows = store.quarantined()
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
    assert store.quarantined() == [], "nothing moved, so nothing recorded"


# --- quarantining a track chosen by hand, not as a duplicate's loser -------

def test_quarantine_one_moves_the_file_and_records_no_keeper(tmp_path,
                                                              state_db,
                                                              identity):
    root = tmp_path / "music"
    source = root / "Artist" / "Album" / "01 Song.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"audio")

    outcome = duplicates.quarantine_one(
        _copy("solo", "Artist/Album/01 Song.mp3"), identity)

    landed = root / duplicates.QUARANTINE_NAME / "Artist" / "Album" / "01 Song.mp3"
    assert landed.is_file()
    assert not source.exists()
    assert outcome["moved_to"] == str(landed)

    row = store.quarantined()[0]
    assert row["track_id"] == "solo"
    assert row["keeper_id"] is None
    assert row["keeper_path"] is None
    assert row["decided_by"] == "alex"


def test_quarantine_one_refuses_a_missing_file(tmp_path, state_db, identity):
    with pytest.raises(ValueError, match="already gone"):
        duplicates.quarantine_one(
            _copy("solo", "Artist/Album/gone.mp3"), identity)
    assert store.quarantined() == []


def test_quarantine_one_refuses_a_file_already_set_aside(tmp_path, state_db,
                                                          identity):
    buried = _copy("buried", "duplicates-removed/Artist/Album/01 Song.mp3")
    with pytest.raises(ValueError, match="already set aside"):
        duplicates.quarantine_one(buried, identity)


def test_quarantine_many_continues_past_one_failure(tmp_path, state_db,
                                                     identity):
    root = tmp_path / "music"
    for name in ("01 Song.mp3", "02 Song.mp3"):
        path = root / "Artist" / "Album" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")

    outcome = duplicates.quarantine_many([
        _copy("a", "Artist/Album/01 Song.mp3"),
        _copy("b", "Artist/Album/gone.mp3"),
        _copy("c", "Artist/Album/02 Song.mp3"),
    ], identity)

    assert [m["path"] for m in outcome["quarantined"]] == [
        "Artist/Album/01 Song.mp3", "Artist/Album/02 Song.mp3"]
    assert any("already gone" in f for f in outcome["failed"])
    assert len(store.quarantined()) == 2


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
                          held_by_others="kelly")]),
            "keep", identity)

    assert source.is_file(), "refusing must leave the file where it was"
    assert store.quarantined() == []


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

    store.dismiss_duplicate(found[0].dismiss_key)
    again = duplicates.find(connection, identity)
    connection.close()
    assert again == []


def test_a_copy_someone_else_starred_outranks_a_better_file():
    """Quality decides, but only among copies whose annotations can move."""
    theirs = _copy("theirs", suffix="mp3", bit_rate=128,
                   held_by_others="kelly")
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

    moved = store.quarantined()[0]["target_path"]
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


# --- the loop that made resolving look broken -----------------------------
# The quarantine sits inside the library tree and is kept out of the scan by
# an .ndignore marker. That marker has to be EMPTY: Navidrome reads a
# non-empty one as a list of glob patterns and skips only what matches. Ours
# carried three lines explaining itself, which matched nothing, so every
# file set aside was scanned straight back into the library, found as a
# duplicate of the copy it had just lost to, and moved one level deeper the
# next time somebody resolved it. From the page it looked like the button
# did nothing.

def test_the_ignore_marker_is_empty(tmp_path):
    """Not a style point. A non-empty marker skips only what its patterns
    match, and prose matches nothing."""
    path = duplicates._quarantine_root(tmp_path / "music")

    marker = path / duplicates.NDIGNORE
    assert marker.exists()
    assert marker.read_bytes() == b"", (
        "a non-empty .ndignore is a pattern list, not a skip")


def test_an_old_explanatory_marker_is_repaired(tmp_path):
    """The directories already on disk carry the text that did not work.
    Creating the marker only when missing would leave them broken for ever."""
    root = tmp_path / "music"
    path = root / duplicates.QUARANTINE_NAME
    path.mkdir(parents=True)
    (path / duplicates.NDIGNORE).write_text(
        "Copies set aside by Download Center as duplicates.\n", "utf-8")

    duplicates._quarantine_root(root)

    assert (path / duplicates.NDIGNORE).read_bytes() == b""


def test_the_explanation_survives_somewhere_readable(tmp_path):
    path = duplicates._quarantine_root(tmp_path / "music")
    readme = (path / duplicates.QUARANTINE_README).read_text(encoding="utf-8")
    assert "ndignore" in readme and "empty" in readme


def test_a_copy_already_set_aside_is_not_buried_deeper(tmp_path, state_db,
                                                        identity):
    """The move that made duplicates-removed/duplicates-removed. Navidrome
    lists the quarantined file, it pairs with the copy it lost to, and
    resolving moves it one directory further down."""
    root = tmp_path / "music"
    keeper = _copy("keep", "Artist/Album/01 Song.mp3")
    buried = _copy("buried",
                   "duplicates-removed/Artist/Album/01 Song.mp3")
    for copy in (keeper, buried):
        (root / copy.path).parent.mkdir(parents=True, exist_ok=True)
        (root / copy.path).write_bytes(b"x")

    outcome = duplicates.resolve(_group([keeper, buried], keeper), "keep",
                                 identity)

    assert outcome["quarantined"] == []
    assert "already set aside" in outcome["failed"][0]
    assert (root / buried.path).exists(), "left where it was"
    assert not (root / "duplicates-removed" / "duplicates-removed").exists()


def test_a_resolved_copy_leaves_the_page_before_navidrome_notices(
        tmp_path, state_db, identity, navidrome_db, monkeypatch):
    """Navidrome does not know a file moved until it rescans. Until then it
    still lists both copies, and the group came straight back."""
    from app.config import settings
    monkeypatch.setattr(settings, "navidrome_db", navidrome_db)
    for track_id, path in (("keep", "Artist/Album/01 Song.mp3"),
                           ("lose", "Artist/Other/01 Song.mp3")):
        add_track(navidrome_db, track_id, path=path, title="Song",
                  album="Album", artist="Artist", album_artist="Artist",
                  mbz_recording_id="mbid-1", library_id=1)

    connection = navidrome.open_db()
    with connection:
        assert len(duplicates.find(connection, identity)) == 1

        store.record_quarantine(
            group_key="k", copy=_copy("lose", "Artist/Other/01 Song.mp3"),
            keeper=_copy("keep"), source="/music/Artist/Other/01 Song.mp3",
            target="/music/duplicates-removed/Artist/Other/01 Song.mp3",
            decided_by="alex")

        assert duplicates.find(connection, identity) == [], (
            "the resolved copy is still in Navidrome's index, but it is not "
            "in the library any more and must not be offered again")


# --- other people's ratings and plays (CODE_REVIEW M30) ---------------------
# Only another person's star protected a copy, so their rating and their
# play count went into the quarantine with it, and the caller's own play
# count was never mentioned.

def _with_plays(db):
    import sqlite3

    connection = sqlite3.connect(db)
    with connection:
        connection.execute("ALTER TABLE annotation ADD COLUMN play_count INTEGER DEFAULT 0")
        connection.executemany(
            "INSERT INTO annotation (user_id, item_id, item_type, starred, rating,"
            " play_count) VALUES (?, ?, 'media_file', 0, ?, ?)",
            [("u-kelly", "rated", 4, 0), ("u-kelly", "played", 0, 7),
             ("u-alex", "played", 0, 3)])
    connection.close()


def test_another_persons_rating_or_plays_protects_a_copy(navidrome_db, identity):
    import sqlite3

    for track in ("rated", "played", "plain"):
        add_track(navidrome_db, track, library_id=1)
    _with_plays(navidrome_db)

    connection = sqlite3.connect(navidrome_db)
    loaded = {c.id: c for c in duplicates._load(connection, identity)}
    connection.close()

    assert loaded["rated"].held_by_others == "kelly"
    assert loaded["played"].held_by_others == "kelly"
    assert loaded["plain"].held_by_others == ""
    assert loaded["played"].plays == 3


def test_a_copy_someone_else_rated_is_refused(tmp_path, state_db, identity):
    keeper = _copy("keep")
    rated = _copy("gone", path="Artist/Album/02 Song.mp3", held_by_others="kelly")

    with pytest.raises(ValueError, match="rated or played by kelly"):
        duplicates.resolve(_group([keeper, rated], keeper), "keep", identity)
