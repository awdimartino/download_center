"""Long work running off the request, one at a time."""

from __future__ import annotations

import threading
import time

import pytest

from app import diskaudit, operations


@pytest.fixture(autouse=True)
def clean():
    operations.reset()
    operations._on_change = None
    yield
    operations.reset()


async def _settle(operation):
    if operation.task:
        await operation.task


@pytest.mark.asyncio
async def test_an_operation_runs_and_reports_its_result():
    operation, started = operations.start("import", "alex", lambda: {"ran": True})
    assert started is True
    await _settle(operation)

    assert operation.status == operations.DONE
    assert operation.result == {"ran": True}
    assert operation.error is None


@pytest.mark.asyncio
async def test_starting_one_already_running_reports_the_one_in_flight():
    """Queueing a second gains nothing - these are idempotent sweeps - and
    costs a thread blocked on a lock, which is what used to starve the pool."""
    release = threading.Event()

    def slow():
        release.wait(timeout=5)
        return {"ran": True}

    first, started_first = operations.start("import", "alex", slow)
    second, started_second = operations.start("import", "alex", slow)

    assert started_first is True
    assert started_second is False
    assert second is first

    release.set()
    await _settle(first)


# Keyed by name alone, Kelly starting a combine while Alex's ran was handed
# Alex's operation - hers never ran and its result went to him - and every
# person's results were readable by everyone (CODE_REVIEW H3).

@pytest.mark.asyncio
async def test_another_persons_operation_does_not_block_yours():
    release = threading.Event()

    def slow():
        release.wait(timeout=5)
        return {"who": "alex"}

    alex, _ = operations.start("combine", "alex", slow)
    kelly, started = operations.start("combine", "kelly", lambda: {"who": "kelly"})
    await _settle(kelly)

    assert started is True
    assert kelly is not alex
    assert kelly.result == {"who": "kelly"}
    release.set()
    await _settle(alex)
    assert alex.result == {"who": "alex"}


@pytest.mark.asyncio
async def test_you_see_only_your_own_operations():
    operation, _ = operations.start("candidates", "alex",
                                    lambda: {"folder": "Alex/Private"})
    await _settle(operation)

    assert [op["owner"] for op in operations.all_operations("alex")] == ["alex"]
    assert operations.all_operations("kelly") == []


def test_stopping_is_asked_of_your_own_run_only():
    operations.get("replaygain", "alex").status = operations.RUNNING

    operations.stop("replaygain", "kelly")

    assert operations.stopping("replaygain", "alex") is False


@pytest.mark.asyncio
async def test_a_failure_is_recorded_rather_than_raised():
    def boom():
        raise RuntimeError("beets exploded")

    operation, _ = operations.start("import", "alex", boom)
    await _settle(operation)

    assert operation.status == operations.FAILED
    assert "beets exploded" in operation.error


@pytest.mark.asyncio
async def test_a_finished_operation_can_be_started_again():
    operation, _ = operations.start("import", "alex", lambda: {"n": 1})
    await _settle(operation)
    again, started = operations.start("import", "alex", lambda: {"n": 2})
    await _settle(again)

    assert started is True
    assert again.result == {"n": 2}


@pytest.mark.asyncio
async def test_finishing_announces_so_the_browser_hears_about_it():
    """The request returned long ago; the socket is how the answer arrives."""
    heard = []

    async def on_change(operation):
        heard.append(operation.as_dict())

    operations.subscribe(on_change)
    operation, _ = operations.start("audit", "alex", lambda: {"files": 3})
    await _settle(operation)

    assert len(heard) == 1
    assert heard[0]["status"] == operations.DONE
    assert heard[0]["result"] == {"files": 3}


@pytest.mark.asyncio
async def test_an_unstarted_operation_reads_as_idle():
    assert operations.get("import", "alex").status == operations.IDLE
    assert operations.get("import", "alex").running is False


# --- the locks that used to block a threadpool worker ---------------------


def test_concurrent_audits_share_one_walk(tmp_path, monkeypatch):
    """The docstring always claimed this. It used to serialise instead: the
    second caller waited for the first, then walked the library again."""
    root = tmp_path / "music"
    root.mkdir()
    (root / "a.mp3").write_bytes(b"x")
    monkeypatch.setattr(diskaudit, "_cache", {})
    monkeypatch.setattr(diskaudit, "_walking", {})

    walks = []
    real_run = diskaudit.run

    def counted(path):
        walks.append(path)
        time.sleep(0.2)
        return real_run(path)

    monkeypatch.setattr(diskaudit, "run", counted)

    results = []
    threads = [threading.Thread(target=lambda: results.append(diskaudit.refresh(root)))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(walks) == 1, f"expected one walk, got {len(walks)}"
    assert len(results) == 4
    assert all(r is not None for r in results)


def test_a_walk_for_a_different_root_is_not_blocked(tmp_path, monkeypatch):
    """Two libraries are two directories; one being read is no reason to
    make the other wait."""
    monkeypatch.setattr(diskaudit, "_cache", {})
    monkeypatch.setattr(diskaudit, "_walking", {})
    alex, kelly = tmp_path / "music", tmp_path / "kelly"
    alex.mkdir()
    kelly.mkdir()

    # Claim alex's root as though a walk were under way.
    diskaudit._walking[str(alex)] = threading.Event()
    audit = diskaudit.refresh(kelly)

    assert audit is not None
    assert str(kelly) in diskaudit._cache


# --- the nightly sweep ------------------------------------------------------
# Once a night, not every quarter of an hour. Beets does a MusicBrainz lookup
# per item and moves files about, and on a machine serving music over a weak
# wifi link that is felt as stuttering playback.


