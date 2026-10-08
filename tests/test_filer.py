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

    real_replace = real_os.replace

    def cross_device(source, target):
        # A rename within one folder never crosses a filesystem.
        if Path(source).parent != Path(target).parent:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_replace(source, target)

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
    assert registry.known(space.library_id, registry.album_key(
        "The Beatles", "Abbey Road")) == filed.album_uuid


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
    assert registry.known(space.library_id, registry.album_key(
        "The Beatles", "Abbey Road")) == existing


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


# --- editing tags by hand ---------------------------------------------------
#
# The two cases this exists for: an album whose artist and title were both
# crammed into the track title, and clearing files out of Unknown Album.

def album_on_disk(space, artist, name, titles, tracked=True):
    """A filed album, as the filer itself would have written it."""
    filed = []
    for n, title in enumerate(titles, start=1):
        source = track(tmp_of(space), name=f"{name}-{n}.mp3",
                       albumartist=artist, album=name, title=title,
                       tracknumber=str(n))
        filed.append(filer.file_track(space, source))
    return filed


def tmp_of(space):
    return space.library_path.parent


def test_renaming_an_album_moves_every_file(space):
    filed = album_on_disk(space, "Unknown Artist", "Unknown Album",
                          ["One", "Two"])
    folder = filed[0].path.parent

    moved = filer.retag_album(space, folder, albumartist="Boards of Canada",
                              album="Music Has the Right to Children")

    assert len(moved) == 2
    for one in moved:
        assert one.path.parent == (space.library_path / "Boards of Canada"
                                   / "Music Has the Right to Children")
        assert one.path.is_file()


def test_renaming_an_album_keeps_its_identity(space):
    """The album keeps its Navidrome identity, so album-level stars and
    play counts survive the change."""
    filed = album_on_disk(space, "Unkown Artist", "Abbey Road", ["One", "Two"])
    before = filed[0].album_uuid

    moved = filer.retag_album(space, filed[0].path.parent,
                              albumartist="The Beatles", album="Abbey Road")

    assert {one.album_uuid for one in moved} == {before}


def test_renaming_an_album_onto_one_that_exists_merges_into_it(space):
    """Only the newcomer can be rewritten, so the established record wins."""
    incumbent = album_on_disk(space, "The Beatles", "Abbey Road", ["Come"])
    stray = album_on_disk(space, "The Beatels", "Abbey Road", ["Something"])

    moved = filer.retag_album(space, stray[0].path.parent,
                              albumartist="The Beatles", album="Abbey Road")

    assert moved[0].album_uuid == incumbent[0].album_uuid


def test_the_emptied_folder_is_removed(space):
    filed = album_on_disk(space, "Unknown Artist", "Unknown Album", ["One"])
    was = filed[0].path.parent

    filer.retag_album(space, was, albumartist="Real", album="Record")

    assert not was.exists()
    assert not was.parent.exists(), "the empty artist folder goes too"


def test_a_folder_that_still_holds_something_is_kept(space):
    """A cover with an identical copy where the tracks went has moved with
    them (M11). Anything else is somebody else's file."""
    filed = album_on_disk(space, "Unknown Artist", "Unknown Album", ["One"])
    was = filed[0].path.parent
    (was / "notes.txt").write_text("rip log", encoding="utf-8")

    filer.retag_album(space, was, albumartist="Real", album="Record")

    assert was.is_dir(), "somebody else's file is not ours to delete"


# --- one track at a time ----------------------------------------------------

def test_retitling_a_track_renames_it_in_place(space):
    filed = album_on_disk(space, "The Beatles", "Abbey Road", ["Wrong Title"])

    moved = filer.retag_track(space, filed[0].path, title="Come Together")

    assert moved.path.name == "01 - Come Together.mp3"
    assert moved.path.parent == filed[0].path.parent
    assert moved.album_uuid == filed[0].album_uuid


