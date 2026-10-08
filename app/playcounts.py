"""Snapshots of what has been played, so history stops being lost.

Navidrome's `annotation` table holds a *cumulative* `play_count` and only the
*most recent* `play_date`. That answers "how many times have I played this,
ever" and nothing else. There is no way to ask what was played in March,
because the information was never kept - each play overwrites the only date
there is and increments a running total.

So this reads that table every five minutes and records what changed. The
rise between two readings is the plays between them, and the `play_date`
beside a rise of one is that play's exact time; a year of them gives a year
of history. (Until 2026-09-26 it read once a day, and those rows carry a
bare date.) Nothing recovers the part before the first snapshot, which is
why this is the one item on the plan with a clock on it.

Three decisions worth stating, because each is a place the obvious version
is wrong:

**Keyed by track UUID, not `media_file.id`.** The id is an index artefact.
Re-import a file and it changes, taking every row that referenced it out of
alignment with the track it described. The UUID is on the file itself and
survives re-tagging, moving and a rebuilt database - which is the entire
reason it exists.

**Only changed counts are stored.** A full capture of this library is a
few thousand rows, almost all identical to the reading before - five
minutes apart, usually nothing changed at all. Storing every row would put
a year in the hundreds of millions; storing the changes puts it at about
one row per play.
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
import os
import sqlite3
import threading
from datetime import datetime, timedelta, tzinfo, UTC
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import memo, navidrome, store
from .config import settings

log = logging.getLogger("navidrome_companion.playcounts")

# The tag Navidrome derives a track's persistent id from. Stored parsed in
# media_file.tags, so it can be read in the same query.
UUID_TAG = "$.navidrome_uuid[0].value"

# A track can carry more than one genre; only the first is read, the same
# choice already made for every other multi-valued tag this app reads.
GENRE_TAG = "$.genre[0].value"



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


def now_stamp() -> str:
    """The moment a reading is taken, to the second, in UTC.

    UTC because this labels machinery rather than listening: it has to
    order readings, and a local stamp goes backwards for an hour every
    autumn. Where the play *happened* is a different question, answered by
    `local_stamp` below.

    Sorts correctly beside the plain `YYYY-MM-DD` labels written while this
    ran nightly: a date is a prefix of any timestamp on the same day, so it
    sorts first, and still sorts before the next day. The two coexist in one
    column with no migration.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def local_stamp(raw: str | None) -> str:
    """Navidrome's play_date, in the listener's own time zone.

    Navidrome writes UTC, to the nanosecond: `2026-09-25 17:09:46.018+00:00`.
    Converted here because every question asked of it is a local one - an
    evening's listening on the US east coast is the next day in UTC, and
    bucketing the raw value puts the last four hours of every month into
    the month after it.

    Returns "" for anything unparseable, which the caller reads as "no time
    for this play" and falls back to when the reading was taken.
    """
    if not raw:
        return ""
    try:
        moment = datetime.fromisoformat(str(raw).strip())
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(zone()).isoformat(timespec="seconds")


def _instant(raw: str | None) -> datetime | None:
    """A stamp from either side - Navidrome's play_date, a reading's own
    stamp, a bare date from the nightly era - as a moment, or None."""
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw).strip())
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def baseline_stamp() -> str | None:
    """When collection began: the stamp of the very first reading.

    Rows from that reading are the counters as they stood when collection
    began - a lifetime of listening, not plays. Any track's first row from a
    later reading is new listening and counts from zero: `take` stores only
    counts above zero, so a track first played after collection began first
    appears at 1. Recorded by `take` in play_collection; without that row
    (history written by hand), the earliest stored reading stands in.
    """
    db = store.connection()
    row = db.execute("SELECT began FROM play_collection").fetchone()
    if row is None:
        row = db.execute("SELECT MIN(taken_on) FROM play_snapshot").fetchone()
    return row[0] if row and row[0] else None


