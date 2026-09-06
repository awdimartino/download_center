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
