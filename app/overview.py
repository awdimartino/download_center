"""What the landing page shows: listening over time, which Navidrome cannot.

Navidrome keeps a cumulative play count and the single most recent play date.
That answers "how many times, ever" and nothing else - there is no way to ask
it what you listened to in March. `play_snapshot` exists because of that, and
this module is the reading end of it: the one thing this application knows
that the thing it sits beside does not.

Two sources, covering disjoint periods by construction:

  * `play_snapshot` - a nightly reading of the cumulative counter. Plays are
    the *increase* between one reading and the next, so a month's listening
    is the sum of those increases.
  * `play_imported` - listening from before the snapshots began, already
    stored as plays per day rather than as a running total.

A track's **first** reading is a baseline, never plays. It is whatever the
counter already said the day this started watching, and counting it would
put a lifetime of listening into whichever month the snapshots began.

An increase is attributed to the day of the *later* reading. With nightly
snapshots that smears a play by at most a day, and only across a month
boundary does it show at all.
"""

from __future__ import annotations

import collections
import logging
from datetime import UTC, datetime
from typing import Any

from . import navidrome, playcounts, store

log = logging.getLogger("download_center.overview")

# How far the chart looks back. Long enough that a year of listening has
# shape, short enough that the bars stay wide enough to read.
MONTHS = 24

# How many artists the list names. Past this it stops being a glance.
TOP_ARTISTS = 6


def _increments(user_id: str) -> list[tuple[str, str, int]]:
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
            "select day, track_uuid, plays from play_imported"
            " where user_id = ?", (user_id,)).fetchall()

    plays: list[tuple[str, str, int]] = []
    previous_track = None
    previous_count = 0
    for track_uuid, taken_on, count, play_date in rows:
        if track_uuid != previous_track:
            # First reading of this track: a baseline, not listening.
            previous_track, previous_count = track_uuid, count
            continue
        if count > previous_count:
            # A rise of more than one means the same track was played
            # twice inside one interval. Only the last of them has a
            # recorded time, so they share it; at a five-minute cadence
            # that smear is bounded by five minutes.
            when = playcounts.local_stamp(play_date) or taken_on
            plays.append((when, track_uuid, count - previous_count))
        previous_count = count

    plays.extend((day, track_uuid, n) for day, track_uuid, n in imported if n)
    return plays


def _months_back(count: int) -> list[str]:
    """The last `count` months as YYYY-MM, oldest first, none skipped.

    Built from the calendar rather than from the data, so a month nobody
    listened in is a gap in the line instead of vanishing and making the
    months either side look adjacent.
    """
    now = datetime.now(UTC)
    months = []
    year, month = now.year, now.month
    for _ in range(count):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(months))


def listening(identity: navidrome.Identity) -> dict[str, Any]:
    """One pass over this person's listening, shaped for the landing page."""
    plays = _increments(identity.user_id)

    by_month: dict[str, int] = collections.Counter()
    for day, _track, n in plays:
        by_month[day[:7]] += n

    wanted = _months_back(MONTHS)
    series = [{"month": m, "plays": by_month.get(m, 0)} for m in wanted]

    # The last twelve months, for the artist list. All-time would be a list
    # of whoever somebody liked in 2019 and never change.
    since = wanted[-12] if len(wanted) >= 12 else wanted[0]
    recent: dict[str, int] = collections.Counter()
    for day, track_uuid, n in plays:
        if day[:7] >= since:
            recent[track_uuid] += n

    named = playcounts._titles(list(recent))
    by_artist: dict[str, int] = collections.Counter()
    for track_uuid, n in recent.items():
        known = named.get(track_uuid)
        if known and known["artist"]:
            by_artist[known["artist"]] += n

    this_month = by_month.get(wanted[-1], 0)
    last_month = by_month.get(wanted[-2], 0) if len(wanted) > 1 else 0

    # The calendar year is always inside the twelve-month window above, so
    # `named` already covers every track in it and this needs no second
    # lookup - it is a second pass over a list that is already in hand.
    year = datetime.now(UTC).year
    played_this_year: set[str] = set()
    plays_this_year = 0
    seconds_this_year = 0.0
    for day, track_uuid, n in plays:
        if not day.startswith(f"{year:04d}-"):
            continue
        plays_this_year += n
        played_this_year.add(track_uuid)
        known = named.get(track_uuid)
        if known:
            # Time listened is an estimate by construction: a play is
            # counted once the counter moves, and Navidrome moves it part
            # way through the track, not at the end.
            seconds_this_year += (known.get("duration") or 0.0) * n

    busiest = max(by_month.items(), key=lambda pair: pair[1],
                  default=("", 0))

    return {
        "months": series,
        "top_artists": [{"artist": a, "plays": n}
                        for a, n in by_artist.most_common(TOP_ARTISTS)],
        "total_plays": sum(by_month.values()),
        "this_month": this_month,
        "last_month": last_month,
        "tracked_since": min((day for day, _, _ in plays), default=""),
        "artists_heard": len(by_artist),
        "year": {"year": year, "plays": plays_this_year,
                 "tracks": len(played_this_year),
                 "seconds": round(seconds_this_year)},
        "busiest_month": {"month": busiest[0], "plays": busiest[1]},
    }


