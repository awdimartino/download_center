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
from app import workspace
from app.api import inbox as inbox_routes

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


def test_something_that_is_not_audio_is_not_deleted(space):
    """Cover art and sleeve notes come with a dragged-in album. Deleting
    somebody's files is not this application's business, so leftovers are
    moved beside the inbox rather than removed (M17)."""
    (space.inbox_dir).mkdir(parents=True, exist_ok=True)
    art = space.inbox_dir / "cover.jpg"
    art.write_bytes(b"not audio")

    inbox.drain(space)

    assert (space.leftovers_dir / "cover.jpg").read_bytes() == b"not audio"


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


# --- failures that used to repeat for ever ----------------------------------

def test_a_file_that_cannot_be_filed_is_not_retried_every_poll(space,
                                                               monkeypatch):
    """One unfilable file used to be retried every 15 seconds for ever -
    5,760 tracebacks a day, none of it visible to anybody."""
    drop(space, albumartist="Artist", album="Album", title="Song")
    attempts = []

    def boom(space_, path):
        attempts.append(path)
        raise OSError("read-only filesystem")

    monkeypatch.setattr(inbox.filer, "file_track", boom)
    first = inbox.drain(space)
    second = inbox.drain(space)

    assert len(attempts) == 1
    assert len(first.failures) == 1
    # Not retried, but still a failure with its reason - not "waiting"
    # for ever (CODE_REVIEW M18).
    assert second.failures == first.failures
    assert second.waiting == 0


def test_changing_the_file_asks_again(space, monkeypatch):
    """Alter the thing and it is a different question - the reasoning the
    deleted refusal table used."""
    path = drop(space, albumartist="Artist", album="Album", title="Song")
    attempts = []

    def boom(space_, path_):
        attempts.append(path_)
        raise OSError("read-only filesystem")

    monkeypatch.setattr(inbox.filer, "file_track", boom)
    inbox.drain(space)
    inbox.drain(space)
    assert len(attempts) == 1

    path.write_bytes(path.read_bytes() + b"\x00")
    os.utime(path, (time.time() - 3600, time.time() - 3600))
    inbox.drain(space)

    assert len(attempts) == 2


def test_a_file_filed_without_its_identity_is_reported(space, monkeypatch):
    """It is in the library and playable, which beats being lost - but no
    play count can follow it, and this is the last thing able to say so."""
    drop(space, albumartist="Artist", album="Album", title="Song",
         tracknumber="1")
    monkeypatch.setattr(inbox.filer, "_write_identity",
                        lambda *args, **kwargs: False)

    result = inbox.drain(space)

    assert len(result.filed) == 1
    assert result.filed[0].is_file()
    assert "identity tags could not be written" in result.failures[0]


def test_the_poller_leaves_a_file_the_worker_is_delivering(space):
    """The quiet period already covers this, but it is a setting and it is
    allowed to be zero; the in-flight set is the fact itself."""
    path = space.inbox_dir / "arriving.mp3"
    shutil.copy(SILENCE, path)
    inbox._delivering.add(path)
    try:
        assert path not in inbox.waiting(space)
    finally:
        inbox._delivering.discard(path)
    assert path in inbox.waiting(space)


# --- the inbox has to exist before anything can be dropped in it ------------

def test_draining_makes_the_inbox_if_it_is_missing(space):
    """`prepare()` otherwise runs only when a download is queued or an
    album is matched. A workspace that has done
    neither has nowhere to drop a file, and `waiting()` reports that as an
    empty inbox rather than a missing one."""
    import shutil as _shutil
    _shutil.rmtree(space.inbox_dir)
    assert not space.inbox_dir.exists()

    inbox.drain_all()

    assert space.inbox_dir.is_dir()
    assert space.incomplete_dir.is_dir()


def test_a_workspace_that_cannot_be_prepared_does_not_stop_the_others(
        space, monkeypatch, tmp_path):
    from app import workspace as ws

    other = ws.Workspace("kelly", 2, "Kelly", tmp_path / "kelly")
    (tmp_path / "kelly").mkdir()
    monkeypatch.setattr(ws, "existing", lambda: [other, space])

    real = ws.Workspace.prepare

    def refuse(self):
        if self.username == "kelly":
            raise ValueError("that directory belongs to somebody else")
        real(self)

    monkeypatch.setattr(ws.Workspace, "prepare", refuse)
    drop(space, albumartist="Artist", album="Album", title="Song",
         tracknumber="1")

    results = inbox.drain_all()

    assert set(results) == {"alex"}, "the good workspace still drained"