def test_changing_a_tracks_album_moves_only_that_track(space):
    """The case for clearing out Unknown Album: one file at a time, out of
    the pile and into the record it belongs to."""
    pile = album_on_disk(space, "Unknown Artist", "Unknown Album",
                         ["One", "Two"])

    moved = filer.retag_track(space, pile[0].path,
                              albumartist="Aphex Twin", album="Drukqs")

    assert moved.path.parent == space.library_path / "Aphex Twin" / "Drukqs"
    assert pile[1].path.is_file(), "the other one did not move"


def test_a_track_moved_into_an_album_joins_its_uuid(space):
    existing = album_on_disk(space, "Aphex Twin", "Drukqs", ["Avril 14th"])
    stray = album_on_disk(space, "Unknown Artist", "Unknown Album", ["Xtal"])

    moved = filer.retag_track(space, stray[0].path,
                              albumartist="Aphex Twin", album="Drukqs")

    assert moved.album_uuid == existing[0].album_uuid


def test_a_moved_track_does_not_drag_its_old_album_uuid_with_it(space):
    """The subtle one. The UUID on the file belongs to the album it is
    leaving; registering that under the new name would fuse two records
    rather than move one track."""
    pile = album_on_disk(space, "Unknown Artist", "Unknown Album",
                         ["One", "Two"])
    left_behind = pile[1].album_uuid

    moved = filer.retag_track(space, pile[0].path,
                              albumartist="Real", album="Record")

    assert moved.album_uuid != left_behind
    assert registry.known(space.library_id,
                          registry.album_key("Real", "Record")) \
        == moved.album_uuid


def test_a_track_number_edit_keeps_the_total_beside_it(space):
    """Dropping the "/12" would change how the release reads and rename
    every file on it."""
    source = track(tmp_of(space), albumartist="A", album="B", title="C",
                   tracknumber="3/12", discnumber="1/2")
    filed = filer.file_track(space, source)

    filer.write_tags(filed.path, track_no=4)

    from mutagen.easyid3 import EasyID3
    assert EasyID3(filed.path)["tracknumber"] == ["4/12"]
    assert EasyID3(filed.path)["discnumber"] == ["1/2"]


def test_editing_leaves_the_other_tags_alone(space):
    """Surgical on purpose: `tagger.tag` deletes the frame set before
    writing, which would take the cover art and the Spotify id with it."""
    from mutagen.id3 import ID3, TXXX

    source = track(tmp_of(space), albumartist="A", album="B", title="C",
                   tracknumber="1")
    filed = filer.file_track(space, source)
    tags = ID3(filed.path)
    tags.add(TXXX(encoding=3, desc="SPOTIFY_ID", text="abc123"))
    tags.save(filed.path)

    filer.write_tags(filed.path, title="Renamed")

    after = ID3(filed.path)
    assert [f.text[0] for f in after.getall("TXXX")
            if f.desc == "SPOTIFY_ID"] == ["abc123"]


def test_a_track_uuid_survives_every_edit(space):
    """It carries the stars. Nothing here may touch it."""
    filed = album_on_disk(space, "A", "B", ["C"])
    before = filed[0].track_uuid

    renamed = filer.retag_track(space, filed[0].path, title="D")
    moved = filer.retag_track(space, renamed.path, albumartist="E", album="F")

    assert moved.track_uuid == before


def test_setting_the_artist_of_an_untagged_track_moves_it(space):
    """`read_meta` falls back to the track artist when there is no album
    artist, so setting the artist decides the folder. A "did they pass an
    album field" test would have missed this and kept the stale album UUID.

    Genuinely untagged, not tagged with the words "Unknown Artist" - that
    is why such a file lands in that folder, and the two behave
    differently.
    """
    loose = filer.file_track(space, track(tmp_of(space), name="x.mp3",
                                          title="Avril 14th"))
    stale = loose.album_uuid
    assert loose.path.parent.parent.name == "Unknown Artist"

    moved = filer.retag_track(space, loose.path, artist="Aphex Twin")

    assert moved.path.parent.parent.name == "Aphex Twin"
    # Still its own record: it gained an artist, not an album, so it is the
    # same loose track in a better-named folder and its identity holds.
    assert moved.album_uuid == stale