def next_day(day: str) -> str:
    """The day after, so a timestamp can be bounded by a date.

    Every reading taken on day D satisfies `D <= taken_on < D+1`, whether it
    is stored as a bare date or as a timestamp. A query that instead asked
    for `taken_on <= D` would exclude every reading actually taken that day,
    because "D" sorts before "DT10:00".
    """
    return (datetime.strptime(day, "%Y-%m-%d").date()
            + timedelta(days=1)).strftime("%Y-%m-%d")



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
        # Two files carrying one UUID each have a count. Taking whichever
        # row came last, in no fixed order, let 5 and 2 alternate between
        # readings and every rise back to 5 counted as three plays. The
        # higher one, every time.
        known = counts.get((track_uuid, user_id))
        if known is not None and known["play_count"] >= (play_count or 0):
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
    rows = store.connection().execute("""
        select track_uuid, user_id, play_count
          from play_snapshot
         where (track_uuid, user_id, taken_on) in (
                   select track_uuid, user_id, max(taken_on)
                     from play_snapshot
                    group by track_uuid, user_id)
    """).fetchall()
    return {(track_uuid, user_id): count for track_uuid, user_id, count in rows}


# --- taking one -------------------------------------------------------------

# One reading at a time. A forced reading (POST /api/playcounts/snapshot)
# and the five-minute timer could otherwise run together: both compared the
# counters against the same previous reading, wrote the same changes twice,
# and the run log counted them twice.
_take_lock = threading.Lock()


def take(when: str | None = None) -> dict[str, Any]:
    """Record every play count that has changed since the last reading.

    Serialised by `_take_lock`; see `_take` for what a reading does."""
    with _take_lock:
        return _take(when)


def _take(when: str | None = None) -> dict[str, Any]:
    """Record every play count that has changed since the last reading.

    Taken every few minutes rather than nightly. Navidrome stores, beside
    the cumulative count, the moment of the *most recent* play - so when a
    track's count rises by one between two readings, that moment is the
    exact time of that play. Reading often enough that the usual rise is
    one turns a running total into a log of individual plays, which is what
    a listening history has to be to answer anything about sessions, time
    of day, or a particular evening.

    A rise of more than one means the same track was played twice inside
    one interval; those plays share the last one's timestamp. At a few
    minutes' cadence that smear is bounded by the interval.

    Idempotent for a given reading: the primary key is (taken_on, track,
    user), so running it twice with the same stamp replaces rather than
    duplicates.
    """
    at = when or now_stamp()
    # The local day this reading belongs to. Taken from `when` when a caller
    # names one, so a reading deliberately labelled with a past day is
    # logged against that day rather than against the day it was typed.
    run_day = when[:10] if when else today()
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
            # Noticed on a day, not at a reading: a counter that fell is
            # one event however many times the next readings re-observe it,
            # and the primary key collapses them.
            anomalies.append((run_day, key[0], key[1], was,
                              row["play_count"]))
        changed.append((at, key[0], key[1], row["username"],
                        row["play_count"], row["play_date"]))

    db = store.connection()
    with store._lock:
        # Only the first reading ever lands; every later one is ignored.
        db.execute("INSERT OR IGNORE INTO play_collection (id, began)"
                   " VALUES (1, ?)", (at,))
        db.executemany(
            "INSERT OR REPLACE INTO play_snapshot"
            " (taken_on, track_uuid, user_id, username, play_count, play_date)"
            " VALUES (?, ?, ?, ?, ?, ?)", changed)
        db.executemany(
            "INSERT OR REPLACE INTO play_anomaly"
            " (noticed_on, track_uuid, user_id, was, became)"
            " VALUES (?, ?, ?, ?, ?)", anomalies)
        # Recorded whether or not anything changed. A day when nobody
        # listened produces no snapshot rows, and without this the day never
        # counts as done - so the job repeats it every half hour and the
        # status never catches up.
        # One row per local day, rewritten by each reading within it. The
        # run log answers "is this collecting?", which is a question about
        # days; two hundred rows a day would answer it no better.
        db.execute(
            "INSERT INTO play_snapshot_run"
            " (day, taken_at, tracked, changed, anomalies)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(day) DO UPDATE SET"
            "   taken_at = excluded.taken_at,"
            "   tracked = excluded.tracked,"
            "   changed = changed + excluded.changed,"
            "   anomalies = anomalies + excluded.anomalies",
            (run_day, datetime.now(UTC).isoformat(timespec="seconds"),
             len(current), len(changed), len(anomalies)))
        db.commit()

    result = {
        "taken": True,
        "at": at,
        "day": run_day,
        # The first run records everything and means nothing: there is no
        # earlier snapshot to subtract from. Real data starts tomorrow.
        "baseline": baseline,
        "tracked": len(current),
        "changed": len(changed),
        "anomalies": len(anomalies),
        "without_uuid": unidentifiable,
        # Whose history this reading moved, so their statistics can be
        # recomputed now rather than on their next visit.
        "users": sorted({row[2] for row in changed}),
    }
    log.info("play snapshot %s: %d changed of %d tracked%s%s", at,
             len(changed), len(current),
             " (baseline)" if baseline else "",
             f", {len(anomalies)} anomal{'y' if len(anomalies) == 1 else 'ies'}"
             if anomalies else "")
    return result