def test_preparing_does_not_disturb_an_inbox_that_is_already_there(space):
    dropped = drop(space, albumartist="Artist", album="Album", title="Song",
                   tracknumber="1")
    assert dropped.exists()

    inbox.drain_all()

    assert not dropped.exists(), "it was filed, not left alone"
    assert space.inbox_dir.is_dir()


# --- a browser upload, the third way in --------------------------------------
#
# An upload's own request handler is the only thing that knows the transfer
# is actually finished - `settled()` cannot, because it also has to be right
# about an SMB copy still arriving. `backdate()` is how that knowledge gets
# into the quiet-period check; `upload_root()` is what keeps one drop's cover
# art from being offered to a different drop's tracks.

def tagged(path: Path, **tags) -> Path:
    shutil.copy(SILENCE, path)
    audio = EasyID3(path)
    for key, value in tags.items():
        audio[key] = str(value)
    audio.save()
    return path


def test_backdating_makes_a_just_written_file_settle_immediately(space,
                                                                  monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "inbox_quiet_seconds", 120)
    path = tagged(space.inbox_dir / "just-arrived.mp3",
                  albumartist="Artist", album="Album", title="Song")
    assert inbox.settled(path) is False

    inbox.backdate(path)

    assert inbox.settled(path) is True


def test_an_uploaded_track_is_filed_once_backdated(space):
    path = inbox.upload_root(space, "batch1") / "a.mp3"
    path.parent.mkdir(parents=True)
    tagged(path, albumartist="The Beatles", album="Abbey Road",
          title="Come Together", tracknumber="1")
    inbox.backdate(path)

    result = inbox.drain(space)

    assert result.filed == [space.library_path / "The Beatles" / "Abbey Road"
                            / "01 - Come Together.mp3"]


def test_an_uploaded_album_carries_its_own_cover(space):
    """The whole reason an upload gets its own folder instead of landing flat
    in the inbox root, the way a delivered download does."""
    root = inbox.upload_root(space, "batch1")
    root.mkdir(parents=True)
    (root / "cover.jpg").write_bytes(b"art")
    track = tagged(root / "a.mp3", albumartist="The Beatles",
                  album="Abbey Road", title="Come Together", tracknumber="1")
    inbox.backdate(track)

    result = inbox.drain(space)

    assert (result.filed[0].parent / "cover.jpg").is_file()


def test_two_uploaded_albums_do_not_share_a_cover(space):
    """Landed flat, the first track to file from either album would have
    carried whichever cover happened to be sitting in the inbox root."""
    one = inbox.upload_root(space, "batch1")
    one.mkdir(parents=True)
    (one / "cover.jpg").write_bytes(b"beatles art")
    track_one = tagged(one / "a.mp3", albumartist="The Beatles",
                       album="Abbey Road", title="Come Together",
                       tracknumber="1")
    inbox.backdate(track_one)

    two = inbox.upload_root(space, "batch2")
    two.mkdir(parents=True)
    track_two = tagged(two / "b.mp3", albumartist="Aphex Twin",
                       album="Drukqs", title="Avril 14th", tracknumber="10")
    inbox.backdate(track_two)

    inbox.drain(space)

    assert (space.library_path / "The Beatles" / "Abbey Road"
            / "cover.jpg").is_file()
    assert not (space.library_path / "Aphex Twin" / "Drukqs"
               / "cover.jpg").is_file()


# --- what a Library edit waits for (CODE_REVIEW M10) ------------------------
# "Settled" meant nothing in the folder modified within the quiet period, and
# every edit modifies files: fixing a title and then its track number was
# refused as "still arriving" for two minutes. What an edit has to wait for
# is the inbox still filing music into that album.

def test_an_album_the_inbox_just_filed_into_is_receiving(tmp_path, monkeypatch):
    from app import inbox
    from app.config import settings

    monkeypatch.setattr(settings, "inbox_quiet_seconds", 120)
    monkeypatch.setattr(inbox, "_arrivals", {})
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01.mp3").write_bytes(b"x")
    other = tmp_path / "Artist" / "Other"
    other.mkdir()

    assert not inbox.receiving(album), "a file merely modified is not arriving"
    inbox._arrived(album)
    assert inbox.receiving(album)
    assert inbox.receiving(album / "01.mp3")
    assert not inbox.receiving(other)