def test_setting_the_artist_of_a_real_album_does_not_move_it(space):
    """A guest credited on one track is not a different album."""
    filed = album_on_disk(space, "The Beatles", "Abbey Road", ["One", "Two"])
    was = filed[0].path.parent

    moved = filer.retag_track(space, filed[0].path,
                              artist="The Beatles, Billy Preston")

    assert moved.path.parent == was
    assert moved.album_uuid == filed[0].album_uuid


def test_a_track_can_become_its_own_single(space):
    """What "As its own single" does: an album of one, which is how Spotify
    presents a single and how the filer files it."""
    pile = album_on_disk(space, "Unknown Artist", "Unknown Album",
                         ["Avril 14th", "Other"])

    moved = filer.retag_track(space, pile[0].path,
                              albumartist="Aphex Twin", album="Avril 14th")

    assert moved.path == (space.library_path / "Aphex Twin" / "Avril 14th"
                          / "01 - Avril 14th.mp3")
    assert pile[1].path.is_file()


def test_the_track_artist_is_writable(space):
    from mutagen.easyid3 import EasyID3

    filed = album_on_disk(space, "A", "B", ["C"])
    filer.write_tags(filed[0].path, artist="Someone Else")

    assert EasyID3(filed[0].path)["artist"] == ["Someone Else"]


# --- folder art -------------------------------------------------------------
# Filing moves audio and nothing else, so an album dropped in as a directory
# arrived in the library with no art unless something was embedded. Navidrome
# reads a folder cover, so this is a wall of album squares against grey ones.

def test_a_folder_cover_follows_its_album(space):
    source = track(tmp_of(space), name="01.mp3", albumartist="Boards",
                   album="Geogaddi", title="Sixtyten")
    (source.parent / "cover.jpg").write_bytes(b"\xff\xd8\xff not really")

    filed = filer.file_track(space, source)

    assert (filed.path.parent / "cover.jpg").read_bytes().startswith(b"\xff")


def test_the_cover_is_copied_not_moved(space):
    """The source folder may still hold tracks nobody has filed yet, and the
    next one finding no cover is the same bug one file later."""
    first = track(tmp_of(space), name="01.mp3", albumartist="Boards",
                  album="Geogaddi", title="Sixtyten")
    cover = first.parent / "cover.jpg"
    cover.write_bytes(b"art")

    filer.file_track(space, first)

    assert cover.exists()


def test_an_album_already_holding_art_keeps_it(space):
    one = track(tmp_of(space), name="01.mp3", albumartist="Boards",
                album="Geogaddi", title="Sixtyten")
    (one.parent / "cover.jpg").write_bytes(b"first")
    filed = filer.file_track(space, one)

    two = track(tmp_of(space), name="02.mp3", albumartist="Boards",
                album="Geogaddi", title="Julie and Candy")
    (two.parent / "cover.jpg").write_bytes(b"second")
    filer.file_track(space, two)

    assert (filed.path.parent / "cover.jpg").read_bytes() == b"first"


def test_a_missing_cover_is_not_a_problem(space):
    source = track(tmp_of(space), name="01.mp3", albumartist="Boards",
                   album="Geogaddi", title="Sixtyten")

    filed = filer.file_track(space, source)

    assert filed.path.exists()
    assert not (filed.path.parent / "cover.jpg").exists()


def test_other_files_in_the_folder_are_left_behind(space):
    """Only art. Rip logs and cue sheets are residue, not part of the album."""
    source = track(tmp_of(space), name="01.mp3", albumartist="Boards",
                   album="Geogaddi", title="Sixtyten")
    (source.parent / "rip.log").write_text("eac", encoding="utf-8")
    (source.parent / "album.cue").write_text("cue", encoding="utf-8")

    filed = filer.file_track(space, source)

    assert not (filed.path.parent / "rip.log").exists()
    assert not (filed.path.parent / "album.cue").exists()


# --- a retag only some of the files took ------------------------------------
# after_retag used to read the first file and stamp the settled UUID on every
# file in the folder. A file beets left unmatched kept its old tags and got
# the new album's identity - two names, one album UUID (CODE_REVIEW H2).