# Six missed readings at the cadence in main.py. Long enough that a slow
# run or a restart is not an alarm, short enough that a collector which
# died this morning is not still reported healthy this evening.
STALE_AFTER_MINUTES = 30


def last_reading() -> str:
    """When the counts were last read, however long ago that was."""
    row = store.connection().execute(
        "SELECT MAX(taken_at) FROM play_snapshot_run").fetchone()
    return row[0] if row and row[0] else ""


def read_recently() -> bool:
    """Whether the collector is alive.

    Asked of the run log rather than of the rows: a quiet hour writes no
    snapshot rows at all, and judging by those would call a working
    collector dead every time nobody was listening.
    """
    when = last_reading()
    if not when:
        return False
    try:
        last = datetime.fromisoformat(when)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - last).total_seconds()
    return age <= STALE_AFTER_MINUTES * 60


def status() -> dict[str, Any]:
    """Enough to tell whether this is working, before there is anything to
    show for it. Statistics need weeks; this needs to be checkable tonight.

    Reports both halves of the record. Snapshots are what this app collects
    from now on; imported rows are the history from before it started, and
    leaving them out of the status made 41,000 plays look like nothing was
    there.
    """
    db = store.connection()

    def one(sql: str) -> Any:
        return db.execute(sql).fetchone()[0]

    imported = db.execute(
        "SELECT source, COUNT(*), SUM(plays),"
        "       substr(MIN(played_at), 1, 10), substr(MAX(played_at), 1, 10)"
        "  FROM play_imported GROUP BY source").fetchall()

    # Days, from readings: there are hundreds of readings a day now, and
    # "how long has this been collecting" is still a question about days.
    return {
        "snapshots": {
            "days": one("SELECT COUNT(DISTINCT substr(taken_on, 1, 10))"
                        "  FROM play_snapshot"),
            "rows": one("SELECT COUNT(*) FROM play_snapshot"),
            "first_day": one("SELECT substr(MIN(taken_on), 1, 10)"
                             "  FROM play_snapshot"),
            "last_day": one("SELECT substr(MAX(taken_on), 1, 10)"
                            "  FROM play_snapshot"),
            "anomalies": one("SELECT COUNT(*) FROM play_anomaly"),
            # Days the job actually ran, which is not the same as days that
            # produced rows.
            "days_run": one("SELECT COUNT(*) FROM play_snapshot_run"),
            "last_run": one("SELECT MAX(day) FROM play_snapshot_run"),
        },
        "imported": [
            {"source": source, "rows": rows, "plays": plays,
             "first_day": first, "last_day": last}
            for source, rows, plays, first, last in imported
        ],
        # What "up to date" means changed with the cadence. While this ran
        # nightly the question was whether yesterday had been captured; now
        # that it reads every few minutes, the only way to be behind is to
        # have stopped, and the answer is how long ago the last reading was.
        "up_to_date": read_recently(),
        "last_reading": last_reading(),
        # How far back the blind spot reaches, if there is one.
        "awaiting": last_reading() or "the first reading",
    }


