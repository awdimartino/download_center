"""Filing a finished file straight into the library, at a frozen path.

Real MP3s in a real directory tree throughout. The bugs this replaces were
all code that looked right and did not run against a filesystem: a path
assembled from the wrong root, a move that overwrote what it was protecting,
a UUID chosen by counting neighbours.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3

from app import filer, registry, uuidtags

SILENCE = Path(__file__).parent / "fixtures" / "silence.mp3"


@pytest.fixture
def space(tmp_path, monkeypatch, state_db):
    # state_db because filing asks the registry which album a track is on,
    # and the registry is a table in state.db.
    from app import workspace
    from app.config import settings

    monkeypatch.setattr(settings, "output_dir", tmp_path / "untagged")
    monkeypatch.setattr(workspace, "CONFIG_DIR", tmp_path / "config")
    library = tmp_path / "music"
    library.mkdir()
    made = workspace.Workspace(username="alex", library_id=1,
                               library_name="Music", library_path=library)
    made.prepare()
    return made


def track(tmp_path, name="track.mp3", **tags) -> Path:
    """A real MP3 in scratch space, tagged as asked."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    path = scratch / name
    shutil.copy(SILENCE, path)
    if tags:
        audio = EasyID3(path)
        for key, value in tags.items():
            audio[key] = str(value)
        audio.save()
    return path


# --- where a file goes ------------------------------------------------------

def test_a_track_is_filed_under_its_album_artist_and_album(space, tmp_path):
    source = track(tmp_path, albumartist="The Beatles", album="Abbey Road",
                   title="Come Together", tracknumber="1")

    filed = filer.file_track(space, source)

    assert filed.path == (space.library_path / "The Beatles" / "Abbey Road"
                          / "01 - Come Together.mp3")
    assert filed.path.is_file()
    assert not source.exists()


def test_the_album_artist_decides_the_folder_not_the_track_artist(space,
                                                                  tmp_path):
    """A guest credited on one track must not split an album in two. That is
    the same trap that made beets read a downloaded Thriller as a Various
    Artists compilation."""
    source = track(tmp_path, albumartist="Michael Jackson",
                   artist="Michael Jackson, Paul McCartney", album="Thriller",
                   title="The Girl Is Mine", tracknumber="3")

    filed = filer.file_track(space, source)

    assert filed.path.parent == space.library_path / "Michael Jackson" / "Thriller"


def test_a_track_with_no_album_goes_to_unknown_album(space, tmp_path):
    source = track(tmp_path, artist="Aphex Twin", title="Avril 14th")

    filed = filer.file_track(space, source)

    assert filed.path == (space.library_path / "Aphex Twin" / "Unknown Album"
                          / "Avril 14th.mp3")


def test_a_fully_untagged_file_is_still_filed(space, tmp_path):
    """It used to sit in a holding directory unseen. In the library it is at
    least playable, and it turns up in the review list."""
    source = track(tmp_path, name="whatever.mp3")

    filed = filer.file_track(space, source)

    assert filed.path == (space.library_path / "Unknown Artist"
                          / "Unknown Album" / "whatever.mp3")


def test_non_album_is_gone(space, tmp_path):
    """It only ever existed because beets' singleton import ignores the album
    tag by design. A Spotify single is a one-track album and is filed as one."""
    source = track(tmp_path, albumartist="Charli xcx", album="360",
                   title="360", tracknumber="1")

    filed = filer.file_track(space, source)

    assert "Non-Album" not in filed.path.parts
    assert filed.path.parent.name == "360"


def test_a_single_disc_release_gets_no_disc_prefix(space, tmp_path):
    source = track(tmp_path, albumartist="The Beatles", album="Abbey Road",
                   title="Come Together", tracknumber="1", discnumber="1")

    filed = filer.file_track(space, source)

    assert filed.path.name == "01 - Come Together.mp3"


def test_a_multi_disc_release_carries_its_disc(space, tmp_path):
    source = track(tmp_path, albumartist="The Beatles",
                   album="The Beatles", title="Birthday",
                   tracknumber="1", discnumber="2/2")

    filed = filer.file_track(space, source)

    assert filed.path.name == "2-01 - Birthday.mp3"


def test_a_slash_in_a_track_number_is_read_as_the_number(space, tmp_path):
    source = track(tmp_path, albumartist="The Beatles", album="Abbey Road",
                   title="Something", tracknumber="2/17")

    filed = filer.file_track(space, source)

    assert filed.path.name == "02 - Something.mp3"


def test_an_untracked_file_is_named_for_its_title_alone(space, tmp_path):
    """Not "00 - Title". There are a lot of these - landing them in the
    library is the point of the redesign - and a library where every one
    sorts first under a fake track zero reads as broken."""
    source = track(tmp_path, albumartist="Artist", album="Album",
                   title="Some Song")

    filed = filer.file_track(space, source)

    assert filed.path.name == "Some Song.mp3"