def collection(identity: navidrome.Identity) -> dict[str, Any]:
    """How much music there is. Counted, not grouped - this is a headline.

    Deliberately not `library.listing`, which walks every row to build an
    object per album. A landing page wants two integers.
    """
    allowed = [lib["id"] for lib in identity.libraries]
    if not allowed:
        return {"tracks": 0, "albums": 0, "available": False}
    try:
        connection = navidrome.open_db()
        with connection:
            live = navidrome.live_clause(connection, allowed)
            row = connection.execute(f"""
                select count(*),
                       count(distinct coalesce(mf.album_artist, mf.artist, '')
                             || char(31) || coalesce(mf.album, ''))
                  from media_file mf where {live}""").fetchone()
    except Exception as exc:
        log.warning("cannot count the collection: %s", exc)
        return {"tracks": 0, "albums": 0, "available": False}
    return {"tracks": row[0] or 0, "albums": row[1] or 0, "available": True}


def _month_name(month: str) -> str:
    """2025-09 -> September 2025. Unparseable months keep their digits."""
    try:
        return datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return month


def _hours(seconds: float) -> str:
    """Seconds as the coarsest unit that still says something.

    Nobody wants four decimal places of listening. Under an hour it reads
    in minutes, because "0 hours" on a new install is a worse headline than
    a small honest number.
    """
    if seconds >= 3600:
        hours = round(seconds / 3600)
        return f"{hours:,} hour" + ("" if hours == 1 else "s")
    minutes = max(1, round(seconds / 60))
    return f"{minutes:,} minute" + ("" if minutes == 1 else "s")


def highlights(heard: dict[str, Any], counted: dict[str, Any]) -> list[dict[str, str]]:
    """One-line facts, any of which can be the headline.

    Three parts rather than a sentence, so the page can set the number in
    large type and the words around it small - which is the whole reason
    the headline is worth having over another tile.

    Only facts that are actually true of this account: a fresh install has
    no listening, and "You have played 0 tracks" is a worse greeting than
    one about the size of the collection.
    """
    facts: list[dict[str, str]] = []
    year = heard.get("year") or {}
    artists = heard.get("top_artists") or []
    busiest = heard.get("busiest_month") or {}

    if year.get("tracks"):
        facts.append({"lead": "You've played", "tail": f"so far in {year['year']}",
                      "value": f"{year['tracks']:,} different track"
                               + ("" if year["tracks"] == 1 else "s")})
    if year.get("seconds"):
        facts.append({"lead": "That's", "value": _hours(year["seconds"]),
                      "tail": "of music this year"})
    if heard.get("this_month"):
        facts.append({"lead": "You've played",
                      "value": f"{heard['this_month']:,} track"
                               + ("" if heard["this_month"] == 1 else "s"),
                      "tail": "this month"})
    if artists:
        facts.append({"lead": "Your most-played artist is",
                      "value": artists[0]["artist"],
                      "tail": f"{artists[0]['plays']:,} plays in the last year"})
    if heard.get("artists_heard", 0) > 1:
        facts.append({"lead": "You've listened to",
                      "value": f"{heard['artists_heard']:,} artists",
                      "tail": "in the last twelve months"})
    if busiest.get("plays"):
        facts.append({"lead": "Your busiest month was",
                      "value": _month_name(busiest["month"]),
                      "tail": f"{busiest['plays']:,} plays"})
    if counted.get("tracks"):
        facts.append({"lead": "Your library holds",
                      "value": f"{counted['tracks']:,} tracks",
                      "tail": f"across {counted.get('albums', 0):,} albums"})
    return facts


def overview(identity: navidrome.Identity) -> dict[str, Any]:
    """Everything the landing page needs, in one request."""
    try:
        heard = listening(identity)
    except Exception as exc:
        log.warning("cannot read listening history: %s", exc)
        heard = {"months": [], "top_artists": [], "total_plays": 0,
                 "this_month": 0, "last_month": 0, "tracked_since": "",
                 "artists_heard": 0, "busiest_month": {"month": "", "plays": 0},
                 "year": {"year": datetime.now(UTC).year, "plays": 0,
                          "tracks": 0, "seconds": 0}}
    counted = collection(identity)
    return {
        "username": identity.username,
        "libraries": [lib["name"] for lib in identity.libraries],
        "collection": counted,
        "listening": heard,
        # All of them, not one: the browser picks, so the headline changes
        # on every visit without asking the server again.
        "highlights": highlights(heard, counted),
        "snapshots": _snapshot_health(),
    }


def _snapshot_health() -> dict[str, Any]:
    """Whether the thing that records listening is actually running.

    The one number on this page that is about the machinery rather than the
    music, and it earns its place: Navidrome keeps only a running total, so
    every interval this does not run is listening nobody can recover.
    """
    try:
        status = playcounts.status()
    except Exception as exc:
        log.warning("cannot read snapshot status: %s", exc)
        return {"up_to_date": None, "last_reading": ""}
    return {"up_to_date": bool(status.get("up_to_date")),
            "last_reading": status.get("last_reading") or ""}