# --- reading it back --------------------------------------------------------

def history_version() -> tuple:
    """Changes whenever the listening history does, and costs next to nothing.

    Derived from the rows rather than bumped by the writers, so a hand edit
    in the sqlite shell (the documented undo for the Last.fm import is one)
    moves it too. `INSERT OR REPLACE` deletes and re-inserts, so a rewritten
    reading still raises the highest rowid; a delete lowers the count.
    """
    db = store.connection()
    snapshots = db.execute(
        "SELECT MAX(rowid), COUNT(*) FROM play_snapshot").fetchone()
    imported = db.execute(
        "SELECT MAX(rowid), COUNT(*) FROM play_imported").fetchone()
    return (store.generation, tuple(snapshots), tuple(imported))


# What Navidrome calls each track, read in one pass and kept until its
# database file changes. Looking titles up per call scanned the whole
# media_file table - JSON tags parsed on every row - once for every 500
# UUIDs asked about, which was most of what Home spent its time on.
_index_lock = threading.Lock()
_index: dict[str, Any] = {"stamp": None, "version": 0, "tracks": {}}


def _db_stamp() -> tuple | None:
    """Navidrome's database files as the filesystem sees them.

    The write-ahead log is included because that is where Navidrome's writes
    land first; the main file can go unchanged for a long time.
    """
    path = settings.navidrome_db
    stamp = [str(path)]
    for name in (str(path), f"{path}-wal"):
        try:
            info = os.stat(name)
        except OSError:
            stamp.append(None)
            continue
        stamp.append((info.st_mtime_ns, info.st_size))
    return tuple(stamp) if stamp[1] is not None else None


def _library_stamp() -> tuple | None:
    """What the track index depends on: media_file, not the database file.

    Keyed on the files' mtime it was rebuilt - a full media_file scan with
    three JSON lookups a row - on nearly every request while music played,
    because Navidrome writes to its database on every play.
    """
    try:
        connection = navidrome.open_db()
    except navidrome.Unavailable:
        return None
    try:
        with connection:
            stamp = navidrome.library_stamp(connection)
    except sqlite3.Error:
        return None
    # No updated_at to go on: the database files, as before.
    return stamp if stamp is not None else _db_stamp()


def track_index() -> tuple[int, dict[str, dict[str, Any]]]:
    """(version, track UUID -> what it is called), from Navidrome's index.

    Rebuilt only when Navidrome's tracks have changed (`_library_stamp`),
    and the version moves only when the rebuild came out different. Navidrome writes to its
    database on every play, so a version tied to the file alone would throw
    away every cached statistic each time a song finished, for a track list
    that had not changed at all.

    A UUID with no row here is a track that has since left the library.
    """
    stamp = _library_stamp()
    with _index_lock:
        if stamp is not None and stamp == _index["stamp"]:
            return _index["version"], _index["tracks"]
        tracks = _read_index()
        if tracks is None:
            # Unavailable is not "the library is empty". Not remembered, so
            # the next call tries again rather than serving nothing for ever.
            return -1, {}
        if tracks != _index["tracks"]:
            _index["version"] += 1
            _index["tracks"] = tracks
        _index["stamp"] = stamp
        return _index["version"], _index["tracks"]