def test_a_title_that_is_illegal_on_windows_is_sanitised(space, tmp_path):
    source = track(tmp_path, albumartist="AC\\DC", album="Back In Black",
                   title="Hells Bells", tracknumber="1")

    filed = filer.file_track(space, source)

    assert "\\" not in filed.path.parent.parent.name
    assert filed.path.parent.parent.parent == space.library_path


def test_the_file_keeps_its_own_extension(space, tmp_path):
    """Downloads are MP3, but the inbox takes whatever is dropped into it."""
    source = track(tmp_path, name="track.MP3", albumartist="Artist",
                   album="Album", title="Song", tracknumber="1")

    filed = filer.file_track(space, source)

    assert filed.path.suffix == ".mp3"


# --- identity ---------------------------------------------------------------

def test_both_uuids_are_written_before_the_file_lands(space, tmp_path):
    source = track(tmp_path, albumartist="The Beatles", album="Abbey Road",
                   title="Come Together", tracknumber="1")

    filed = filer.file_track(space, source)

    track_uuid, album_uuid = uuidtags.read(filed.path)
    assert track_uuid == filed.track_uuid
    assert album_uuid == filed.album_uuid


def test_two_tracks_of_one_album_share_an_album_uuid(space, tmp_path):
    one = filer.file_track(space, track(
        tmp_path, name="a.mp3", albumartist="The Beatles",
        album="Abbey Road", title="Come Together", tracknumber="1"))
    two = filer.file_track(space, track(
        tmp_path, name="b.mp3", albumartist="the beatles",
        album="Abbey Road ", title="Something", tracknumber="2"))

    assert one.album_uuid == two.album_uuid
    assert one.track_uuid != two.track_uuid


def test_a_track_joins_an_album_already_in_the_library(space, tmp_path):
    """Downloading track five of a record you already hold. Given a fresh
    album UUID it would arrive as a second, one-track copy of that album."""
    first = filer.file_track(space, track(
        tmp_path, name="a.mp3", albumartist="Mk.gee",
        album="A Museum of Contradiction", title="Are You Looking Up",
        tracknumber="1"))

    later = filer.file_track(space, track(
        tmp_path, name="b.mp3", albumartist="Mk.gee",
        album="A Museum of Contradiction", title="Alesis", tracknumber="5"))

    assert later.album_uuid == first.album_uuid


def test_two_different_albums_do_not_share_a_uuid(space, tmp_path):
    """745 files across 101 albums shared one UUID, because assignment keyed
    on the directory rather than on the album tag."""
    one = filer.file_track(space, track(
        tmp_path, name="a.mp3", albumartist="The Beatles",
        album="Abbey Road", title="Come Together", tracknumber="1"))
    two = filer.file_track(space, track(
        tmp_path, name="b.mp3", albumartist="The Beatles",
        album="Revolver", title="Taxman", tracknumber="1"))

    assert one.album_uuid != two.album_uuid


def test_a_track_uuid_already_on_the_file_is_never_replaced(space, tmp_path):
    """It carries the stars."""
    source = track(tmp_path, albumartist="The Beatles", album="Abbey Road",
                   title="Come Together", tracknumber="1")
    uuidtags.write(source, "33333333-3333-4333-8333-333333333333", None)

    filed = filer.file_track(space, source)

    assert filed.track_uuid == "33333333-3333-4333-8333-333333333333"


def test_an_album_uuid_already_on_disk_is_adopted(space, tmp_path):
    """The migration pass re-files music that is already in the library.
    Minting a second UUID beside the one on disk would split the record."""
    existing = "44444444-4444-4444-8444-444444444444"
    source = track(tmp_path, albumartist="The Beatles", album="Abbey Road",
                   title="Come Together", tracknumber="1")
    uuidtags.write(source, None, existing)

    filed = filer.file_track(space, source)

    assert filed.album_uuid == existing
    assert registry.known(space.library_id, filed.album_key) == existing


def test_the_registry_wins_over_a_stale_uuid_on_disk(space, tmp_path):
    """Two copies of one album arriving with different album UUIDs on them
    are one album here, and the registered value is the one they settle on."""
    first = filer.file_track(space, track(
        tmp_path, name="a.mp3", albumartist="The Beatles",
        album="Abbey Road", title="Come Together", tracknumber="1"))

    stale = track(tmp_path, name="b.mp3", albumartist="The Beatles",
                  album="Abbey Road", title="Something", tracknumber="2")
    uuidtags.write(stale, None, "55555555-5555-4555-8555-555555555555")

    filed = filer.file_track(space, stale)

    assert filed.album_uuid == first.album_uuid
    assert uuidtags.read(filed.path)[1] == first.album_uuid


# --- moving -----------------------------------------------------------------

