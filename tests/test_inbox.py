"""The drop point that empties itself.

Staging held 877 of Alex's files and 327 of Kelly's, waiting for a match that
never came. The inbox exists to have the opposite property: at rest it is
empty, so "is anything waiting" has an obvious answer rather than a backlog.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3

from app import inbox, uuidtags

SILENCE = Path(__file__).parent / "fixtures" / "silence.mp3"


@pytest.fixture
def space(tmp_path, monkeypatch, state_db):
    from app import workspace
    from app.config import settings

    monkeypatch.setattr(settings, "output_dir", tmp_path / "untagged")
    monkeypatch.setattr(settings, "inbox_quiet_seconds", 0)
    monkeypatch.setattr(workspace, "CONFIG_DIR", tmp_path / "config")
    (tmp_path / "music").mkdir()
    made = workspace.Workspace(username="alex", library_id=1,
                               library_name="Music",
                               library_path=tmp_path / "music")
    made.prepare()
    return made


def drop(space, name="dropped.mp3", into=None, **tags) -> Path:
    """A real MP3 in the inbox, tagged as asked and old enough to be settled."""
    parent = space.inbox_dir / into if into else space.inbox_dir
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / name
    shutil.copy(SILENCE, path)
    if tags:
        audio = EasyID3(path)
        for key, value in tags.items():
            audio[key] = str(value)
        audio.save()
    old = time.time() - 3600
    os.utime(path, (old, old))
    return path


# --- what gets filed --------------------------------------------------------

def test_a_dropped_file_is_filed_by_its_own_tags(space):
    drop(space, albumartist="The Beatles", album="Abbey Road",
         title="Come Together", tracknumber="1")

    result = inbox.drain(space)

    assert result.filed == [space.library_path / "The Beatles" / "Abbey Road"
                            / "01 - Come Together.mp3"]
    assert result.filed[0].is_file()


def test_the_folder_a_file_was_dropped_in_does_not_decide_anything(space):
    """A dragged-in directory used to be handed to beets as one release. A
    folder of 746 loose tracks spanning a hundred albums is not an album."""
    drop(space, name="a.mp3", into="misc dump", albumartist="The Beatles",
         album="Abbey Road", title="Come Together", tracknumber="1")
    drop(space, name="b.mp3", into="misc dump", albumartist="Aphex Twin",
         album="Drukqs", title="Avril 14th", tracknumber="10")

    inbox.drain(space)

    assert (space.library_path / "The Beatles" / "Abbey Road"
            / "01 - Come Together.mp3").is_file()
    assert (space.library_path / "Aphex Twin" / "Drukqs"
            / "10 - Avril 14th.mp3").is_file()


def test_a_dropped_track_joins_an_album_already_in_the_library(space):
    """The hand-dropped copy and the downloaded one are the same record."""
    from app import filer

    first = drop(space, name="a.mp3", albumartist="The Beatles",
                 album="Abbey Road", title="Come Together", tracknumber="1")
    filed = filer.file_track(space, first)

    drop(space, name="b.mp3", albumartist="The Beatles", album="Abbey Road",
         title="Something", tracknumber="2")
    result = inbox.drain(space)

    assert uuidtags.read(result.filed[0])[1] == filed.album_uuid


def test_a_one_track_fragment_is_filed_like_anything_else(space):
    """89 folders held a single track each and none of them could ever
    import, because a fragment cannot match a full release."""
    drop(space, albumartist="The Beatles", album="Abbey Road",
         title="Something", tracknumber="2")

    result = inbox.drain(space)

    assert result.filed[0].parent.name == "Abbey Road"


# --- what is left behind ----------------------------------------------------

def test_the_inbox_is_empty_afterwards(space):
    """No music left, and no tree of emptied folders either - a dragged-in
    album leaves its own directory behind, and that reads as "something is
    still in there". The hidden scratch directory stays: it is where the
    next download will be built."""
    drop(space, into="an album", albumartist="Artist", album="Album",
         title="Song", tracknumber="1")

    inbox.drain(space)

    assert inbox.waiting(space) == []
    assert [p.name for p in space.inbox_dir.iterdir()] == [".incomplete"]


def test_a_file_still_being_written_is_left_alone(space, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "inbox_quiet_seconds", 120)
    path = space.inbox_dir / "arriving.mp3"
    shutil.copy(SILENCE, path)

    result = inbox.drain(space)

    assert result.filed == []
    assert result.waiting == 1
    assert path.exists()


def test_something_that_is_not_audio_is_not_touched(space):
    """Cover art and sleeve notes come with a dragged-in album. Deleting
    somebody's files is not this application's business."""
    (space.inbox_dir).mkdir(parents=True, exist_ok=True)
    art = space.inbox_dir / "cover.jpg"
    art.write_bytes(b"not audio")

    inbox.drain(space)

    assert art.exists()


def test_a_file_that_cannot_be_filed_is_reported_not_swallowed(space,
                                                               monkeypatch):
    drop(space, albumartist="Artist", album="Album", title="Song")

    def boom(space_, path):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(inbox.filer, "file_track", boom)
    result = inbox.drain(space)

    assert result.filed == []
    assert len(result.failures) == 1
    assert "read-only filesystem" in result.failures[0]


# --- draining with nobody signed in -----------------------------------------

def test_every_workspace_on_disk_is_drained(space):
    drop(space, albumartist="Artist", album="Album", title="Song",
         tracknumber="1")

    results = inbox.drain_all()

    assert set(results) == {"alex"}
    assert results["alex"].changed


def test_an_unmounted_library_is_left_alone(space, monkeypatch):
    """Navidrome reports where a library lives; this is a different container
    with its own mounts. Filing into a path that is not mounted writes into
    the container and loses the music on the next restart."""
    drop(space, albumartist="Artist", album="Album", title="Song",
         tracknumber="1")
    shutil.rmtree(space.library_path)

    assert inbox.drain_all() == {}
    assert len(inbox.waiting(space)) == 1


def test_nothing_in_the_inbox_is_not_an_error(space):
    assert inbox.drain(space).filed == []
    assert inbox.drain_all() == {}


# --- a download coming in the same way --------------------------------------
#
# There is one road into the library. A download is built in the hidden
# scratch directory, tagged, then delivered into the inbox like anything
# else - the worker just files it on the spot rather than waiting for the
# poller to work out what it already knows.

def built(space, job="job1", item="i1", **tags) -> Path:
    """A finished, tagged download sitting in scratch space."""
    path = inbox.scratch_path(space, job, item)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SILENCE, path)
    if tags:
        audio = EasyID3(path)
        for key, value in tags.items():
            audio[key] = str(value)
        audio.save()
    return path


def test_a_download_is_filed_the_moment_it_is_delivered(space):
    source = built(space, albumartist="The Beatles", album="Abbey Road",
                   title="Come Together", tracknumber="1")

    filed = inbox.deliver(space, source)

    assert filed.path == (space.library_path / "The Beatles" / "Abbey Road"
                          / "01 - Come Together.mp3")
    assert filed.path.is_file()
    assert not source.exists()


def test_a_download_and_a_hand_drop_land_in_the_same_album(space):
    """The whole reason there is one road. These used to be two, and the
    difference between them is where `Non-Album/` came from."""
    downloaded = inbox.deliver(space, built(
        space, albumartist="The Beatles", album="Abbey Road",
        title="Come Together", tracknumber="1"))

    drop(space, name="b.mp3", albumartist="The Beatles", album="Abbey Road",
         title="Something", tracknumber="2")
    dropped = inbox.drain(space)

    assert dropped.filed[0].parent == downloaded.path.parent
    assert uuidtags.read(dropped.filed[0])[1] == downloaded.album_uuid


def test_the_poller_cannot_grab_a_download_being_built(space, monkeypatch):
    """Scratch space is hidden, so a file yt-dlp is still writing is not a
    file to file. Without this the poller would file a partial MP3 under a
    real name the moment it stopped changing."""
    from app.config import settings

    monkeypatch.setattr(settings, "inbox_quiet_seconds", 0)
    half = built(space, albumartist="The Beatles", album="Abbey Road")

    assert inbox.waiting(space) == []
    assert inbox.drain(space).filed == []
    assert half.exists()


def test_the_poller_does_not_race_the_worker_for_a_delivered_file(space,
                                                                  monkeypatch):
    """A rename keeps the file's mtime, so what lands in the inbox is seconds
    old and stays unsettled for the whole quiet period - long after the
    worker has filed it."""
    from app.config import settings

    monkeypatch.setattr(settings, "inbox_quiet_seconds", 120)
    source = built(space, albumartist="The Beatles", album="Abbey Road",
                   title="Come Together", tracknumber="1")
    arrived = inbox._move_in(source, space.inbox_dir / source.name)

    assert inbox.settled(arrived) is False
    assert inbox.drain(space).filed == []


def test_a_download_orphaned_by_a_crash_is_picked_up_later(space, monkeypatch):
    """The application died between delivering and filing. The file is in the
    inbox, so a restart files it - where before it sat in scratch space and
    was thrown away with the job."""
    from app.config import settings

    source = built(space, albumartist="The Beatles", album="Abbey Road",
                   title="Come Together", tracknumber="1")
    inbox._move_in(source, space.inbox_dir / source.name)

    monkeypatch.setattr(settings, "inbox_quiet_seconds", 0)
    result = inbox.drain(space)

    assert result.filed == [space.library_path / "The Beatles" / "Abbey Road"
                            / "01 - Come Together.mp3"]


def test_discarding_a_job_leaves_what_it_already_filed(space):
    """Cancelling a job throws away its unfinished downloads. It does not
    un-download the tracks that finished."""
    filed = inbox.deliver(space, built(
        space, item="i1", albumartist="Artist", album="Album", title="Done",
        tracknumber="1"))
    unfinished = built(space, item="i2", albumartist="Artist", album="Album")

    inbox.discard(space, "job1")

    assert not unfinished.exists()
    assert filed.path.is_file()


def test_delivering_two_files_of_the_same_name_keeps_both(space):
    """Item ids make this impossible within a job, but two jobs can finish
    into the inbox at once, and overwriting is how a download disappears."""
    one = built(space, job="a", item="same", albumartist="Artist",
                album="Album", title="One", tracknumber="1")
    two = built(space, job="b", item="same", albumartist="Artist",
                album="Album", title="Two", tracknumber="2")

    first = inbox.deliver(space, one)
    second = inbox.deliver(space, two)

    assert first.path != second.path
    assert first.path.is_file() and second.path.is_file()


# --- files loose at the top of the library ----------------------------------

def loose(space, name="stray.mp3", **tags) -> Path:
    """An audio file sitting at the library root, in no album folder."""
    path = space.library_path / name
    shutil.copy(SILENCE, path)
    if tags:
        audio = EasyID3(path)
        for key, value in tags.items():
            audio[key] = str(value)
        audio.save()
    old = time.time() - 3600
    os.utime(path, (old, old))
    return path


def test_a_file_loose_in_the_library_is_filed(space):
    """The filer never produces one, so it was put there by hand or by
    something that ran before any of this existed. With no folder there is
    no album for the review page to offer."""
    stray = loose(space, albumartist="The Beatles", album="Abbey Road",
                  title="Come Together", tracknumber="1")

    result = inbox.drain(space)

    assert not stray.exists()
    assert result.filed == [space.library_path / "The Beatles" / "Abbey Road"
                            / "01 - Come Together.mp3"]


def test_an_untagged_loose_file_gets_a_folder_too(space):
    loose(space, name="mystery.mp3")

    inbox.drain(space)

    assert (space.library_path / "Unknown Artist" / "Unknown Album"
            / "mystery.mp3").is_file()


def test_music_already_in_an_album_folder_is_not_touched(space):
    """Only the top level. Everything below it has been filed already, and
    re-walking the whole library every 15 seconds is not this loop's job."""
    folder = space.library_path / "The Beatles" / "Abbey Road"
    folder.mkdir(parents=True)
    settled_file = folder / "01 - Come Together.mp3"
    shutil.copy(SILENCE, settled_file)

    assert inbox.loose_in_library(space) == []
    assert inbox.drain(space).filed == []
    assert settled_file.exists()


def test_a_directory_at_the_library_root_is_not_a_loose_file(space):
    """`duplicates-removed/` lives there, and it is not music to file."""
    (space.library_path / "duplicates-removed").mkdir()
    assert inbox.loose_in_library(space) == []


def test_a_loose_file_still_being_copied_is_left_alone(space, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "inbox_quiet_seconds", 120)
    path = space.library_path / "arriving.mp3"
    shutil.copy(SILENCE, path)

    result = inbox.drain(space)

    assert result.filed == []
    assert result.waiting == 1
    assert path.exists()
