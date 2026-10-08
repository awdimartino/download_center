"""Long work runs on a pool of its own, so it cannot starve requests (2H2).

Each test shrinks the loop's default pool to one thread, starts long work
that blocks until released, then asks the default pool for something quick.
When the long work used `asyncio.to_thread`, it held that one thread and the
quick call waited - which on the Pi was every page, sign-in and healthcheck
behind eight downloads and a ReplayGain run.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import downloader, operations, spotify, worker


async def _default_pool_still_answers() -> bool:
    try:
        return await asyncio.wait_for(asyncio.to_thread(lambda: True), 2)
    except TimeoutError:
        return False


@pytest.fixture
def one_default_thread():
    pool = ThreadPoolExecutor(max_workers=1)
    yield pool
    pool.shutdown(wait=False, cancel_futures=True)


@pytest.mark.asyncio
async def test_an_operation_leaves_the_default_pool_free(one_default_thread):
    asyncio.get_running_loop().set_default_executor(one_default_thread)
    release = threading.Event()
    started = threading.Event()

    def work():
        started.set()
        release.wait(10)
        return {}

    operation, _ = operations.start("threads-test", "alex", work)
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.02)
        assert started.is_set()
        assert await _default_pool_still_answers()
    finally:
        release.set()
        await operation.task


@pytest.mark.asyncio
async def test_a_download_leaves_the_default_pool_free(one_default_thread, monkeypatch, tmp_path):
    asyncio.get_running_loop().set_default_executor(one_default_thread)
    release = threading.Event()
    started = threading.Event()

    def download(url, temp, progress):
        started.set()
        release.wait(10)
        return temp

    monkeypatch.setattr(downloader, "download", download)
    item = {"id": "1", "title": "T", "status": "queued"}
    task = asyncio.create_task(
        worker._download_with_retries(item, "https://example.test/v", tmp_path / "x"))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.02)
        assert started.is_set()
        assert await _default_pool_still_answers()
    finally:
        release.set()
        await task


def test_spotify_does_not_wait_out_a_retry_after():
    """urllib3 honours Retry-After up to six hours per retry; one long rate
    limit held a thread for the whole wait."""
    session = spotify._session()
    retry = session.get_adapter("https://api.spotify.com").max_retries
    assert retry.respect_retry_after_header is False
    assert 429 in retry.status_forcelist
