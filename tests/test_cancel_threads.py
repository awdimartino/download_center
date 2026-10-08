"""Cancelling a job stops its threads, not only the wait for them (2M2).

`task.cancel()` used to end the asyncio side alone. A download carried on
into a scratch folder already deleted, its gate slot handed to the next
download meanwhile; a track cancelled while filing was filed anyway, marked
cancelled with no path, and filed a second time by Retry.
"""

from __future__ import annotations

import asyncio
import http.server
import threading
import time
from types import SimpleNamespace

import pytest

from app import downloader, worker


async def _until(flag: threading.Event, seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while not flag.is_set():
        assert time.monotonic() < deadline, "never happened"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_cancelled_download_stops_its_thread_before_the_task_ends(
        monkeypatch, tmp_path):
    started, finished = threading.Event(), threading.Event()

    def download(url, destination, on_progress=None, stop=None):
        started.set()
        try:
            # What yt-dlp's hooks do, chunk by chunk.
            for _ in range(500):
                if stop is not None and stop.is_set():
                    raise downloader.Cancelled()
                time.sleep(0.01)
            return destination
        finally:
            finished.set()

    monkeypatch.setattr(worker.downloader, "download", download)
    item = {"id": "1", "title": "T", "status": "queued"}
    task = asyncio.create_task(
        worker._download_with_retries(item, "https://example.test/v", tmp_path / "x"))
    await _until(started)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Stopped, and stopped before the cancellation was let through: the
    # gate slot and the scratch folder are only released after this.
    assert finished.is_set()


@pytest.mark.asyncio
async def test_a_track_cancelled_while_filing_is_reported_where_it_went(
        monkeypatch, tmp_path):
    filing, release = threading.Event(), threading.Event()
    landed = tmp_path / "library" / "01 - T.mp3"

    def deliver(space, path):
        filing.set()
        release.wait(5)
        return SimpleNamespace(path=landed, identified=True)

    monkeypatch.setattr(worker.downloader, "download",
                        lambda url, destination, on_progress=None, stop=None: destination)
    monkeypatch.setattr(worker.tagger, "tag", lambda path, item: None)
    monkeypatch.setattr(worker.inbox, "deliver", deliver)
    monkeypatch.setattr(worker.inbox, "scratch_path",
                        lambda space, job, item: tmp_path / "scratch" / item)
    monkeypatch.setattr(worker.settings, "rate_limit_sleep", 0)

    item = {"id": "1", "title": "T", "status": "queued",
            "direct_url": "https://example.test/v"}
    task = asyncio.create_task(worker._process_item(item, None, "j1", worker.Gate()))
    await _until(filing)
    task.cancel()
    await asyncio.sleep(0.05)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert item["status"] == "complete"
    assert item["file_path"] == str(landed)


class _Slow(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - the handler's name
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(4_000_000))
        self.end_headers()
        try:
            for _ in range(400):
                self.wfile.write(b"\0" * 10_000)
                time.sleep(0.01)
        except OSError:
            pass

    def log_message(self, *args):
        pass


def test_the_real_downloader_gives_up_when_asked(tmp_path):
    """yt-dlp lets a caller in only through its hooks; raising there is the
    way it supports to abort."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = threading.Event()
    stop.set()
    url = f"http://127.0.0.1:{server.server_address[1]}/song.mp3"
    began = time.monotonic()
    try:
        with pytest.raises(downloader.Cancelled):
            downloader.download(url, tmp_path / "song.mp3", stop=stop)
    finally:
        server.shutdown()
    assert time.monotonic() - began < 3