def _read_index() -> dict[str, dict[str, Any]] | None:
    try:
        connection = navidrome.open_db()
    except navidrome.Unavailable as exc:
        log.warning("cannot name tracks: %s", exc)
        return None
    found: dict[str, dict[str, Any]] = {}
    try:
        with connection:
            live = navidrome.live_clause(connection)
            rows = connection.execute(f"""
                select json_extract(mf.tags, '{UUID_TAG}') as uuid,
                       mf.title, mf.artist, mf.album, mf.duration,
                       coalesce(nullif(mf.album_artist, ''), mf.artist),
                       mf.id,
                       json_extract(mf.tags, '{GENRE_TAG}') as genre,
                       ({live}) as live
                  from media_file mf
                 where json_extract(mf.tags, '{UUID_TAG}') is not null
            """).fetchall()
    except sqlite3.Error as exc:
        log.warning("cannot name tracks: %s", exc)
        return None
    finally:
        connection.close()
    for (uuid, title, artist, album, duration, album_artist, media_id,
         genre, live) in rows:
        # A UUID on two files - a live one and a copy left behind as
        # missing - names the one still there.
        if uuid in found and not live:
            continue
        found[uuid] = {"title": title or "", "artist": artist or "",
                       # The library's own id for the file, which is what
                       # the cover endpoint is asked for.
                       "id": media_id,
                       "album": album or "",
                       # Carried for the callers that turn plays into hours.
                       "duration": duration or 0.0,
                       # Whose album this is, not who is credited on this
                       # particular track - a compilation's tracks should
                       # still group under one album.
                       "album_artist": album_artist or "",
                       "genre": genre or ""}
    return found


def increments(user_id: str) -> list[tuple[str, str, int]]:
    """Every play this person made, as (when, track uuid, how many).

    Shared between callers until the history changes; read-only."""
    return memo.cached(("increments", user_id), history_version(),
                       lambda: _read_increments(user_id))


def _read_increments(user_id: str) -> list[tuple[str, str, int]]:
    """Every play this person made, as (when, track uuid, how many).

    Both sources flattened into the same shape, so everything downstream is
    a sum over one list rather than two special cases.

    `when` is the play's own time wherever it is known. Navidrome stores
    the moment of a track's most recent play beside its running total, so a
    reading that catches the count rising by one carries that play's exact
    timestamp - which is the whole reason the counts are read every few
    minutes rather than nightly. Where it is missing or unreadable the
    reading's own time stands in, and for rows written while this ran
    nightly that is a bare date. All three sort and bucket alike.
    """
    db = store.connection()
    with store._lock:
        rows = db.execute(
            "select track_uuid, taken_on, play_count, play_date"
            "  from play_snapshot where user_id = ?"
            " order by track_uuid, taken_on",
            (user_id,)).fetchall()
        imported = db.execute(
            "select played_at, track_uuid, plays from play_imported"
            " where user_id = ?", (user_id,)).fetchall()

    first = baseline_stamp()
    began = _instant(first)
    plays: list[tuple[str, str, int]] = []
    previous_track = None
    previous_count = 0
    for track_uuid, taken_on, count, play_date in rows:
        if track_uuid != previous_track:
            previous_track = track_uuid
            if taken_on == first:
                # The counter as it stood when collection began: a
                # lifetime, not listening.
                previous_count = count
                continue
            # First seen later: `take` stores only counts above zero, so
            # this row is the track's first plays. Treating every first row
            # as a baseline lost the first play of everything newly played
            # - exactly the music being discovered.
            previous_count = 0
        last_played = _instant(play_date)
        if (count > previous_count and began is not None
                and last_played is not None and last_played < began):
            # A rise whose latest play happened before collection began
            # holds no play the collector could have seen: a lifetime
            # counter coming back into view. A track missing, set aside or
            # not yet stamped at the first reading had its whole history
            # counted as new plays when it reappeared, dated months back,
            # and counted again by any Last.fm import; so did one of two
            # files sharing a UUID going missing and coming back.
            previous_count = count
            continue
        if count > previous_count:
            # A rise of more than one means the same track was played
            # twice inside one interval. Only the last of them has a
            # recorded time, so they share it; at a five-minute cadence
            # that smear is bounded by five minutes.
            # The reading's own stamp is UTC, so it is converted the same
            # way; bucketed raw, a reading at 01:00 UTC put a New York
            # evening's play on the next day. A bare date from the nightly
            # era has no time to convert and stands as it is.
            when = (local_stamp(play_date)
                    or (local_stamp(taken_on) if "T" in taken_on else taken_on))
            plays.append((when, track_uuid, count - previous_count))
        previous_count = count

    plays.extend((day, track_uuid, n) for day, track_uuid, n in imported if n)
    return plays


