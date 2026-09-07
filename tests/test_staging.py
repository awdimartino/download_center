"""Naming files, and getting them into place without losing one."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import staging


# --- sanitising -------------------------------------------------------------

def test_a_backslash_is_stripped():
    """It was not. Inside the character class `\\|` escaped the pipe, so the
    backslash itself never joined the set - and a title like "AC\\DC" came
    through untouched, to be read as a directory separator on Windows."""
    assert "\\" not in staging.sanitize("AC\\DC - Back In Black")


@pytest.mark.parametrize("char", list('<>:"/\\|?*'))
def test_every_character_windows_forbids_is_stripped(char):
    assert char not in staging.sanitize(f"a{char}b")


def test_control_characters_are_stripped():
    assert staging.sanitize("a\x00\x1fb") == "a__b"


def test_brackets_survive_because_they_are_legal():
    """Sanitising these would mangle "Song [Remix]", which is a real title on
    both filesystems. The glob that could not cope with them was the bug."""
    assert staging.sanitize("Song [Remix]") == "Song [Remix]"


def test_a_reserved_device_name_is_prefixed():
    assert staging.sanitize("CON").startswith("_")
    assert staging.sanitize("com1.mp3").startswith("_")


def test_a_trailing_dot_or_space_is_removed():
    assert staging.sanitize("Album.") == "Album"
    assert staging.sanitize("Album ") == "Album"


def test_a_long_name_is_truncated_without_a_trailing_dot():
    got = staging.sanitize("x" * 200 + ".")
    assert len(got) <= staging.MAX_COMPONENT
    assert not got.endswith((".", " "))


def test_an_empty_name_still_produces_something_usable():
    assert staging.sanitize("") == "unknown"
    assert staging.sanitize(None) == "unknown"
    # Not "unknown": every character was illegal rather than absent, so it
    # becomes "___" - still a legal single component, which is the contract.
    assert staging.sanitize("///") == "___"


def test_a_sanitised_name_is_one_path_component():
    """The whole point: this value is joined onto a directory."""
    for awkward in ("AC\\DC", "a/b", "x:y", "p|q"):
        assert len(Path(staging.sanitize(awkward)).parts) == 1


# --- moving into place ------------------------------------------------------

def test_a_collision_is_numbered_rather_than_overwritten(tmp_path):
    target = tmp_path / "Song.mp3"
    target.write_bytes(b"original")
    source = tmp_path / "incoming.mp3"
    source.write_bytes(b"new")

    landed = staging._move_into_place(source, target)

    assert target.read_bytes() == b"original"
    assert landed.name == "Song (2).mp3"
    assert landed.read_bytes() == b"new"


def test_running_out_of_names_raises_rather_than_overwriting(tmp_path):
    """The loop used to leave `target` at the original name when all 98 were
    taken, so the one case the numbering exists for ended in os.replace
    destroying the file it was protecting."""
    target = tmp_path / "Song.mp3"
    target.write_bytes(b"original")
    for n in range(2, 100):
        (tmp_path / f"Song ({n}).mp3").write_bytes(b"taken")
    source = tmp_path / "incoming.mp3"
    source.write_bytes(b"new")

    with pytest.raises(FileExistsError):
        staging._move_into_place(source, target)

    assert target.read_bytes() == b"original"
    assert source.is_file(), "a refused move must leave the source alone"


def test_a_move_creates_the_parent_directory(tmp_path):
    source = tmp_path / "incoming.mp3"
    source.write_bytes(b"new")
    target = tmp_path / "albums" / "Artist - Album" / "01 Song.mp3"

    landed = staging._move_into_place(source, target)
    assert landed == target
    assert target.is_file()


# --- naming -----------------------------------------------------------------

def test_a_multi_disc_track_carries_its_disc_number():
    item = {"title": "Song", "artist": "A", "track_no": 4, "disc_no": 2}
    assert staging.track_filename(item, multi_disc=True).startswith("2-04 ")
    assert staging.track_filename(item, multi_disc=False).startswith("04 ")


def test_a_track_with_no_number_still_produces_a_filename():
    item = {"title": "Song", "artist": "A", "track_no": None, "disc_no": None}
    assert staging.track_filename(item, multi_disc=False).endswith(".mp3")


# --- where a track is staged ------------------------------------------------
# One folder per album, decided by what the track says it is on. A singleton
# import files to `Non-Album/$artist/$title` whatever the album tag says -
# measured against beets, not assumed - so sending a fragment to singles put
# it nowhere near the rest of its record.

def _item(number, *, album="Thriller", album_id="a1", total=9, title=None):
    return {
        "id": f"i{number}", "album_id": album_id, "album": album,
        "album_total": total, "album_artist": "Michael Jackson",
        "artist": "Michael Jackson", "title": title or f"Track {number}",
        "track_no": number, "disc_no": 1,
    }


def _space(tmp_path, monkeypatch):
    from app import workspace
    from app.config import settings

    monkeypatch.setattr(settings, "output_dir", tmp_path / "untagged")
    monkeypatch.setattr(workspace, "CONFIG_DIR", tmp_path / "config")
    library = tmp_path / "music"
    library.mkdir()
    space = workspace.Workspace(username="alex", library_id=1,
                                library_name="Music", library_path=library)
    space.prepare()
    return space


def test_a_complete_album_is_staged_as_an_album(tmp_path, monkeypatch):
    space = _space(tmp_path, monkeypatch)
    items = [_item(n) for n in range(1, 10)]

    layout = staging.plan(space, "job1", items)

    assert all(space.albums_dir in plan["final"].parents
               for plan in layout.values())
    assert all(plan["complete_album"] for plan in layout.values())


def test_a_fragment_is_still_staged_under_its_album(tmp_path, monkeypatch):
    """Two tracks of a twelve track record. Beets will not match that
    unattended and they wait for someone to choose the release - but they
    wait *together*, under the album they belong to."""
    space = _space(tmp_path, monkeypatch)
    items = [_item(1), _item(2)]

    layout = staging.plan(space, "job1", items)

    for plan in layout.values():
        assert plan["final"].parent.name == "Michael Jackson - Thriller"
        assert plan["complete_album"] is False


def test_a_track_with_no_album_is_a_single(tmp_path, monkeypatch):
    """A yt-dlp download of something that is not on a record. Non-Album is
    where that belongs and a single is what it is."""
    space = _space(tmp_path, monkeypatch)
    loose = _item(1, album=None, album_id=None, total=0)

    layout = staging.plan(space, "job1", [loose])

    assert layout["i1"]["final"].parent == space.singles_dir


# --- regrouping what is already there ---------------------------------------

def _staged(path, album=None, artist="Michael Jackson", albumartist=None):
    import shutil

    from mutagen.easyid3 import EasyID3

    fixture = Path(__file__).parent / "fixtures" / "silence.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, path)
    tags = EasyID3(path)
    tags["title"] = path.stem
    tags["artist"] = artist
    if albumartist:
        tags["albumartist"] = albumartist
    if album:
        tags["album"] = album
    tags.save()
    return path


def test_loose_singles_are_grouped_into_their_album(tmp_path, monkeypatch):
    space = _space(tmp_path, monkeypatch)
    _staged(space.singles_dir / "one.mp3", album="Thriller")
    _staged(space.singles_dir / "two.mp3", album="Thriller")

    staging.regroup(space)

    folder = space.albums_dir / "Michael Jackson - Thriller"
    assert sorted(p.name for p in folder.iterdir()) == ["one.mp3", "two.mp3"]
    assert not any(space.singles_dir.iterdir())


def test_a_dump_of_many_albums_is_split_up(tmp_path, monkeypatch):
    """The case this was written for: a folder of loose tracks spanning a
    hundred albums, which staging handed to beets as a single release."""
    space = _space(tmp_path, monkeypatch)
    dump = space.albums_dir / "8-28"
    _staged(dump / "a.mp3", album="Thriller")
    _staged(dump / "b.mp3", album="Bad")

    staging.regroup(space)

    assert (space.albums_dir / "Michael Jackson - Thriller" / "a.mp3").exists()
    assert (space.albums_dir / "Michael Jackson - Bad" / "b.mp3").exists()
    assert not dump.exists(), "the emptied folder is not left to be imported"


def test_an_untagged_file_stays_a_single(tmp_path, monkeypatch):
    space = _space(tmp_path, monkeypatch)
    _staged(space.singles_dir / "mystery.mp3", album=None)

    staging.regroup(space)

    assert (space.singles_dir / "mystery.mp3").exists()


def test_grouping_uses_the_album_artist(tmp_path, monkeypatch):
    """A guest on one track must not split an album in two - the same trap
    that made beets read a downloaded Thriller as a compilation."""
    space = _space(tmp_path, monkeypatch)
    _staged(space.singles_dir / "solo.mp3", album="Thriller",
            artist="Michael Jackson", albumartist="Michael Jackson")
    _staged(space.singles_dir / "duet.mp3", album="Thriller",
            artist="Michael Jackson, Paul McCartney",
            albumartist="Michael Jackson")

    staging.regroup(space)

    folder = space.albums_dir / "Michael Jackson - Thriller"
    assert sorted(p.name for p in folder.iterdir()) == ["duet.mp3", "solo.mp3"]


def test_regrouping_twice_changes_nothing(tmp_path, monkeypatch):
    """It runs before every import, so it has to be a no-op once settled."""
    space = _space(tmp_path, monkeypatch)
    _staged(space.singles_dir / "one.mp3", album="Thriller")
    staging.regroup(space)

    second = staging.regroup(space)

    assert second == {"grouped": 0, "singles": 0}
    assert (space.albums_dir / "Michael Jackson - Thriller" / "one.mp3").exists()


def test_a_guest_on_the_album_artist_does_not_split_the_album(tmp_path,
                                                              monkeypatch):
    """The Thriller trap one level up. A dump of real files carries "Drake",
    "Drake, Detail" and "Drake, JAŸ-Z" as the *album* artist of one record;
    grouping on the raw string filed Nothing Was The Same into three
    folders. Measured on the real backlog: 123 folders for 102 albums."""
    space = _space(tmp_path, monkeypatch)
    for name, credited in (("a.mp3", "Drake"),
                           ("b.mp3", "Drake, Detail"),
                           ("c.mp3", "Drake, JAY-Z")):
        _staged(space.singles_dir / name, album="Nothing Was The Same",
                artist=credited, albumartist=credited)

    staging.regroup(space)

    folders = [p.name for p in space.albums_dir.iterdir() if p.is_dir()]
    assert folders == ["Drake - Nothing Was The Same"]


def test_an_artist_with_a_comma_in_their_name_survives(tmp_path, monkeypatch):
    """Nothing is split on a comma. "Tyler, The Creator" is one artist."""
    space = _space(tmp_path, monkeypatch)
    _staged(space.singles_dir / "a.mp3", album="IGOR",
            artist="Tyler, The Creator", albumartist="Tyler, The Creator")

    staging.regroup(space)

    assert (space.albums_dir / "Tyler, The Creator - IGOR" / "a.mp3").exists()


def test_genuinely_different_artists_stay_apart(tmp_path, monkeypatch):
    """A soundtrack with three composers shares no prefix. Keeping the parts
    separate is the safe way to be wrong: they wait for review rather than
    being merged into a record that does not exist."""
    space = _space(tmp_path, monkeypatch)
    _staged(space.singles_dir / "a.mp3", album="Greatest Hits",
            artist="Queen", albumartist="Queen")
    _staged(space.singles_dir / "b.mp3", album="Greatest Hits",
            artist="Sade", albumartist="Sade")

    staging.regroup(space)

    folders = sorted(p.name for p in space.albums_dir.iterdir() if p.is_dir())
    assert folders == ["Queen - Greatest Hits", "Sade - Greatest Hits"]


def test_a_track_called_dots_is_not_mistaken_for_a_dotfile(tmp_path,
                                                           monkeypatch):
    """Portraits Of Tracy have tracks called "..." and "... (Continued)".
    A `name.startswith(".")` guard reads those as dotfiles and skips them,
    which left two real songs sitting in staging with nothing to explain
    why - found only by counting the files in and the files out."""
    space = _space(tmp_path, monkeypatch)
    _staged(space.singles_dir / "... - Portraits Of Tracy.mp3",
            album="Drive Home", artist="Portraits Of Tracy")

    staging.regroup(space)

    assert (space.albums_dir / "Portraits Of Tracy - Drive Home"
            / "... - Portraits Of Tracy.mp3").exists()


def test_the_owner_marker_is_left_alone(tmp_path, monkeypatch):
    """The reason the dot check existed. It is a suffix question, not a
    name question: .owner is not audio."""
    space = _space(tmp_path, monkeypatch)
    marker = space.staging / ".owner"

    staging.regroup(space)

    assert marker.exists()


# --- editions of one record -------------------------------------------------
# Tags disagree about editions far more often than they disagree about
# records. Splitting on that hands beets two fragments where it could have
# had one album to match, which is how one Eagles record became three
# folders in staging.

def test_a_remaster_is_the_same_album(tmp_path, monkeypatch):
    """The real case: "One of These Nights" and "One of These Nights (2013
    Remaster)" as two folders, seven files and nine, neither matchable."""
    space = _space(tmp_path, monkeypatch)
    _staged(space.singles_dir / "a.mp3", album="One of These Nights",
            artist="Eagles", albumartist="Eagles")
    _staged(space.singles_dir / "b.mp3",
            album="One of These Nights (2013 Remaster)",
            artist="Eagles", albumartist="Eagles")

    staging.regroup(space)

    folders = [p.name for p in space.albums_dir.iterdir() if p.is_dir()]
    assert folders == ["Eagles - One of These Nights"], \
        "the folder keeps the plain title, not the remaster suffix"