def test_a_retag_only_some_files_took_is_refused_untouched(space):
    filed = album_on_disk(space, "Old Artist", "Old Album", ["One", "Two", "Three"])
    folder = filed[0].path.parent
    was = filer.album_key_of(folder)
    for one in filed[:2]:
        retag(one.path, albumartist="New Artist", album="New Album")

    with pytest.raises(filer.NotEditable, match="no longer agree"):
        filer.after_retag(space, filer.audio_in(folder), was)

    assert {uuidtags.read(one.path)[1] for one in filed} == {filed[0].album_uuid}
    assert registry.known(space.library_id, was) == filed[0].album_uuid
    assert registry.known(
        space.library_id, registry.album_key("New Artist", "New Album")) is None


def test_a_file_with_no_album_is_not_given_the_albums_identity(space, tmp_path):
    filed = album_on_disk(space, "Artist", "Record", ["One"])
    folder = filed[0].path.parent
    stray = folder / "stray.mp3"
    shutil.copy(track(tmp_path, name="stray-src.mp3", title="Stray"), stray)
    was = filer.album_key_of(folder)
    retag(filed[0].path, album="Record (Remastered)")

    settled = filer.after_retag(space, filer.audio_in(folder), was)

    assert settled == filed[0].album_uuid
    assert uuidtags.read(stray)[1] != settled


# A download is moved to the inbox root and filed from there, so a stray
# cover.jpg dropped at the root over SMB was copied into every later album
# with no folder cover - and Navidrome prefers it to the embedded art
# (CODE_REVIEW H7). The same for a loose file at the library root.

def test_a_cover_at_the_inbox_root_is_not_carried(space, tmp_path):
    (space.inbox_dir / "cover.jpg").write_bytes(b"somebody else's")
    arrived = space.inbox_dir / "download.mp3"
    shutil.copy(track(tmp_path, albumartist="Boards", album="Geogaddi",
                      title="Sixtyten"), arrived)

    filed = filer.file_track(space, arrived)

    assert not (filed.path.parent / "cover.jpg").exists()


def test_a_cover_at_the_library_root_is_not_carried(space, tmp_path):
    (space.library_path / "folder.jpg").write_bytes(b"somebody else's")
    loose = space.library_path / "loose.mp3"
    shutil.copy(track(tmp_path, albumartist="Boards", album="Geogaddi",
                      title="Sixtyten"), loose)

    filed = filer.file_track(space, loose)

    assert not (filed.path.parent / "folder.jpg").exists()


def test_a_cover_in_a_folder_dropped_into_the_inbox_is_carried(space, tmp_path):
    dropped = space.inbox_dir / "upload-1" / "Geogaddi"
    dropped.mkdir(parents=True)
    (dropped / "cover.jpg").write_bytes(b"its own")
    arrived = dropped / "01.mp3"
    shutil.copy(track(tmp_path, albumartist="Boards", album="Geogaddi",
                      title="Sixtyten"), arrived)

    filed = filer.file_track(space, arrived)

    assert (filed.path.parent / "cover.jpg").read_bytes() == b"its own"


# A cover is copied along with the tracks, so a folder every track had left
# still held cover.jpg and was never pruned (CODE_REVIEW M11).

def test_a_renamed_albums_old_folder_is_removed_cover_and_all(space):
    filed = album_on_disk(space, "Old Artist", "Old Album", ["One"])
    old = filed[0].path.parent
    (old / "cover.jpg").write_bytes(b"art")

    moved = filer.retag_album(space, old, albumartist="New Artist",
                              album="New Album")

    assert not old.exists()
    assert not old.parent.exists()
    assert (moved[0].path.parent / "cover.jpg").read_bytes() == b"art"


def test_a_cover_that_did_not_travel_keeps_the_folder(space):
    filed = album_on_disk(space, "Old Artist", "Old Album", ["One"])
    old = filed[0].path.parent
    (old / "cover.jpg").write_bytes(b"old art")
    target = space.library_path / "New Artist" / "New Album"
    target.mkdir(parents=True)
    (target / "cover.jpg").write_bytes(b"different art")

    filer.retag_album(space, old, albumartist="New Artist", album="New Album")

    assert (old / "cover.jpg").read_bytes() == b"old art"


