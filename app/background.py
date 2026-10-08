"""The loops that run with nobody signed in, and stopping work at shutdown."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from . import (
    diskaudit,
    heartbeat,
    inbox,
    navidrome,
    operations,
    overview,
    playcounts,
    threads,
    workspace,
)
from .config import settings
from .jobs import RUNNING, _stop_job

log = logging.getLogger("navidrome_companion")


# Process start, for the uptime the health panel reports.
STARTED_AT = time.time()


# How long a shutdown waits for operations to reach a stopping point. Docker
# kills the process when its own grace period runs out, so the compose file
# gives it longer than this.
SHUTDOWN_GRACE = 60


async def _wind_down() -> None:
    """Stop in-flight work before the process exits.

    A shutdown used to cancel the three loops and nothing else. Downloads
    and operations went on in their threads, which kept the process alive
    until Docker killed it - possibly part way through rewriting a file in
    the library. Jobs are cancelled the way the Cancel button does it, which
    stops each download and waits for anything being filed; operations are
    asked to stop after the album or file they are on.
    """
    running = list(RUNNING)
    operation_tasks = operations.stop_all()
    if running or operation_tasks:
        log.info("shutting down: stopping %d job(s) and %d operation(s)",
                 len(running), len(operation_tasks))
    await asyncio.gather(*(_stop_job(job_id) for job_id in running))
    if operation_tasks:
        _, late = await asyncio.wait(operation_tasks, timeout=SHUTDOWN_GRACE)
        if late:
            log.warning("%d operation(s) still running at shutdown", len(late))


def _inbox_problem(results: dict[str, inbox.Result]) -> str | None:
    """What a completed pass still has to say, without naming anybody: Health
    is shown to every account."""
    unreadable = workspace.unreadable()
    broken = sum(1 for result in results.values() if result.broken)
    parts = []
    if unreadable:
        parts.append(f"{unreadable} workspace(s) could not be read")
    if broken:
        parts.append(f"{broken} inbox(es) could not be looked at")
    return "; ".join(parts) + ". See the log." if parts else None


async def _inbox_loop() -> None:
    """File whatever has been dropped into an inbox, as soon as it settles.

    It replaced a nightly sweep. The sweep ran beets, which does a
    MusicBrainz lookup per item and moves files about, and on a machine
    serving music over one link that was felt as stuttering playback - so it
    was pushed to once a night and everything waited hours. Filing reads tags
    and renames, so it can run whenever something appears.
    """
    while True:
        try:
            results = await threads.run(inbox.drain_all)
            # A download asks Navidrome to scan when it finishes; a drop
            # used to wait for Navidrome's own schedule instead.
            if any(result.changed for result in results.values()):
                await threads.run(navidrome.notify)
            heartbeat.ok("inbox", _inbox_problem(results))
        except Exception as exc:
            log.exception("draining the inbox failed")
            heartbeat.failed("inbox", exc)
        await asyncio.sleep(inbox.POLL_SECONDS)


# How often the play counts are read: every five minutes, not nightly.
# Navidrome records the moment of a track's most recent play beside its
# running total, so a reading that
# catches a count rising by one has that play's exact time - and reading
# this often means the rise is almost always one. The cost is a single
# indexed join over a few thousand annotation rows, measured at 34ms on the
# Pi this runs on: about ten seconds of work a day for the difference
# between "you played this 1,204 times" and a listening history.
SNAPSHOT_MINUTES = 5


async def _snapshot_loop() -> None:
    """Read the play counts, every few minutes, for ever.

    Navidrome keeps a cumulative total and the moment of the most recent
    play, so anything not captured between two plays of the same track is
    gone for good. Reading often is what makes the difference recoverable:
    at this cadence a track's count almost always rises by exactly one
    between readings, and the timestamp beside it is then that play's.

    Unconditional, where this used to ask "has today been done". That
    question belonged to a job that ran once a day and had to survive
    restarts without repeating itself; here, repeating is free - only
    changed counts are stored, so a reading that finds nothing new writes
    nothing but the run-log row that says the collector is alive.
    """
    # Everyone on the first pass, so the first visit after a restart or a
    # deploy is not the one that computes; after that, whoever just played
    # something - a reading with new plays is what makes their cached
    # statistics stale.
    first = True
    while True:
        try:
            taken = await threads.run(playcounts.take)
            if first or taken.get("users"):
                await threads.run(
                    overview.warm, None if first else taken["users"])
            first = False
            heartbeat.ok("snapshot")
        except Exception as exc:
            log.exception("play-count snapshot failed")
            heartbeat.failed("snapshot", exc)
        await asyncio.sleep(SNAPSHOT_MINUTES * 60)


async def _audit_loop() -> None:
    """Keep the on-disk identity audit reasonably fresh.

    It reads tags from every file in the library, so it cannot run inside a
    request. Refreshing on a slow timer means the health panel always has an
    answer, even if it is a few hours old - and a stale answer is only old,
    never wrong, because nothing here writes anything.
    """
    while True:
        # The whole pass, not only each audit: reading the library list can
        # fail too (a locked database), and an exception escaping here ended
        # the task for the life of the process while Health's audit aged.
        try:
            failed = 0
            for root in await threads.run(_library_roots):
                if await threads.run(diskaudit.stale, root):
                    try:
                        await threads.run(diskaudit.refresh, root)
                    except Exception:
                        failed += 1
                        log.exception("disk audit failed for %s", root)
            heartbeat.ok("audit", f"{failed} library audit(s) failed. See the "
                                  "log." if failed else None)
        except Exception as exc:
            log.exception("disk audit pass failed")
            heartbeat.failed("audit", exc)
        await asyncio.sleep(600)


def _library_roots() -> list[Path]:
    """Every library Navidrome knows about, as paths inside this container.

    Read from Navidrome rather than configured, so a library added there is
    audited here without anyone remembering to say so. A path we cannot see
    is reported once rather than retried silently forever.
    """
    try:
        connection = navidrome.open_db()
    except navidrome.Unavailable:
        return [settings.music_dir]
    with connection:
        rows = connection.execute("select name, path from library").fetchall()

    roots = []
    for name, path in rows:
        root = Path(path)
        if root.is_dir():
            roots.append(root)
        elif str(root) not in _warned_missing:
            _warned_missing.add(str(root))
            log.warning("library %r is at %s, which is not mounted here - "
                        "its files cannot be audited or stamped", name, root)
    return roots or [settings.music_dir]


# Libraries we have already complained about, so the log says it once.
_warned_missing: set[str] = set()