def test_an_arrival_stops_counting_after_the_quiet_period(tmp_path, monkeypatch):
    from app import inbox
    from app.config import settings

    monkeypatch.setattr(inbox, "_arrivals", {})
    inbox._arrived(tmp_path)
    monkeypatch.setattr(settings, "inbox_quiet_seconds", -1)

    assert not inbox.receiving(tmp_path)


def test_a_delivery_marks_its_album_as_receiving(space, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(inbox, "_arrivals", {})
    monkeypatch.setattr(settings, "inbox_quiet_seconds", 120)
    source = built(space, albumartist="The Beatles", album="Abbey Road",
                   title="Come Together", tracknumber="1")

    filed = inbox.deliver(space, source)

    assert inbox.receiving(filed.path.parent)


# --- residue (CODE_REVIEW M17) -----------------------------------------------
# Covers, cue sheets and logs stayed for ever and kept their folders, so the
# inbox was never empty at rest - and a cover left at the top was carried
# into every later download (H7).

def _residue(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    old = time.time() - 3600
    os.utime(path, (old, old))
    os.utime(path.parent, (old, old))


def test_an_album_drop_leaves_nothing_behind(space):
    drop(space, into="an album", albumartist="Artist", album="Album",
         title="Song", tracknumber="1")
    for name in ("cover.jpg", "album.cue", "rip.log"):
        _residue(space.inbox_dir / "an album" / name)

    inbox.drain(space)

    assert [p.name for p in space.inbox_dir.iterdir()] == [".incomplete"]


def test_a_cover_at_the_top_is_set_aside(space):
    _residue(space.inbox_dir / "cover.jpg")

    inbox.drain(space)

    assert not (space.inbox_dir / "cover.jpg").exists()
    assert (space.leftovers_dir / "cover.jpg").exists()


def test_a_file_the_inbox_does_not_recognise_is_kept(space):
    _residue(space.inbox_dir / "letters" / "cover.jpg")
    _residue(space.inbox_dir / "letters" / "thesis.docx")

    inbox.drain(space)

    assert (space.inbox_dir / "letters" / "thesis.docx").exists()
    assert (space.inbox_dir / "letters" / "cover.jpg").exists()


def test_residue_beside_audio_still_to_file_is_kept(space, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "inbox_quiet_seconds", 120)
    folder = space.inbox_dir / "arriving"
    _residue(folder / "cover.jpg")
    (folder / "01.mp3").write_bytes(b"still copying")

    inbox.drain(space)

    assert (folder / "cover.jpg").exists()


def test_two_drains_at_once_file_a_file_once(space, monkeypatch):
    """Upload-finish and the poller both drained, and nothing serialised
    them: two track UUIDs minted, and the loser reported a spurious failure
    (CODE_REVIEW M19)."""
    import threading

    drop(space, albumartist="Artist", album="Album", title="Song",
         tracknumber="1")
    real = inbox.filer.file_track
    calls = []

    def slow(space_, path, **kw):
        calls.append(path)
        time.sleep(0.3)
        return real(space_, path, **kw)

    monkeypatch.setattr(inbox.filer, "file_track", slow)
    results = []
    threads = [threading.Thread(target=lambda: results.append(inbox.drain(space)))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(calls) == 1
    assert [r.failures for r in results] == [[], []]
    assert sum(len(r.filed) for r in results) == 1


# --- uploads are streamed (CODE_REVIEW M20) ----------------------------------
# The whole upload was read into memory before its size was checked.

def _upload(data: bytes, name="song.mp3", size=None):
    import io

    from starlette.datastructures import UploadFile

    upload = UploadFile(io.BytesIO(data), filename=name, size=size)

    async def never(*args, **kwargs):
        raise AssertionError("the upload was read whole")

    upload.read = never
    return upload


def _session():
    from types import SimpleNamespace
    return SimpleNamespace(identity=SimpleNamespace(username="alex"))


@pytest.mark.asyncio
async def test_an_upload_is_copied_in_chunks(space, monkeypatch):

    monkeypatch.setattr(workspace, "for_session", lambda identity, lid: space)
    monkeypatch.setattr(inbox_routes, "UPLOAD_CHUNK", 4)
    data = SILENCE.read_bytes()

    answer = await inbox_routes.upload_to_inbox(_upload(data), "", None, None, _session())

    landed = inbox.upload_root(space, answer["batch"]) / "song.mp3"
    assert landed.read_bytes() == data
    assert not list(landed.parent.glob(".*.part"))


@pytest.mark.asyncio
async def test_a_declared_oversize_upload_is_refused_before_reading(space,
                                                                    monkeypatch):
    from fastapi import HTTPException


    monkeypatch.setattr(workspace, "for_session", lambda identity, lid: space)
    monkeypatch.setattr(inbox_routes, "MAX_UPLOAD_BYTES", 10)

    with pytest.raises(HTTPException) as refused:
        await inbox_routes.upload_to_inbox(_upload(b"x" * 50, size=50), "", None, None,
                                   _session())
    assert refused.value.status_code == 413


@pytest.mark.asyncio
async def test_an_undeclared_oversize_upload_stops_at_the_cap(space, monkeypatch):
    from fastapi import HTTPException


    monkeypatch.setattr(workspace, "for_session", lambda identity, lid: space)
    monkeypatch.setattr(inbox_routes, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(inbox_routes, "UPLOAD_CHUNK", 4)

    with pytest.raises(HTTPException) as refused:
        await inbox_routes.upload_to_inbox(_upload(b"x" * 50), "", None, None, _session())
    assert refused.value.status_code == 413
    assert not [p for p in space.inbox_dir.rglob("*") if p.is_file()]


# --- a drop is filed when it is finished (CODE_REVIEW M21) -------------------
# Every file was backdated as it landed, so the poller filed an album's
# tracks mid-upload, before its cover (often last) had arrived.

@pytest.mark.asyncio
async def test_an_album_drop_is_filed_with_its_cover_that_came_last(space,
                                                                    monkeypatch):
    from mutagen.easyid3 import EasyID3 as Tags

    from app.config import settings

    monkeypatch.setattr(settings, "inbox_quiet_seconds", 120)
    monkeypatch.setattr(workspace, "for_session", lambda identity, lid: space)
    tracks = []
    for n in (1, 2):
        path = space.staging / f"src{n}.mp3"
        shutil.copy(SILENCE, path)
        tags = Tags(path)
        tags.update({"albumartist": "Artist", "album": "Album",
                     "title": f"Song {n}", "tracknumber": str(n)})
        tags.save()
        tracks.append(path.read_bytes())

    batch = None
    for n, data in enumerate(tracks, start=1):
        answer = await inbox_routes.upload_to_inbox(
            _upload(data, name=f"{n:02d}.mp3"), f"Album/{n:02d}.mp3", batch,
            None, _session())
        batch = answer["batch"]
    mid_upload = inbox.drain(space)          # the poller, mid-drop
    await inbox_routes.upload_to_inbox(_upload(b"\xff\xd8 art", name="cover.jpg"),
                               "Album/cover.jpg", batch, None, _session())

    finished = await inbox_routes.finish_upload(None, batch, _session())

    assert mid_upload.filed == []
    assert len(finished["filed"]) == 2
    album = space.library_path / "Artist" / "Album"
    assert (album / "cover.jpg").read_bytes() == b"\xff\xd8 art"


# --- what a crash leaves in scratch space (L3) --------------------------------

def test_unfinished_downloads_are_cleared_at_start_up(space):
    """Jobs live in memory, so nothing ever finished or discarded a job's
    scratch folder once the process that owned it had died."""
    built(space, job="crashed", item="i1", title="Half")
    (space.incomplete_dir / "stray.part").write_bytes(b"x")

    assert inbox.clear_scratch() == 2

    assert list(space.incomplete_dir.iterdir()) == []
    assert space.incomplete_dir.is_dir()


def test_clearing_scratch_leaves_the_inbox_alone(space):
    dropped = drop(space, albumartist="Artist", album="Album", title="Song",
                   tracknumber="1")

    assert inbox.clear_scratch() == 0
    assert dropped.exists()


def test_a_download_that_cannot_be_filed_is_not_left_for_the_poller(space,
                                                                     monkeypatch):
    """The worker marks it failed and Retry fetches it again. Left in the
    inbox, the poller filed it as well - two copies (L4)."""
    from app import filer

    source = built(space, albumartist="Artist", album="Album", title="Song",
                   tracknumber="1")
    real = filer.file_track
    monkeypatch.setattr(filer, "file_track",
                        lambda space, path: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError):
        inbox.deliver(space, source)

    assert source.exists()
    assert inbox.waiting(space) == []
    monkeypatch.setattr(filer, "file_track", real)
    assert inbox.drain(space).filed == []


# --- a hidden folder in an upload's path (L17) ---------------------------------

def test_an_upload_under_a_hidden_folder_is_filed(space):
    """The poller walks past hidden folders, so a drop of `.music/Album/`
    sat in the inbox for ever."""

    segments = inbox_routes._relpath_segments(".music/Abbey Road/01.mp3", None)
    assert segments == ["music", "Abbey Road", "01.mp3"]

    path = inbox.upload_root(space, "batch1").joinpath(*segments)
    path.parent.mkdir(parents=True)
    tagged(path, albumartist="The Beatles", album="Abbey Road",
           title="Come Together", tracknumber="1")
    inbox.backdate(path)

    assert len(inbox.drain(space).filed) == 1


def test_a_dot_dot_segment_still_cannot_climb_out():

    assert inbox_routes._relpath_segments("../../etc/song.mp3", None) == ["etc", "song.mp3"]


@pytest.mark.asyncio
async def test_a_hidden_file_is_refused_rather_than_left_unfiled(space, monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app import workspace

    monkeypatch.setattr(workspace, "for_session", lambda identity, lid: space)

    async def close():
        return None

    upload = SimpleNamespace(filename="._01.mp3", size=10, file=None, close=close)
    with pytest.raises(HTTPException) as refused:
        await inbox_routes.upload_to_inbox(file=upload, relpath="Album/._01.mp3",
                                   batch=None, library_id=None,
                                   session=SimpleNamespace(identity=None))
    assert refused.value.status_code == 400
    assert "hidden" in refused.value.detail


def test_the_poller_cannot_see_a_delivery_while_it_is_being_moved(space,
                                                                  monkeypatch):
    """At a quiet period of 0 nothing else keeps them apart, and the file
    was only marked as the worker's once the move had finished (L18)."""
    seen_mid_move = []
    real = shutil.move

    def move(src, dst):
        result = real(src, dst)
        seen_mid_move.extend(inbox.waiting(space))
        return result

    monkeypatch.setattr(inbox.shutil, "move", move)
    source = built(space, albumartist="The Beatles", album="Abbey Road",
                   title="Come Together", tracknumber="1")

    inbox.deliver(space, source)

    assert seen_mid_move == []


def test_a_move_that_fails_partway_leaves_no_half_file(space, monkeypatch):
    def half(src, dst):
        Path(dst).write_bytes(b"half")
        raise OSError("No space left on device")

    monkeypatch.setattr(inbox.shutil, "move", half)
    source = built(space, title="Song")
    whole = source.read_bytes()

    with pytest.raises(OSError):
        inbox.deliver(space, source)

    assert source.read_bytes() == whole
    assert inbox.waiting(space) == []


def test_an_uploaded_names_single_dot_survives_for_refusal_and_dots_are_kept():
    """sanitize() now replaces a single leading dot (2H4); the upload route
    still has to see it to refuse macOS's ._ companions. A title that is an
    ellipsis is a real track and is kept as it is."""

    assert inbox_routes._relpath_segments("Album/._01.mp3", None)[-1] == "._01.mp3"
    assert inbox_routes._relpath_segments("Album/... (Continued).mp3", None)[-1] \
        == "... (Continued).mp3"


# --- one bad workspace is its own problem (2M5) -------------------------------

def test_an_unreadable_marker_does_not_stop_anybody_elses_inbox(space):
    """A root-owned or damaged .owner raised out of workspace.existing(), and
    nobody's inbox was filed - every fifteen seconds, for ever."""
    from app import workspace

    broken = space.staging.parent / "kelly-2"
    broken.mkdir()
    (broken / ".owner").write_bytes(b"\xff\xfe not text")
    drop(space, albumartist="Artist", album="Album", title="Song",
         tracknumber="1")

    results = inbox.drain_all()

    assert results["alex"].changed
    assert workspace.unreadable() == 1


def test_a_drain_that_raises_is_that_workspaces_failure_alone(space, monkeypatch):
    from app import workspace

    other = workspace.Workspace(username="kelly", library_id=2,
                                library_name="Music",
                                library_path=space.library_path)
    other.prepare()
    real = inbox.drain

    def drain(which):
        if which.username == "kelly":
            raise PermissionError("cannot list the inbox")
        return real(which)

    monkeypatch.setattr(inbox, "drain", drain)
    drop(space, albumartist="Artist", album="Album", title="Song",
         tracknumber="1")

    results = inbox.drain_all()

    assert results["alex"].changed
    assert "PermissionError" in results["kelly"].broken


# --- what the poller cannot file is reported, not forgotten (2M6) -------------

def test_what_will_never_be_filed_is_listed_with_why(space):
    """These sat in the inbox for ever, counted nowhere: waiting() passes
    over them in silence."""
    import os
    import time

    inbox_dir = space.inbox_dir
    (inbox_dir / "Album").mkdir(parents=True)
    (inbox_dir / "Album" / "01 - Song.wma").write_bytes(b"x")
    (inbox_dir / ".5 The Gray Chapter").mkdir()
    (inbox_dir / ".5 The Gray Chapter" / "01 - Prelude.mp3").write_bytes(b"x")
    future = inbox_dir / "Later.mp3"
    future.write_bytes(b"x")
    ahead = time.time() + 86400
    os.utime(future, (ahead, ahead))
    (inbox_dir / ".incomplete" / "job").mkdir(parents=True)
    (inbox_dir / ".incomplete" / "job" / "item.mp3").write_bytes(b"x")

    found = inbox.overlooked(space)

    assert len(found) == 3
    assert any("Song.wma: a format this cannot tag" in line for line in found)
    assert any("Prelude.mp3: in a hidden folder" in line for line in found)
    assert any("Later.mp3: dated in the future" in line for line in found)


def test_the_last_pass_is_kept_for_its_owner_alone(space, monkeypatch):
    monkeypatch.setattr(inbox, "_status", {})

    def boom(space, path, adopt_album=True):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(inbox.filer, "file_track", boom)
    drop(space, albumartist="Artist", album="Album", title="Song",
         tracknumber="1")
    (space.inbox_dir / "Other.wma").write_bytes(b"x")

    inbox.drain_all()

    [entry] = inbox.status_for("alex")
    assert any("read-only filesystem" in line for line in entry["failures"])
    assert entry["overlooked"] == ["Other.wma: a format this cannot tag, so cannot file"]
    assert inbox.status_for("kelly") == []


# --- an upload too big is refused before it is stored (2L4) ----------------------

def test_an_oversized_upload_is_refused_from_its_declared_length():
    """The handler's check came after Starlette had spooled the whole body
    to /tmp, so any size was stored first."""

    big = str(inbox_routes.MAX_UPLOAD_BYTES + inbox_routes.UPLOAD_OVERHEAD + 1)
    assert inbox_routes._upload_refusal({"content-length": big}).status_code == 413
    assert inbox_routes._upload_refusal({}).status_code == 411
    assert inbox_routes._upload_refusal({"content-length": "12345"}) is None


def test_the_middleware_asks_before_the_body_is_read():
    import inspect

    from app import main

    source = inspect.getsource(main.require_session)
    assert source.index("_upload_refusal") < source.index("call_next(request)")


# --- filing waits for the album it goes into (2D3) ---------------------------------

def test_a_drop_waits_while_its_album_is_being_changed(space):
    """The lock covered where files left from, never where they arrived: a
    drop landed in the middle of a rename or a ReplayGain run."""
    from app import folderlock

    drop(space, albumartist="Artist", album="Album", title="Song", tracknumber="1")
    going = space.library_path / "Artist" / "Album"

    with folderlock.holding(going):
        held = inbox.drain(space)
    assert held.filed == [] and held.waiting == 1 and held.failures == []

    assert len(inbox.drain(space).filed) == 1


def test_a_download_waits_for_its_album_then_gives_up_cleanly(space, monkeypatch, tmp_path):
    from app import folderlock

    monkeypatch.setattr(inbox, "DELIVER_WAIT", 0)
    built = space.incomplete_dir / "job" / "item.mp3"
    built.parent.mkdir(parents=True)
    tagged(built, albumartist="Artist", album="Album", title="Song", tracknumber="1")

    with folderlock.holding(space.library_path / "Artist" / "Album"):
        with pytest.raises(folderlock.Busy):
            inbox.deliver(space, built)

    assert built.exists(), "it went back where it was built, to be retried"