def test_a_track_moving_out_of_an_album_leaves_its_cover_for_the_rest(space):
    filed = album_on_disk(space, "Artist", "Album", ["One", "Two"])
    folder = filed[0].path.parent
    (folder / "cover.jpg").write_bytes(b"art")

    filer.retag_track(space, filed[0].path, album="Elsewhere")

    assert (folder / "cover.jpg").exists()


def test_an_album_with_an_untaggable_file_is_refused_before_any_write(space):
    """A failure on file k left 0..k-1 retagged, unregistered and unmoved -
    an album half renamed (CODE_REVIEW M13). Every file is opened first."""
    filed = album_on_disk(space, "Artist", "Album", ["One", "Two"])
    folder = filed[0].path.parent
    (folder / "03 - Broken.mp3").write_bytes(b"not audio at all")

    with pytest.raises(filer.NotEditable):
        filer.retag_album(space, folder, albumartist="Artist", album="Renamed")

    assert {EasyID3(one.path)["album"][0] for one in filed} == {"Album"}


# --- two jobs filing the same track at once (L14) ------------------------------

def test_two_moves_onto_one_name_at_once_keep_both_files(tmp_path, monkeypatch):
    """Both used to choose the same free name and the second move replaced
    the first file. The move is slowed so both have chosen before either
    lands."""
    import os
    import threading
    import time

    real = os.replace

    def slow(src, dst):
        time.sleep(0.1)
        real(src, dst)

    monkeypatch.setattr(os, "replace", slow)
    target = tmp_path / "lib" / "01 - Song.mp3"
    sources = []
    for n in (1, 2):
        source = tmp_path / f"job{n}.mp3"
        source.write_bytes(f"copy {n}".encode())
        sources.append(source)

    threads = [threading.Thread(target=filer._move_into_place, args=(s, target))
               for s in sources]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    landed = sorted(p.read_bytes() for p in target.parent.iterdir())
    assert landed == [b"copy 1", b"copy 2"]


def test_a_failed_move_leaves_no_placeholder(tmp_path, monkeypatch):
    import os

    def broken(src, dst):
        raise PermissionError("read-only")

    monkeypatch.setattr(os, "replace", broken)
    source = tmp_path / "job.mp3"
    source.write_bytes(b"audio")
    target = tmp_path / "lib" / "01 - Song.mp3"

    with pytest.raises(PermissionError):
        filer._move_into_place(source, target)

    assert source.exists()
    assert list(target.parent.iterdir()) == []


# --- names Navidrome would hide (2H4) ----------------------------------------

@pytest.mark.parametrize("name, filed_as", [
    (".38 Special", "_38 Special"),
    (".5: The Gray Chapter", "_5_ The Gray Chapter"),
    (".hack//SIGN", "_hack__SIGN"),
])
def test_a_single_leading_dot_is_not_left_to_hide_the_folder(name, filed_as):
    """Navidrome skips a folder or file whose name starts with exactly one
    dot, so the album was filed, reported Done and never appeared."""
    assert filer.sanitize(name) == filed_as


@pytest.mark.parametrize("name", ["...And Justice for All", "..Baby One More Time"])
def test_two_or_more_leading_dots_are_kept(name):
    """Navidrome scans these: an ellipsis is a title, not a hidden file."""
    assert filer.sanitize(name) == name


def test_a_copy_that_dies_part_way_leaves_nothing_under_the_real_name(
        tmp_path, monkeypatch):
    """A full disk or a restart mid-copy left a truncated track under its
    real name, and the next attempt filed "01 - Song (2).mp3" beside it,
    both carrying one track UUID (2M4)."""
    import errno
    import os
    import shutil

    real_replace = os.replace

    def cross_device(source, target):
        if Path(source).parent != Path(target).parent:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_replace(source, target)

    def half_copy(source, target, **kwargs):
        Path(target).write_bytes(Path(source).read_bytes()[:3])
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "replace", cross_device)
    monkeypatch.setattr(shutil, "copy2", half_copy)
    source = tmp_path / "scratch" / "job.mp3"
    source.parent.mkdir()
    source.write_bytes(b"the whole track")
    target = tmp_path / "lib" / "01 - Song.mp3"

    with pytest.raises(OSError):
        filer._move_into_place(source, target)

    assert source.read_bytes() == b"the whole track"
    assert list(target.parent.iterdir()) == []

    monkeypatch.undo()
    monkeypatch.setattr(os, "replace", cross_device)
    assert filer._move_into_place(source, target) == target
    assert target.read_bytes() == b"the whole track"


