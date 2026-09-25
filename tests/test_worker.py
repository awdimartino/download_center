"""What the browser is told while a job runs.

The pusher used to send the entire job twice a second regardless of whether
anything had changed. For a 200-track playlist that is the whole list
re-serialised and sent to a phone 120 times a minute, almost all of it
identical to the last one.
"""

from __future__ import annotations

import asyncio

import pytest

from app import worker


def _job(items, status="running"):
    return {"id": "j1", "owner": "alex", "status": status, "items": items}


def _item(item_id, status="pending", progress=0.0, error=None):
    return {"id": item_id, "status": status, "progress": progress,
            "error": error}


async def _tick(job, seen, ticks=1):
    """Run the pusher for a few intervals, then stop it."""
    async def push(j):
        seen.append(("full", None))

    async def push_progress(j, changed):
        seen.append(("delta", [i["id"] for i in changed]))

    task = asyncio.create_task(worker._pusher(job, push, push_progress))
    await asyncio.sleep(worker.PUSH_INTERVAL * ticks + 0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.fixture(autouse=True)
def fast_ticks(monkeypatch):
    monkeypatch.setattr(worker, "PUSH_INTERVAL", 0.02)


# --- what counts as a change ----------------------------------------------

def test_progress_is_compared_at_one_percent():
    """yt-dlp's hook fires hundreds of times per file and nobody can see a
    thousandth of a bar move."""
    a = worker._item_state(_item("x", "downloading", 0.5001))
    b = worker._item_state(_item("x", "downloading", 0.5004))
    assert a == b

    c = worker._item_state(_item("x", "downloading", 0.51))
    assert a != c


def test_status_and_error_are_part_of_the_state():
    base = worker._item_state(_item("x", "downloading", 0.5))
    assert base != worker._item_state(_item("x", "tagging", 0.5))
    assert base != worker._item_state(_item("x", "downloading", 0.5, "boom"))


# --- what actually goes over the socket -----------------------------------

@pytest.mark.asyncio
async def test_nothing_is_sent_when_nothing_moved():
    job = _job([_item("a", "pending")])
    seen = []
    await _tick(job, seen, ticks=3)
    # The first tick reports the initial state; after that, silence.
    assert len(seen) == 1, f"expected one message, got {seen}"


@pytest.mark.asyncio
async def test_only_the_items_that_moved_are_sent():
    items = [_item("a"), _item("b"), _item("c")]
    job = _job(items)
    seen = []

    async def push(j):
        seen.append(("full", None))

    async def push_progress(j, changed):
        seen.append(("delta", [i["id"] for i in changed]))

    task = asyncio.create_task(worker._pusher(job, push, push_progress))
    await asyncio.sleep(worker.PUSH_INTERVAL * 1.5)
    seen.clear()

    items[1]["status"] = "downloading"
    await asyncio.sleep(worker.PUSH_INTERVAL * 1.5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert seen, "a change should be reported"
    kind, ids = seen[0]
    assert kind == "delta"
    assert ids == ["b"], f"only b moved, but {ids} was sent"


@pytest.mark.asyncio
async def test_a_job_status_change_is_reported_even_with_no_item_changes():
    job = _job([_item("a")])
    seen = []

    async def push(j):
        seen.append("full")

    async def push_progress(j, changed):
        seen.append(j["status"])

    task = asyncio.create_task(worker._pusher(job, push, push_progress))
    await asyncio.sleep(worker.PUSH_INTERVAL * 1.5)
    seen.clear()

    job["status"] = "tagging"
    await asyncio.sleep(worker.PUSH_INTERVAL * 1.5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert "tagging" in seen


@pytest.mark.asyncio
async def test_without_a_progress_callback_it_falls_back_to_the_whole_job():
    """Callers that predate the delta path still work."""
    job = _job([_item("a")])
    seen = []

    async def push(j):
        seen.append(j)

    task = asyncio.create_task(worker._pusher(job, push, None))
    await asyncio.sleep(worker.PUSH_INTERVAL * 1.5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert seen and seen[0] is job


# --- a job, end to end ------------------------------------------------------
#
# The change these cover: a finished track goes into the library immediately,
# under the album it says it is on. It used to be written to a staging area
# hidden from Navidrome and held there until beets agreed to admit it, which
# it refused to do for 82% of what it was given.

import shutil
from pathlib import Path

SILENCE = Path(__file__).parent / "fixtures" / "silence.mp3"


@pytest.fixture
def library(tmp_path, monkeypatch, state_db):
    """A workspace with a mounted library, and a download that always works."""
    from app import workspace
    from app.config import settings

    monkeypatch.setattr(settings, "output_dir", tmp_path / "untagged")
    monkeypatch.setattr(settings, "rate_limit_sleep", 0)
    monkeypatch.setattr(workspace, "CONFIG_DIR", tmp_path / "config")
    (tmp_path / "music").mkdir()
    space = workspace.Workspace(username="alex", library_id=1,
                                library_name="Music",
                                library_path=tmp_path / "music")
    space.prepare()

    def download(url, destination, on_progress=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SILENCE, destination)
        return destination

    monkeypatch.setattr(worker.downloader, "download", download)
    monkeypatch.setattr(worker.matcher, "find",
                        lambda item: ("https://example.invalid/x", 0.9, {}))
    monkeypatch.setattr(worker.navidrome, "notify", lambda *a, **k: False)
    # The real tagger runs: it is what decides where the file is filed now,
    # so stubbing it would test nothing. No item here carries a cover_url, so
    # it never reaches the network.
    return space


def _track(number, title, album="Abbey Road", artist="The Beatles"):
    return {
        "id": f"item{number}", "spotify_id": f"s{number}", "isrc": None,
        "title": title, "artist": artist, "primary_artist": artist,
        "album_artist": artist, "album": album, "album_id": "alb1",
        "album_total": 17, "track_no": number, "disc_no": 1,
        "status": "pending", "progress": 0.0, "error": None,
    }


async def _run(space, items):
    job = {"id": "job1", "owner": "alex", "title": "Abbey Road",
           "status": "queued", "items": items}
    await worker.run_job(job, lambda j: asyncio.sleep(0), space)
    return job


@pytest.mark.asyncio
async def test_a_download_lands_in_the_library_not_in_staging(library):
    from app.config import settings

    items = [_track(1, "Come Together")]
    await _run(library, items)

    filed = library.library_path / "The Beatles" / "Abbey Road" / "01 - Come Together.mp3"
    assert filed.is_file()
    assert items[0]["status"] == "complete"
    assert items[0]["file_path"] == str(filed)
    assert not list(settings.output_dir.rglob("*.mp3"))


@pytest.mark.asyncio
async def test_a_filed_track_carries_both_uuids(library):
    from app import uuidtags

    items = [_track(1, "Come Together")]
    await _run(library, items)

    track_uuid, album_uuid = uuidtags.read(Path(items[0]["file_path"]))
    assert track_uuid and album_uuid


@pytest.mark.asyncio
async def test_an_incomplete_album_is_filed_anyway(library):
    """Two tracks of a seventeen track record. They used to wait outside the
    library for a match that never came; now they are two playable tracks in
    that album's folder."""
    items = [_track(1, "Come Together"), _track(2, "Something")]
    await _run(library, items)

    folder = library.library_path / "The Beatles" / "Abbey Road"
    assert sorted(p.name for p in folder.iterdir()) == [
        "01 - Come Together.mp3", "02 - Something.mp3"]


@pytest.mark.asyncio
async def test_every_track_of_one_album_shares_its_album_uuid(library):
    from app import uuidtags

    items = [_track(1, "Come Together"), _track(2, "Something")]
    await _run(library, items)

    albums = {uuidtags.read(Path(item["file_path"]))[1] for item in items}
    assert len(albums) == 1


@pytest.mark.asyncio
async def test_the_scratch_directory_is_cleared(library):
    from app import inbox

    await _run(library, [_track(1, "Come Together")])

    assert not inbox.scratch_root(library, "job1").exists()




# --- nothing is skipped any more --------------------------------------------
#
# The ledger recorded that a track had been fetched once, and that stayed true
# after the file was deleted, replaced or moved to another library - so a
# track that left the library became permanently unfetchable, reported as
# "skipped" with nothing to say why. Browse still marks what the library
# actually holds; it just does not refuse anything.

@pytest.mark.asyncio
async def test_asking_for_the_same_track_twice_downloads_it_twice(library):
    """The second copy is a duplicate for the Duplicates tab to catch, which
    is a far better place for it than a track nobody can fetch."""
    first = [_track(1, "Come Together")]
    await _run(library, first)
    second = [_track(1, "Come Together")]
    await _run(library, second)

    assert second[0]["status"] == "complete"
    folder = library.library_path / "The Beatles" / "Abbey Road"
    assert sorted(p.name for p in folder.iterdir()) == [
        "01 - Come Together (2).mp3", "01 - Come Together.mp3"]


@pytest.mark.asyncio
async def test_a_download_that_fails_leaves_nothing_behind(library,
                                                           monkeypatch):
    from app import inbox

    def boom(url, destination, on_progress=None):
        raise worker.downloader.DownloadError("no audio")

    monkeypatch.setattr(worker.downloader, "download", boom)
    items = [_track(1, "Come Together")]
    job = await _run(library, items)

    assert items[0]["status"] == "failed"
    assert job["status"] == "failed"
    assert not list(library.library_path.rglob("*.mp3"))
    assert not inbox.scratch_root(library, "job1").exists()


@pytest.mark.asyncio
async def test_a_retry_does_not_re_download_what_already_finished(library,
                                                                  monkeypatch):
    """Retry resets the failures and re-runs the whole job. Without a guard
    where the work starts, every track that succeeded the first time is
    downloaded and filed again - one duplicate per completed track, per
    press of Retry."""
    from app.config import settings

    monkeypatch.setattr(settings, "max_attempts", 1)
    fetched = []
    real = worker.downloader.download
    broken = {"i2"}

    def counting(url, destination, on_progress=None):
        fetched.append(destination.stem)
        if destination.stem in broken:
            raise worker.downloader.DownloadError("no audio")
        return real(url, destination, on_progress)

    monkeypatch.setattr(worker.downloader, "download", counting)

    items = [_track(1, "Come Together"), _track(2, "Something")]
    items[0]["id"], items[1]["id"] = "i1", "i2"
    await _run(library, items)

    assert items[0]["status"] == "complete"
    assert items[1]["status"] == "failed"
    assert fetched == ["i1", "i2"]

    # What retry_job does: reset only the failures, then run the job again.
    broken.clear()
    items[1].update(status="pending", error=None, progress=0, attempts=0)
    await _run(library, items)

    assert fetched.count("i1") == 1, "the completed track was fetched again"
    folder = library.library_path / "The Beatles" / "Abbey Road"
    assert sorted(p.name for p in folder.iterdir()) == [
        "01 - Come Together.mp3", "02 - Something.mp3"]