def test_discs_of_one_album_come_together(tmp_path, monkeypatch):
    space = _space(tmp_path, monkeypatch)
    _staged(space.singles_dir / "a.mp3", album="The Wall (Disc 1)",
            artist="Pink Floyd", albumartist="Pink Floyd")
    _staged(space.singles_dir / "b.mp3", album="The Wall (Disc 2)",
            artist="Pink Floyd", albumartist="Pink Floyd")

    staging.regroup(space)

    folders = [p.name for p in space.albums_dir.iterdir() if p.is_dir()]
    assert len(folders) == 1


def test_a_live_album_is_a_different_record(tmp_path, monkeypatch):
    """Over-merging is the worse mistake: it invents an album that does not
    exist. Only markers meaning "another pressing of this" are stripped."""
    space = _space(tmp_path, monkeypatch)
    _staged(space.singles_dir / "a.mp3", album="Unplugged",
            artist="Nirvana", albumartist="Nirvana")
    _staged(space.singles_dir / "b.mp3", album="Unplugged (Live)",
            artist="Nirvana", albumartist="Nirvana")

    staging.regroup(space)

    assert len({p.name for p in space.albums_dir.iterdir() if p.is_dir()}) == 2


def test_a_numbered_sequel_is_a_different_record(tmp_path, monkeypatch):
    space = _space(tmp_path, monkeypatch)
    _staged(space.singles_dir / "a.mp3", album="Greatest Hits",
            artist="Queen", albumartist="Queen")
    _staged(space.singles_dir / "b.mp3", album="Greatest Hits Vol. 2",
            artist="Queen", albumartist="Queen")

    staging.regroup(space)

    assert len({p.name for p in space.albums_dir.iterdir() if p.is_dir()}) == 2


def test_a_missing_album_artist_still_groups(tmp_path, monkeypatch):
    """The other half of the Eagles split: seven of the files carried no
    album artist at all, so they fell back to the track artist and landed
    somewhere else."""
    space = _space(tmp_path, monkeypatch)
    _staged(space.singles_dir / "a.mp3", album="One of These Nights",
            artist="Eagles", albumartist=None)
    _staged(space.singles_dir / "b.mp3",
            album="One of These Nights (2013 Remaster)",
            artist="Eagles", albumartist="Eagles")

    staging.regroup(space)

    folders = [p.name for p in space.albums_dir.iterdir() if p.is_dir()]
    assert folders == ["Eagles - One of These Nights"]