def plays_between(start: str, end: str,
                  user_id: str | None = None) -> list[dict[str, Any]]:
    """Plays per track over a range of local days, most played first.

    Shared between callers until the history changes - the Listening panel
    asks the same range for its tracks, albums and genres - so the rows are
    read-only. A caller that wants to add to one copies it first.
    """
    return memo.cached(("plays_between", start, end, user_id),
                       history_version(),
                       lambda: _plays_between(start, end, user_id))


def _plays_between(start: str, end: str,
                   user_id: str | None = None) -> list[dict[str, Any]]:
    """Summed from `increments`, the per-play list Home is built from, by
    the local day each play happened on.

    This used to subtract the count at the start of the range from the count
    at the end, bucketed by the UTC time of the *reading*, while Home
    bucketed each play by its own local time. A play at 20:58 in New York
    landed on different days in the two halves of one page, and "Today"
    stopped moving at 8pm. One list, one clock.
    """
    db = store.connection()
    if user_id is not None:
        users = [user_id]
    else:
        users = [row[0] for row in db.execute(
            "SELECT user_id FROM play_snapshot UNION"
            " SELECT user_id FROM play_imported")]
    names = dict(db.execute(
        "SELECT user_id, username FROM play_snapshot GROUP BY user_id"))
    for who, username in db.execute(
            "SELECT user_id, username FROM play_imported GROUP BY user_id"):
        names.setdefault(who, username)

    totals: dict[tuple[str, str], int] = {}
    for who in users:
        for when, track_uuid, plays in increments(who):
            # A bare date, a timestamp and a local stamp all start with the
            # day they belong to.
            if start <= when[:10] <= end:
                key = (track_uuid, who)
                totals[key] = totals.get(key, 0) + plays

    out = [{"track_uuid": track_uuid, "user_id": who,
            "username": names.get(who), "plays": plays}
           for (track_uuid, who), plays in totals.items()]
    out.sort(key=lambda row: row["plays"], reverse=True)
    return out


def _titles(uuids: list[str]) -> dict[str, dict[str, Any]]:
    """Track UUID -> what it is called, from Navidrome's index.

    Snapshots are keyed by UUID and nothing else, which is the point - the
    identity survives retagging, moving and a rebuilt database - but it makes
    the stored rows unreadable on their own. A UUID with no row here is a
    track that has since left the library; its plays still happened, so it is
    reported rather than dropped.
    """
    if not uuids:
        return {}
    _version, tracks = track_index()
    return {uuid: tracks[uuid] for uuid in uuids if uuid in tracks}


def top_tracks(start: str, end: str, user_id: str,
               limit: int = 25) -> list[dict[str, Any]]:
    """The most played tracks over a range, named and ready to show."""
    # Copied: plays_between's rows are shared with every other caller.
    rows = [dict(row) for row in plays_between(start, end, user_id)[:limit]]
    names = _titles([row["track_uuid"] for row in rows])
    for row in rows:
        known = names.get(row["track_uuid"])
        row["title"] = known["title"] if known else "(no longer in the library)"
        row["artist"] = known["artist"] if known else ""
        row["album"] = known["album"] if known else ""
        row["known"] = known is not None
    return rows


