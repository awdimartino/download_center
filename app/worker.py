"""Runs a resolved job: dedupe, match, download, tag, file.

A track goes into the library the moment it is finished, tagged from Spotify
and playable. Nothing waits for its album to be complete, nothing waits for a
nightly sweep, and nothing is held back because a matcher would not vouch for
it - beets refused 82% of what it was handed, almost all of it for mechanical
reasons that had nothing to do with the music.

So there is no publish step and no batch. Each item files itself as it lands,
which is also what makes a partial job useful: nine tracks of a twelve track
record are nine tracks you can play, sitting under the album they belong to,
rather than a directory nobody is allowed to look at.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from collections.abc import Awaitable, Callable

from . import downloader, inbox, matcher, navidrome, tagger
from . import workspace
from .config import settings

log = logging.getLogger("navidrome_companion.worker")

# Delay before each retry. A transient 429 or dropped connection clears
# quickly; anything still failing after half a minute is not going to fix
# itself by trying harder.
BACKOFF = (2, 8, 30)

# How often the browser is told about in-flight progress. yt-dlp's hook fires
# hundreds of times per file, which is far more than a UI needs.
PUSH_INTERVAL = 0.5

# One gate for the whole process, not one per job.
#
# It used to be created inside run_job, so `concurrency` meant "per job" and
# three queued playlists ran three times as many downloads at once - each
# spawning yt-dlp and ffmpeg, on a Raspberry Pi. rate_limit_sleep had the
# same problem: it paced one job while the others ignored it, which is not
# what "stay under YouTube's radar" means.
#
# Sized by the setting at the moment each download asks, not when the gate
# was made. Rebuilding a Semaphore when the setting changed left jobs already
# running on the old one, so old and new together ran past either limit.
# Lowering the limit holds new downloads until enough finish; raising it
# lets more in as each one finishes.
#
# Built lazily because an asyncio primitive binds to the running loop.
class Gate:
    def __init__(self) -> None:
        self._active = 0
        self._turn = asyncio.Condition()

    def _room(self) -> bool:
        return self._active < max(1, settings.concurrency)

    def locked(self) -> bool:
        return not self._room()

    async def __aenter__(self) -> None:
        async with self._turn:
            await self._turn.wait_for(self._room)
            self._active += 1

    async def __aexit__(self, *exc: object) -> None:
        async with self._turn:
            self._active -= 1
            self._turn.notify_all()


_gate: Gate | None = None


def gate() -> Gate:
    global _gate
    if _gate is None:
        _gate = Gate()
    return _gate


Push = Callable[[dict[str, Any]], Awaitable[None]]
# Job plus only the items whose visible state moved.
Progress = Callable[[dict[str, Any], list], Awaitable[None]]


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


async def _match_with_retries(item: dict[str, Any]) -> Any:
    """Find the recording, retrying only when the search itself failed.

    A MatchError means the results were seen and none of them was good
    enough; asking again returns the same results. A SearchUnavailable means
    the question never got through - a 429, a dropped connection - and that
    clears on its own. Treating the two alike meant one rate limit failed
    every remaining track in the job, permanently, with a message blaming
    YouTube's catalogue for a problem with the connection.
    """
    last: Exception | None = None
    for attempt in range(1, settings.max_attempts + 1):
        try:
            return await asyncio.to_thread(matcher.find, item)
        except matcher.SearchUnavailable as exc:
            last = exc
            if attempt >= settings.max_attempts:
                break
            delay = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
            log.warning("search unavailable (%s), retrying in %ds",
                        str(exc)[:80], delay)
            _mark(item, "retrying", error=str(exc)[:200])
            await asyncio.sleep(delay)
    raise last if last else matcher.SearchUnavailable("search failed")


async def _process(item: dict[str, Any], space: workspace.Workspace,
                   job_id: str, gate: Gate) -> None:
    """One item, whatever goes wrong with it.

    Every error is this item's failure, with its message. One that escaped
    used to reach `gather`, which re-raised it without cancelling the other
    items: they went on downloading and holding slots after the job had been
    marked failed and dropped - uncancellable, undeletable, stuck animating,
    and Retry said there was nothing to retry.
    """
    try:
        await _process_item(item, space, job_id, gate)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("unexpected error on %s", item.get("title"))
        _mark(item, "failed", error=f"{type(exc).__name__}: {exc}"[:200],
              progress=0)


async def _process_item(item: dict[str, Any], space: workspace.Workspace,
                        job_id: str, gate: Gate) -> None:
    # Already in the library. A retry resets the failures and re-runs the
    # whole job, so without this every track that succeeded the first time is
    # downloaded and filed again - one duplicate per completed track, per
    # press of Retry. The ledger lookup used to be what stopped that; the
    # guard belongs here instead, where the work is started, rather than in a
    # store that no longer exists.
    if item["status"] == "complete":
        return

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
                url, score, _parts = await _match_with_retries(item)
            except matcher.MatchError as exc:
                # Not retried: an identical search returns identical results,
                # so trying again only burns time. This is a judgement about
                # the results, not about reaching YouTube - see
                # _match_with_retries for the case that is worth another go.
                _mark(item, "failed", error=str(exc))
                return
            except matcher.SearchUnavailable as exc:
                _mark(item, "failed",
                      error=f"Could not reach YouTube Music: {exc}"[:200])
                return
            except Exception as exc:
                _mark(item, "failed", error=f"Search error: {exc}"[:200])
                return

            item["match_url"] = url
            item["match_score"] = round(score, 3)

        temp = inbox.scratch_path(space, job_id, item["id"])
        try:
            path = await _download_with_retries(item, url, temp)
        except downloader.DownloadError as exc:
            _mark(item, "failed", error=str(exc)[:200], progress=0)
            return

        _mark(item, "tagging", progress=1.0)
        try:
            await asyncio.to_thread(tagger.tag, path, item)
        except Exception as exc:
            # Fatal. These tags are what the file is filed by and what
            # Navidrome shows, so an untagged download went in as
            # Unknown Artist/Unknown Album/<item id>.mp3 and said Done. The
            # file stays in scratch space, cleared with the job, and Retry
            # fetches it again.
            log.warning("tagging failed for %s: %s", item["title"], exc)
            _mark(item, "failed", error=f"Could not tag the download: {exc}"[:200],
                  progress=0)
            return

        # Into the inbox, and filed from there - the same road a
        # hand-dropped file takes. The worker only calls it directly rather
        # than waiting for the poller to work out what it already knows.
        try:
            filed = await asyncio.to_thread(inbox.deliver, space, path)
        except Exception as exc:
            log.exception("could not file %s", item["title"])
            _mark(item, "failed", error=f"Could not file the download: {exc}"[:200])
            return

        # error=None: a retry along the way left its message behind, shown
        # as a tooltip on a row that says Done.
        _mark(item, "complete", file_path=str(filed.path), error=None)
        if not filed.identified:
            # Filed and playable, but nothing can follow it: stars, play
            # counts and the Library's album records all hang off the UUIDs.
            # The inbox reports the same thing for a hand-dropped file.
            item["warning"] = ("Filed, but its identity tags could not be "
                               "written, so stars and play counts cannot "
                               "follow it.")

        if settings.rate_limit_sleep:
            await asyncio.sleep(settings.rate_limit_sleep)


def _item_state(item: dict[str, Any]) -> tuple:
    """The part of an item a browser can see change.

    Progress is rounded to a percent: yt-dlp's hook fires hundreds of times
    per file and nobody can see a thousandth of a bar move.
    """
    return (item["status"], round((item.get("progress") or 0) * 100),
            item.get("error"))


async def _pusher(job: dict[str, Any], push: Push,
                  push_progress: Progress | None = None) -> None:
    """Tell the browser what changed, at a fixed rate, while the job runs.

    It used to send the entire job - every track, with all its metadata -
    twice a second. For a 200-track playlist that is the whole list
    re-serialised and sent to a phone 120 times a minute, almost all of it
    identical to the last one. Now only the items whose visible state moved
    are sent, and nothing at all is sent when nothing moved.
    """
    last: dict[str, tuple] = {}
    last_status = None
    try:
        while True:
            await asyncio.sleep(PUSH_INTERVAL)
            changed = [item for item in job["items"]
                       if last.get(item["id"]) != _item_state(item)]
            for item in changed:
                last[item["id"]] = _item_state(item)

            if not changed and job["status"] == last_status:
                continue
            last_status = job["status"]

            if push_progress is None:
                await push(job)
            else:
                await push_progress(job, changed)
    except asyncio.CancelledError:
        pass


async def run_job(job: dict[str, Any], push: Push,
                  space: workspace.Workspace,
                  push_progress: Progress | None = None) -> None:
    """Drive one job to completion, into one person's library.

    The workspace comes from whoever queued the job rather than from
    configuration: their scratch space and their destination. Nothing
    downstream has to know whose download this was.
    """
    items = job["items"]
    job["status"] = "running"
    await push(job)

    # Everything asked for is fetched. Re-download prevention was a ledger
    # row saying a track had been downloaded once, which stayed true after the
    # file was deleted, replaced or moved - so a track that left the library
    # became permanently unfetchable with nothing to say why.
    ticker = asyncio.create_task(_pusher(job, push, push_progress))
    try:
        await asyncio.gather(
            *(_process(item, space, job["id"], gate()) for item in items)
        )
    finally:
        ticker.cancel()

    # Each item filed itself as it finished, so there is nothing to publish
    # and nothing to import. An album that came up short is simply a few
    # tracks of that album, in that album's folder, playable now and listed
    # for review until someone confirms what they are.
    filed = [item for item in items if item["status"] == "complete"]

    await asyncio.to_thread(inbox.discard, space, job["id"])

    failed = sum(1 for item in items if item["status"] == "failed")
    job["status"] = ("complete" if not failed
                     else "failed" if not filed else "partial")
    log.info("%s: %d filed, %d failed", job["title"], len(filed), failed)
    await push(job)

    # The files are already in the library and Navidrome will find them on
    # its own schedule; this only makes it sooner. Failing it is not worth
    # failing the job over.
    if filed:
        await asyncio.to_thread(navidrome.notify)