def test_a_real_collision_is_numbered_not_overwritten(space, tmp_path):
    first = filer.file_track(space, track(
        tmp_path, name="a.mp3", albumartist="Artist", album="Album",
        title="Song", tracknumber="1"))
    second = filer.file_track(space, track(
        tmp_path, name="b.mp3", albumartist="Artist", album="Album",
        title="Song", tracknumber="1"))

    assert first.path.exists()
    assert second.path != first.path
    assert second.path.name == "01 - Song (2).mp3"


def test_filing_a_file_that_is_already_in_place_leaves_it_there(space,
                                                                tmp_path):
    """Idempotent, which is what lets the migration pass run over music that
    is already in the library."""
    filed = filer.file_track(space, track(
        tmp_path, albumartist="Artist", album="Album", title="Song",
        tracknumber="1"))

    again = filer.file_track(space, filed.path)

    assert again.path == filed.path
    assert again.track_uuid == filed.track_uuid
    assert again.album_uuid == filed.album_uuid
    assert list(filed.path.parent.iterdir()) == [filed.path]


def test_a_move_across_filesystems_still_works(space, tmp_path, monkeypatch):
    """Scratch space and the library are separate bind mounts under Docker,
    so os.replace raises EXDEV on almost every real move."""
    import errno
    import os as real_os

    def cross_device(source, target):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(filer.os, "replace", cross_device)
    source = track(tmp_path, albumartist="Artist", album="Album",
                   title="Song", tracknumber="1")

    filed = filer.file_track(space, source)

    assert filed.path.is_file()
    assert not source.exists()
    assert real_os.path.getsize(filed.path) > 0


# --- retagging --------------------------------------------------------------
#
# The one thing allowed to move a file after it is written, because a person
# confirmed it in the review page.

def retag(path: Path, **tags) -> None:
    audio = EasyID3(path)
    for key, value in tags.items():
        audio[key] = str(value)
    audio.save()


def test_a_retag_keeps_the_album_uuid(space, tmp_path):
    """The album keeps its Navidrome identity, so album-level stars and play
    counts survive and no file needs its UUID rewritten."""
    filed = filer.file_track(space, track(
        tmp_path, albumartist="Unknown Artist", album="Unknown Album",
        title="Come Together", tracknumber="1"))
    was = filer.album_key_of(filed.path.parent)

    retag(filed.path, albumartist="The Beatles", album="Abbey Road")
    settled = filer.after_retag(space, [filed.path], was)

    assert settled == filed.album_uuid
    assert uuidtags.read(filed.path)[0] == filed.track_uuid


def test_a_retag_moves_the_key_not_the_identity(space, tmp_path):
    filed = filer.file_track(space, track(
        tmp_path, albumartist="The Beatels", album="Abbey Road",
        title="Come Together", tracknumber="1"))
    was = filer.album_key_of(filed.path.parent)

    retag(filed.path, albumartist="The Beatles")
    filer.after_retag(space, [filed.path], was)

    assert registry.known(space.library_id, was) is None
    assert registry.album_uuid_for(space.library_id, "The Beatles",
                                   "Abbey Road") == filed.album_uuid


def test_a_retag_onto_an_album_already_there_merges_into_it(space, tmp_path):
    """Only the newcomer can be rewritten, so choosing its value would not
    move the established album - it would split it."""
    incumbent = filer.file_track(space, track(
        tmp_path, name="a.mp3", albumartist="The Beatles", album="Abbey Road",
        title="Come Together", tracknumber="1"))
    stray = filer.file_track(space, track(
        tmp_path, name="b.mp3", albumartist="Unknown Artist",
        album="Unknown Album", title="Something", tracknumber="2"))
    was = filer.album_key_of(stray.path.parent)

    retag(stray.path, albumartist="The Beatles", album="Abbey Road")
    settled = filer.after_retag(space, [stray.path], was)

    assert settled == incumbent.album_uuid
    assert uuidtags.read(stray.path)[1] == incumbent.album_uuid


def test_an_album_never_registered_keeps_the_uuid_on_its_files(space,
                                                               tmp_path):
    """The migration case: music filed before the registry existed carries a
    UUID and has no row. Minting a fresh one would orphan the stars."""
    existing = "66666666-6666-4666-8666-666666666666"
    path = track(tmp_path, albumartist="The Beatles", album="Abbey Rd",
                 title="Come Together", tracknumber="1")
    uuidtags.write(path, "77777777-7777-4777-8777-777777777777", existing)
    was = registry.album_key("The Beatles", "Abbey Rd")

    retag(path, album="Abbey Road")
    settled = filer.after_retag(space, [path], was)

    assert settled == existing
    assert registry.album_uuid_for(space.library_id, "The Beatles",
                                   "Abbey Road") == existing