def top_albums(start: str, end: str, user_id: str,
               limit: int = 10) -> list[dict[str, Any]]:
    """The most played albums over a range, plays summed across their tracks.

    Grouped by artist and album together, not album alone - two different
    artists' "Greatest Hits" are not the same record.
    """
    rows = plays_between(start, end, user_id)
    names = _titles([row["track_uuid"] for row in rows])

    totals: dict[tuple[str, str], int] = {}
    for row in rows:
        known = names.get(row["track_uuid"])
        if not known or not known["album"]:
            # A track that left the library, or was never tagged with an
            # album, has nothing to group it with - it is in top_tracks
            # already, and a row here would say "by nobody, called nothing".
            continue
        key = (known["album_artist"], known["album"])
        totals[key] = totals.get(key, 0) + row["plays"]

    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    return [{"artist": artist, "album": album, "plays": plays}
            for (artist, album), plays in ranked[:limit]]


def _genres(uuids: list[str]) -> dict[str, str]:
    """Track UUID -> its first genre tag, from the same index `_titles`
    reads. A UUID with no entry is untagged or has left the library."""
    if not uuids:
        return {}
    _version, tracks = track_index()
    return {uuid: tracks[uuid]["genre"] for uuid in uuids
            if uuid in tracks and tracks[uuid]["genre"]}


def top_genres(start: str, end: str, user_id: str,
               limit: int = 10) -> list[dict[str, Any]]:
    """The most played genres over a range. Untagged tracks are left out,
    the same choice `top_albums` makes for untagged albums."""
    rows = plays_between(start, end, user_id)
    genres = _genres([row["track_uuid"] for row in rows])

    totals: dict[str, int] = {}
    for row in rows:
        genre = genres.get(row["track_uuid"])
        if genre:
            totals[genre] = totals.get(genre, 0) + row["plays"]

    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    return [{"genre": genre, "plays": plays} for genre, plays in ranked[:limit]]


def coverage(user_id: str) -> dict[str, Any]:
    """What has been captured *for one person*.

    `status()` answers for the installation, which is right for a health
    check and wrong for a panel: the imported Last.fm history belongs to
    whoever listened to it, and 41,203 plays shown to an account that has
    never played anything is both confusing and somebody else's business.

    The job facts - whether the collector is alive and when it last read -
    stay global, because they are about the collector rather than the
    collection.
    """
    db = store.connection()
    return {
        **memo.cached(("coverage", user_id), history_version(),
                      lambda: _person_coverage(user_id)),
        # About the job, not the person - and about the clock, so never
        # cached: the last reading moves every few minutes even when nobody
        # is listening.
        "days_run": db.execute(
            "SELECT COUNT(*) FROM play_snapshot_run").fetchone()[0],
        "last_run": db.execute(
            "SELECT MAX(day) FROM play_snapshot_run").fetchone()[0],
        "up_to_date": read_recently(),
        "last_reading": last_reading(),
    }


def _person_coverage(user_id: str) -> dict[str, Any]:
    db = store.connection()
    imported, first, last = db.execute(
        "SELECT COALESCE(SUM(plays), 0),"
        "       substr(MIN(played_at), 1, 10), substr(MAX(played_at), 1, 10)"
        "  FROM play_imported WHERE user_id = ?", (user_id,)).fetchone()
    sources = [row[0] for row in db.execute(
        "SELECT DISTINCT source FROM play_imported WHERE user_id = ?",
        (user_id,)).fetchall()]
    # Days, not readings. There are a few hundred readings a day now, and
    # "how long has this been collecting for me" is a question about days.
    snapshot_days = db.execute(
        "SELECT COUNT(DISTINCT substr(taken_on, 1, 10)) FROM play_snapshot"
        " WHERE user_id = ?", (user_id,)).fetchone()[0]

    return {
        "imported_plays": imported,
        "imported_from": first,
        "imported_to": last,
        "imported_sources": sources,
        "snapshot_days": snapshot_days,
    }