# --- a cover only goes where it belongs (2M7) --------------------------------

def test_moving_one_track_does_not_take_its_old_albums_cover(space):
    """Navidrome prefers a folder cover to embedded art, so every track of
    the album the stray joined showed the other album's picture."""
    abbey = album_on_disk(space, "The Beatles", "Abbey Road", ["Come Together"])
    (abbey[0].path.parent / "cover.jpg").write_bytes(b"abbey road")
    stray = album_on_disk(space, "The Beatles", "Abbey Road", ["Jet"])[0]
    album_on_disk(space, "Wings", "Band on the Run", ["Band on the Run"])

    moved = filer.retag_track(space, stray.path, albumartist="Wings",
                              album="Band on the Run")

    assert not (moved.path.parent / "cover.jpg").exists()


def test_a_folder_of_several_albums_gives_its_cover_to_none_of_them(space, tmp_path):
    dump = space.inbox_dir / "upload-1" / "everything"
    dump.mkdir(parents=True)
    (dump / "cover.jpg").write_bytes(b"one picture")
    for n, album in enumerate(["Geogaddi", "Campfire Headphase"], start=1):
        shutil.copy(track(tmp_path, name=f"{n}.mp3", albumartist="Boards",
                          album=album, title=f"Song {n}"), dump / f"{n}.mp3")

    filed = filer.file_track(space, dump / "1.mp3")

    assert not (filed.path.parent / "cover.jpg").exists()


def test_a_cover_is_not_carried_into_an_album_already_on_disk(space, tmp_path):
    existing = album_on_disk(space, "Boards", "Geogaddi", ["Music Is Math"])[0]
    dropped = space.inbox_dir / "upload-1" / "Geogaddi"
    dropped.mkdir(parents=True)
    (dropped / "cover.jpg").write_bytes(b"the drop's own")
    shutil.copy(track(tmp_path, albumartist="Boards", album="Geogaddi",
                      title="Sixtyten", tracknumber="2"), dropped / "02.mp3")

    filed = filer.file_track(space, dropped / "02.mp3")

    assert filed.path.parent == existing.path.parent
    assert not (filed.path.parent / "cover.jpg").exists()


def test_a_folder_cover_is_found_whatever_its_case(space):
    folder = tmp_of(space) / "rip"
    folder.mkdir()
    (folder / "Folder.JPG").write_bytes(b"art")
    assert filer.folder_cover(folder).name == "Folder.JPG"


# --- an album on disk the registry has not heard of (2M8) ---------------------

def test_a_track_moved_into_an_unregistered_album_joins_its_uuid(space):
    """Files agreeing on an album UUID under a key the registry has never
    seen (retagged by another tool, a backfill conflict). Minting split the
    album, and the next Save album moved every file to the newcomer."""
    album = album_on_disk(space, "Queen", "Greatest Hits", ["Bohemian Rhapsody",
                                                            "Don't Stop Me Now"])
    on_disk = album[0].album_uuid
    registry.forget(space.library_id, album[0].album_key)
    stray = album_on_disk(space, "Queen", "Innuendo", ["Innuendo"])[0]

    moved = filer.retag_track(space, stray.path, album="Greatest Hits")

    assert moved.path.parent == album[0].path.parent
    assert moved.album_uuid == on_disk
    assert uuidtags.read(moved.path)[1] == on_disk


def test_files_that_disagree_are_not_guessed_between(space):
    album = album_on_disk(space, "Queen", "Greatest Hits", ["One", "Two"])
    uuidtags.write(album[1].path, None, "22222222-2222-4222-8222-222222222222")
    registry.forget(space.library_id, album[0].album_key)
    stray = album_on_disk(space, "Queen", "Innuendo", ["Innuendo"])[0]

    moved = filer.retag_track(space, stray.path, album="Greatest Hits")

    assert moved.album_uuid not in {album[0].album_uuid,
                                    "22222222-2222-4222-8222-222222222222"}