def test_every_track_of_a_retagged_album_ends_up_on_one_uuid(space, tmp_path):
    one = filer.file_track(space, track(
        tmp_path, name="a.mp3", albumartist="A", album="X", title="One",
        tracknumber="1"))
    two = filer.file_track(space, track(
        tmp_path, name="b.mp3", albumartist="A", album="X", title="Two",
        tracknumber="2"))
    was = filer.album_key_of(one.path.parent)

    for path in (one.path, two.path):
        retag(path, albumartist="The Beatles", album="Abbey Road")
    filer.after_retag(space, [one.path, two.path], was)

    assert (uuidtags.read(one.path)[1] == uuidtags.read(two.path)[1]
            == one.album_uuid)


def test_a_retag_of_nothing_does_nothing(space, tmp_path):
    assert filer.after_retag(space, [], "some key") == ""
    assert registry.count(space.library_id) == 0


# --- a file that does not say what album it is on ---------------------------
#
# These all used to fold onto "Unknown Artist / Unknown Album", which is one
# registry key, which is one album UUID - so forty unrelated rips became one
# forty-track record in Navidrome. That is the failure the registry exists to
# prevent, reached from the other direction.

def test_two_untagged_files_are_two_albums(space, tmp_path):
    one = filer.file_track(space, track(tmp_path, name="a.mp3"))
    two = filer.file_track(space, track(tmp_path, name="b.mp3"))

    assert one.album_uuid != two.album_uuid
    assert one.album_key != two.album_key


def test_an_untagged_file_still_lands_in_unknown_album(space, tmp_path):
    """Its identity stands alone; where it goes is unchanged."""
    filed = filer.file_track(space, track(tmp_path, name="mystery.mp3"))

    assert filed.path == (space.library_path / "Unknown Artist"
                          / "Unknown Album" / "mystery.mp3")


def test_a_known_artist_with_no_album_is_still_its_own_record(space, tmp_path):
    """Two loose singles by one artist share a folder, not a record - they
    are unrelated tracks that happen to name the same person."""
    one = filer.file_track(space, track(tmp_path, name="a.mp3",
                                        artist="Aphex Twin", title="Avril 14th"))
    two = filer.file_track(space, track(tmp_path, name="b.mp3",
                                        artist="Aphex Twin", title="Xtal"))

    assert one.path.parent == two.path.parent
    assert one.album_uuid != two.album_uuid


def test_re_filing_an_untagged_file_keeps_its_identity(space, tmp_path):
    """Keyed on the track UUID, which is already on the file, so the second
    pass finds the same row rather than minting another."""
    filed = filer.file_track(space, track(tmp_path, name="mystery.mp3"))
    again = filer.file_track(space, filed.path)

    assert again.album_uuid == filed.album_uuid
    assert again.album_key == filed.album_key


def test_an_album_that_names_itself_still_groups(space, tmp_path):
    """The loose key is only for files with no album tag at all."""
    one = filer.file_track(space, track(
        tmp_path, name="a.mp3", albumartist="The Beatles", album="Abbey Road",
        title="Come Together", tracknumber="1"))
    two = filer.file_track(space, track(
        tmp_path, name="b.mp3", albumartist="The Beatles", album="Abbey Road",
        title="Something", tracknumber="2"))

    assert one.album_uuid == two.album_uuid


# --- one numbering rule, not two --------------------------------------------

def test_an_unused_name_is_the_target_when_it_is_free(tmp_path):
    assert filer.unused_name(tmp_path / "a.mp3") == tmp_path / "a.mp3"


def test_an_unused_name_numbers_past_what_is_taken(tmp_path):
    (tmp_path / "a.mp3").write_bytes(b"x")
    (tmp_path / "a (2).mp3").write_bytes(b"x")
    assert filer.unused_name(tmp_path / "a.mp3") == tmp_path / "a (3).mp3"


def test_running_out_of_names_raises_rather_than_overwriting(tmp_path):
    """The loop used to fall through and leave `target` at the original
    name, so the one case the numbering exists to prevent ended in the
    caller overwriting the file it was protecting."""
    (tmp_path / "a.mp3").write_bytes(b"x")
    for n in range(2, 100):
        (tmp_path / f"a ({n}).mp3").write_bytes(b"x")

    with pytest.raises(FileExistsError):
        filer.unused_name(tmp_path / "a.mp3")


def test_the_inbox_uses_the_filers_numbering(space, tmp_path):
    """The name settled on the way in is the name the filer then has to
    collide with, so one rule has to govern both."""
    from app import inbox

    assert inbox._move_in.__doc__ and "unused_name" in inbox._move_in.__doc__
    taken = space.inbox_dir / "x.mp3"
    space.inbox_dir.mkdir(parents=True, exist_ok=True)
    taken.write_bytes(b"x")
    source = track(tmp_path, name="x.mp3")

    assert inbox._move_in(source, taken).name == "x (2).mp3"
