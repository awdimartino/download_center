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
    monkeypatch.setattr(settings, "staging_quiet_seconds", 0)
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
    drop(space, into="an album", albumartist="Artist", album="Album",
         title="Song", tracknumber="1")

    inbox.drain(space)

    assert list(space.inbox_dir.rglob("*")) == []


def test_a_file_still_being_written_is_left_alone(space, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "staging_quiet_seconds", 120)
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