# --- an untagged stray does not decide the album's key (2M9) -------------------

def test_renaming_an_album_with_an_untagged_stray_first_keeps_its_uuid(space):
    """require_one_album lets a file with no album tag through, but the key
    was read from the first file alone: when the stray sorted first there
    was nothing to follow, and the whole album got a new UUID."""
    album = album_on_disk(space, "Boards", "Geogaddi", ["Music Is Math",
                                                        "Sixtyten"])
    folder = album[0].path.parent
    stray = folder / "00 - stray.mp3"      # sorts first, names no album
    shutil.copy(track(tmp_of(space), name="stray.mp3", albumartist="Boards",
                      title="Stray"), stray)
    before = album[0].album_uuid

    moved = filer.retag_album(space, folder, album="Geogaddi (Remaster)")

    tagged = [m for m in moved if m.album_key != registry.loose_key(m.track_uuid)]
    assert {m.album_uuid for m in tagged} == {before}


# --- a rename that fails part way (2M10) ----------------------------------------

def test_a_rename_that_fails_part_way_puts_back_what_it_wrote(space, monkeypatch):
    """A failure on file k used to leave 0..k-1 renamed: a folder with two
    names, which every album action then refused."""
    album = album_on_disk(space, "Boards", "Geogaddi", ["One", "Two", "Three"])
    folder = album[0].path.parent
    real = filer.write_tags
    calls = {"n": 0}

    def fails_second(path, **fields):
        calls["n"] += 1
        if calls["n"] == 2:
            raise filer.NotEditable("No space left on device")
        real(path, **fields)

    monkeypatch.setattr(filer, "write_tags", fails_second)
    with pytest.raises(filer.NotEditable):
        filer.retag_album(space, folder, album="Geogaddi (Remaster)")

    assert {filer.read_meta(p).album for p in filer.audio_in(folder)} == {"Geogaddi"}
    assert not (folder / filer.RENAMING).exists()

    monkeypatch.setattr(filer, "write_tags", real)
    moved = filer.retag_album(space, folder, album="Geogaddi (Remaster)")
    assert {m.album_uuid for m in moved} == {album[0].album_uuid}


def test_a_rename_cut_short_by_a_restart_can_be_finished(space):
    """Nothing can roll back a container that stopped mid-loop. The marker
    left in the folder says the second name is the first one's rename."""
    import json

    album = album_on_disk(space, "Boards", "Geogaddi", ["One", "Two", "Three"])
    folder = album[0].path.parent
    (folder / filer.RENAMING).write_text(
        json.dumps({"was": album[0].album_key}), encoding="utf-8")
    filer.write_tags(album[0].path, album="Geogaddi (Remaster)")   # then it stopped

    moved = filer.retag_album(space, folder, album="Geogaddi (Remaster)")

    assert {m.album_uuid for m in moved} == {album[0].album_uuid}
    assert len({m.path.parent for m in moved}) == 1
    assert not folder.exists()


def test_without_the_marker_two_names_are_still_refused(space):
    album = album_on_disk(space, "Boards", "Geogaddi", ["One", "Two"])
    folder = album[0].path.parent
    filer.write_tags(album[0].path, album="Something Else")

    with pytest.raises(filer.NotEditable):
        filer.retag_album(space, folder, album="Geogaddi")


# --- a numbered copy stays put (2L9) ----------------------------------------------

def test_a_numbered_copy_keeps_its_name_across_saves(space):
    """It was never "already in place", so each save moved it to the next
    free number and the next one moved it back."""
    first, second = [filer.file_track(space, track(
        tmp_of(space), name=f"same-{n}.mp3", albumartist="Boards",
        album="Geogaddi", title="Same", tracknumber="1")) for n in (1, 2)]
    assert second.path.name == "01 - Same (2).mp3"

    for _ in range(2):
        moved = filer.retag_album(space, first.path.parent, album="Geogaddi")

    assert sorted(m.path.name for m in moved) == sorted([first.path.name,
                                                        second.path.name])
