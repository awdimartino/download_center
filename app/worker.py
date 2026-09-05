"""Runs a resolved job: dedupe, match, download, tag, publish.

Ordering here is deliberate. Every item is checked against the ledger before
any layout is planned, because a track already in the library must not count
towards its album being complete. Re-queuing a record you hold eight tracks of
would otherwise build an "album" directory containing two files, and beets
would try to match that against the full release and fail.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from . import beets_runner, downloader, ledger, matcher, staging, tagger
from . import workspace
from .config import settings

log = logging.getLogger("download_center.worker")

# Delay before each retry. A transient 429 or dropped connection clears
# quickly; anything still failing after half a minute is not going to fix
# itself by trying harder.
BACKOFF = (2, 8, 30)

# How often the browser is told about in-flight progress. yt-dlp's hook fires
# hundreds of times per file, which is far more than a UI needs.
PUSH_INTERVAL = 0.5

Push = Callable[[dict[str, Any]], Awaitable[None]]


def _mark(item: dict[str, Any], status: str, **fields: Any) -> None:
    item["status"] = status
    item.update(fields)


async def _download_with_retries(item: dict[str, Any], url: str, temp) -> Any:
    """Download, retrying only failures that could plausibly succeed later."""
    def progress(fraction: float) -> None:
        # Called from yt-dlp's thread. Mutating the dict is enough; the
        # periodic pusher picks the value up on its next tick.
        item["progress"] = fraction

    last_error = ""
    for attempt in range(1, settings.max_attempts + 1):
        item["attempts"] = attempt
        _mark(item, "downloading")
        try:
            return await asyncio.to_thread(downloader.download, url, temp, progress)
        except downloader.DownloadError as exc:
            last_error = str(exc)
            if attempt >= settings.max_attempts:
                break
            delay = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
            log.warning("download attempt %d failed (%s), retrying in %ds",
                        attempt, last_error[:80], delay)
            _mark(item, "retrying", error=last_error)
            await asyncio.sleep(delay)

    raise downloader.DownloadError(last_error)


async def _process(item: dict[str, Any], dest: dict[str, Any],
                   gate: asyncio.Semaphore) -> None:
    async with gate:
        # Items from a direct link already name their audio, so there is
        # nothing to search for and no confidence to score.
        direct = item.get("direct_url")
        if direct:
            url = direct
            item["match_url"] = direct
        else:
            _mark(item, "matching")
            try:
                url, score, _parts = await asyncio.to_thread(matcher.find, item)
            except matcher.MatchError as exc:
                # Not retried: an identical search returns identical results,
                # so trying again only burns time.
                _mark(item, "failed", error=str(exc))
                return
            except Exception as exc:
                _mark(item, "failed", error=f"Search error: {exc}"[:200])
                return

            item["match_url"] = url
            item["match_score"] = round(score, 3)

        try:
            path = await _download_with_retries(item, url, dest["temp"])
        except downloader.DownloadError as exc:
            _mark(item, "failed", error=str(exc)[:200], progress=0)
            return

        _mark(item, "tagging", progress=1.0)
        try:
            await asyncio.to_thread(tagger.tag, path, item)
        except Exception as exc:
            # A tagging failure is not fatal: beets will retag anyway, and a
            # correct file with poor tags beats discarding the download.
            log.warning("tagging failed for %s: %s", item["title"], exc)

        _mark(item, "complete", file_path=str(dest["final"]))

        if settings.rate_limit_sleep:
            await asyncio.sleep(settings.rate_limit_sleep)


async def _pusher(job: dict[str, Any], push: Push) -> None:
    """Send the job to the browser at a fixed rate while it is running."""
    try:
        while True:
            await asyncio.sleep(PUSH_INTERVAL)
            await push(job)
    except asyncio.CancelledError:
        pass


async def run_job(job: dict[str, Any], push: Push,
                  space: workspace.Workspace) -> None:
    """Drive one job to completion, into one person's library.

    The workspace comes from whoever queued the job rather than from
    configuration: their staging area, their beets database, their
    destination. Nothing downstream has to know whose download this was.
    """
    items = job["items"]
    job["status"] = "running"
    await push(job)

    # Dedupe first, so completeness is judged on what will actually be written.
    pending = []
    for item in items:
        held = await asyncio.to_thread(
            ledger.already_downloaded, item["spotify_id"], item.get("isrc")
        )
        if held:
            _mark(item, "skipped")
        else:
            pending.append(item)

    if not pending:
        job["status"] = "complete"
        log.info("%s: every track already in the ledger", job["title"])
        await push(job)
        return

    layout = staging.plan(space, job["id"], pending)
    gate = asyncio.Semaphore(settings.concurrency)
    ticker = asyncio.create_task(_pusher(job, push))

    try:
        await asyncio.gather(
            *(_process(item, layout[item["id"]], gate) for item in pending)
        )
    finally:
        ticker.cancel()

    demoted = await asyncio.to_thread(
        staging.demote_partial_albums, space, job["id"], pending, layout
    )
    if demoted:
        log.info("%s: %d track(s) demoted to singles (album incomplete)",
                 job["title"], demoted)

    published: list = []
    try:
        published = await asyncio.to_thread(staging.publish, space, job["id"])
        log.info("%s: published %d path(s)", job["title"], len(published))
    except Exception as exc:
        log.exception("publishing failed for %s", job["title"])
        job["error"] = f"Publishing failed: {exc}"
    else:
        # Recorded only now, so the ledger reflects what reached the staging
        # tree. A job that dies before publishing leaves orphans in scratch
        # space that should be fetched again, not skipped.
        for item in pending:
            if item["status"] == "complete":
                await asyncio.to_thread(ledger.record, item, item.get("file_path"))

    failed = sum(1 for item in items if item["status"] == "failed")
    done = sum(1 for item in items if item["status"] in ("complete", "skipped"))
    job["status"] = "complete" if not failed else ("failed" if not done else "partial")
    log.info("%s: %d done, %d failed", job["title"], done, failed)
    await push(job)

    # Tagging is a separate phase with its own status, because it can take a
    # while and its outcome is independent of whether the downloads worked.
    if published:
        job["status"] = "tagging"
        await push(job)
        try:
            job["beets"] = await asyncio.to_thread(
                beets_runner.import_paths, space, published)
        except Exception as exc:
            log.exception("beets import raised for %s", job["title"])
            job["beets"] = {"ran": True, "imported": 0, "skipped": 0,
                            "failed": [str(exc)[:200]]}
        job["status"] = "complete" if not failed else ("failed" if not done else "partial")
        await push(job)
