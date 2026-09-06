"""Nightly snapshots of what has been played, so history stops being lost.

Navidrome's `annotation` table holds a *cumulative* `play_count` and only the
*most recent* `play_date`. That answers "how many times have I played this,
ever" and nothing else. There is no way to ask what was played in March,
because the information was never kept - each play overwrites the only date
there is and increments a running total.

So this reads that table once a day and records what changed. Two snapshots
either side of a day give the plays in that day; a year of them gives a year
of history. Nothing recovers the part before the first snapshot, which is
why this is the one item on the plan with a clock on it.

Three decisions worth stating, because each is a place the obvious version
is wrong:

**Keyed by track UUID, not `media_file.id`.** The id is an index artefact.
Re-import a file and it changes, taking every row that referenced it out of
alignment with the track it described. The UUID is on the file itself and
survives re-tagging, moving and a rebuilt database - which is the entire
reason it exists.

**Only changed counts are stored.** A full nightly capture of this library
is a few thousand rows, almost all identical to the night before. Most
nights a few dozen tracks are played. Storing every row would put a year at
close to a million; storing the changes puts it in the tens of thousands.
Reading a day's value means taking the most recent row at or before it,
which the index is shaped for.

**A count that goes down is an anomaly, not a negative number.** A
re-import or a reset can lower a counter. That is not minus four plays. It
is recorded as the fact it is, so a later statistic can decide what to do
about it rather than quietly averaging it in.

Navidrome's database is opened read-only, like everywhere else here.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, tzinfo, UTC
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import ledger, navidrome
from .config import settings

log = logging.getLogger("download_center.playcounts")

# The tag Navidrome derives a track's persistent id from. Stored parsed in
# media_file.tags, so it can be read in the same query.
UUID_TAG = "$.navidrome_uuid[0].value"



def zone() -> tzinfo:
    """Which midnight closes a listening day.

    Configured rather than taken from the container's TZ, which is Etc/UTC.
    For a listener on the US east coast that would put the boundary at 8pm
    and split every evening across two reported days.

    The fallback is `datetime.UTC`, not `ZoneInfo("UTC")`. ZoneInfo needs a
    time zone database - the system one, or the `tzdata` package - and on a
    machine with neither, looking up "UTC" fails exactly like any other name.
    A fallback that can raise the error it is catching is not a fallback.
    """
    name = settings.play_day_timezone or "UTC"
    if name.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone %r; falling back to UTC", name)
        return UTC


def today() -> str:
    """The date it is now, where the listener is."""
    return datetime.now(zone()).strftime("%Y-%m-%d")


def last_complete_day() -> str:
    """The most recent day that has actually finished.

    A snapshot records a cumulative total at the moment it runs, so the only
    day it can describe in full is the one before it. Labelling it with
    today's date was an off-by-one that attributed every delta a day late.

    Yesterday, always - not "yesterday if it is still early". That version
    had an edge the loop fell straight into: a restart at four in the
    afternoon wrote a *partial* reading of today under today's label, and
    because the day was then marked done, the run after midnight skipped it.
    The day was left permanently half-closed and nothing said so. Aiming at
    yesterday means the target only changes when a day genuinely ends.
    """
    return (datetime.now(zone()) - timedelta(days=1)).strftime("%Y-%m-%d")


# --- reading what Navidrome currently believes -----------------------------

def _current(connection: sqlite3.Connection) -> tuple[dict, int]:
    """Every non-zero play count, keyed by (track uuid, user).

    Returns the counts and how many rows had to be dropped for having no
    UUID - a number worth watching rather than hiding, because a track
    without one cannot be followed across a re-import and its history would
    silently restart.
    """
    # Joined to `folder`, not just filtered on `media_file.missing`. When a
    # directory vanishes Navidrome marks the *folder* row missing and leaves
    # the rows beneath it untouched, so a query that trusts `mf.missing`
    # alone counts tracks whose files were deleted months ago. On this
    # library that is 49 tracks left behind by the migration - their plays
    # are real history, but of tracks that no longer exist, and carrying
    # them forward for ever would quietly inflate every later statistic.
    rows = connection.execute(f"""
        select a.user_id,
               u.user_name,
               json_extract(mf.tags, '{UUID_TAG}') as track_uuid,
               a.play_count,
               a.play_date
          from annotation a
          join media_file mf on mf.id = a.item_id
          join folder f on f.id = mf.folder_id
          join user u on u.id = a.user_id
         where a.item_type = 'media_file'
           and a.play_count > 0
           and mf.missing = 0
           and f.missing = 0
    """).fetchall()

    counts: dict[tuple[str, str], dict[str, Any]] = {}
    unidentifiable = 0
    for user_id, username, track_uuid, play_count, play_date in rows:
        if not track_uuid:
            unidentifiable += 1
            continue
        counts[(track_uuid, user_id)] = {
            "username": username,
            "play_count": play_count or 0,
            "play_date": play_date,
        }
    return counts, unidentifiable


def _last_known() -> dict[tuple[str, str], int]:
    """The most recent stored count for every track and user.

    One row per pair, which is what makes "only store changes" readable: the
    comparison is against the last thing written, whenever that was.
    """
    rows = ledger.connection().execute("""
        select track_uuid, user_id, play_count
          from play_snapshot
         where (track_uuid, user_id, taken_on) in (
                   select track_uuid, user_id, max(taken_on)
                     from play_snapshot
                    group by track_uuid, user_id)
    """).fetchall()
    return {(track_uuid, user_id): count for track_uuid, user_id, count in rows}


# --- taking one -------------------------------------------------------------

def take(when: str | None = None) -> dict[str, Any]:
    """Record every play count that has changed since the last snapshot.

    Idempotent for a given day: the primary key is (day, track, user), so
    running it twice replaces rather than duplicates.
    """
    day = when or last_complete_day()
    try:
        source = navidrome.open_db()
    except navidrome.Unavailable as exc:
        log.warning("no play snapshot today: %s", exc)
        return {"taken": False, "reason": str(exc)}

    with source:
        current, unidentifiable = _current(source)

    previous = _last_known()
    baseline = not previous

    changed = []
    anomalies = []
    for key, row in current.items():
        was = previous.get(key)
        if was is not None and was == row["play_count"]:
            continue
        if was is not None and row["play_count"] < was:
            anomalies.append((day, key[0], key[1], was, row["play_count"]))
        changed.append((day, key[0], key[1], row["username"],
                        row["play_count"], row["play_date"]))

    store = ledger.connection()
    with ledger._lock:
        store.executemany(
            "INSERT OR REPLACE INTO play_snapshot"
            " (taken_on, track_uuid, user_id, username, play_count, play_date)"
            " VALUES (?, ?, ?, ?, ?, ?)", changed)
        store.executemany(
            "INSERT OR REPLACE INTO play_anomaly"
            " (noticed_on, track_uuid, user_id, was, became)"
            " VALUES (?, ?, ?, ?, ?)", anomalies)
        store.commit()

    result = {
        "taken": True,
        "day": day,
        # The first run records everything and means nothing: there is no
        # earlier snapshot to subtract from. Real data starts tomorrow.
        "baseline": baseline,
        "tracked": len(current),
        "changed": len(changed),
        "anomalies": len(anomalies),
        "without_uuid": unidentifiable,
    }
    log.info("play snapshot %s: %d changed of %d tracked%s%s", day,
             len(changed), len(current),
             " (baseline)" if baseline else "",
             f", {len(anomalies)} anomal{'y' if len(anomalies) == 1 else 'ies'}"
             if anomalies else "")
    return result


def taken_on(day: str) -> bool:
    """Whether a snapshot already exists for that day."""
    row = ledger.connection().execute(
        "SELECT 1 FROM play_snapshot WHERE taken_on = ? LIMIT 1",
        (day,)).fetchone()
    return row is not None


def status() -> dict[str, Any]:
    """Enough to tell whether this is working, before there is anything to
    show for it. Statistics need weeks; this needs to be checkable tonight.

    Reports both halves of the record. Snapshots are what this app collects
    from now on; imported rows are the history from before it started, and
    leaving them out of the status made 41,000 plays look like nothing was
    there.
    """
    store = ledger.connection()

    def one(sql: str) -> Any:
        return store.execute(sql).fetchone()[0]

    imported = store.execute(
        "SELECT source, COUNT(*), SUM(plays), MIN(day), MAX(day)"
        "  FROM play_imported GROUP BY source").fetchall()

    # The day the loop is aiming at. `today` is never the answer - a day
    # cannot be summarised until it has finished.
    wanted = last_complete_day()
    return {
        "snapshots": {
            "days": one("SELECT COUNT(DISTINCT taken_on) FROM play_snapshot"),
            "rows": one("SELECT COUNT(*) FROM play_snapshot"),
            "first_day": one("SELECT MIN(taken_on) FROM play_snapshot"),
            "last_day": one("SELECT MAX(taken_on) FROM play_snapshot"),
            "anomalies": one("SELECT COUNT(*) FROM play_anomaly"),
        },
        "imported": [
            {"source": source, "rows": rows, "plays": plays,
             "first_day": first, "last_day": last}
            for source, rows, plays, first, last in imported
        ],
        # The question worth asking of a nightly job: is it up to date?
        "up_to_date": taken_on(wanted),
        "awaiting": wanted,
    }


# --- reading it back --------------------------------------------------------

def plays_between(start: str, end: str,
                  user_id: str | None = None) -> list[dict[str, Any]]:
    """Plays per track over a date range, as deltas between snapshots.

    The value on a given day is the most recent snapshot at or before it,
    because only changes are stored. A count that fell is reported as zero
    plays rather than a negative number; the drop itself is in play_anomaly.
    """
    def value_at(day: str) -> dict[tuple[str, str], int]:
        rows = ledger.connection().execute("""
            select s.track_uuid, s.user_id, s.play_count
              from play_snapshot s
              join (select track_uuid, user_id, max(taken_on) as taken_on
                      from play_snapshot
                     where taken_on <= ?
                     group by track_uuid, user_id) latest
                on latest.track_uuid = s.track_uuid
               and latest.user_id = s.user_id
               and latest.taken_on = s.taken_on
        """, (day,)).fetchall()
        return {(t, u): c for t, u, c in rows}

    # A snapshot labelled D holds the total at the *end* of D, so the
    # opening balance for the range is the end of the day before it.
    before = (datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC)
              - timedelta(days=1)).strftime("%Y-%m-%d")
    opening, closing = value_at(before), value_at(end)

    names = dict(ledger.connection().execute(
        "SELECT user_id, username FROM play_snapshot GROUP BY user_id"))

    totals: dict[tuple[str, str], int] = {}
    for key, finished in closing.items():
        if user_id is not None and key[1] != user_id:
            continue
        started = opening.get(key, 0)
        if finished > started:
            totals[key] = finished - started

    # Days before the snapshots began, imported from Last.fm. Added rather
    # than merged: the two sources cover disjoint periods by construction -
    # the import stops the day snapshots start - so nothing is counted twice.
    imported = ledger.connection().execute("""
        select track_uuid, user_id, username, sum(plays)
          from play_imported
         where day between ? and ?
         group by track_uuid, user_id, username
    """, (start, end)).fetchall()
    for track_uuid, who, username, plays in imported:
        if user_id is not None and who != user_id:
            continue
        names.setdefault(who, username)
        totals[(track_uuid, who)] = totals.get((track_uuid, who), 0) + plays

    out = [{"track_uuid": track_uuid, "user_id": who,
            "username": names.get(who), "plays": plays}
           for (track_uuid, who), plays in totals.items()]
    out.sort(key=lambda row: row["plays"], reverse=True)
    return out
