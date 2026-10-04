"""One-time import of Last.fm scrobbles, for the history snapshots cannot reach.

Snapshots start the night they are switched on. Everything before that is
gone as far as Navidrome is concerned - it keeps a cumulative count and one
date, so the shape of a year of listening was never recorded. Last.fm did
record it, per scrobble, with real timestamps.

**Why the matching is easier than it looks.** Last.fm stores the artist and
track name the scrobbler sent; it does not resolve them to a MusicBrainz
entity. Those strings came from the tags on these files. So both sides of
the match are our own metadata, which is a far smaller problem than matching
against somebody else's catalogue.

**Why it is still not certain.** A scrobble is text and nothing else. Two
tracks in the library can normalise to the same artist and title - a single
and its album appearance, a remaster beside the original. Where that
happens the scrobble is reported as ambiguous rather than assigned to
whichever row came back first, because a backfill that quietly matched sixty
per cent would poison every statistic built on it afterwards and never say
so.

Run by hand, per person, and never as a background job:

    python -m app.lastfm alex            # report only
    python -m app.lastfm alex --apply
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import json
import logging
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from . import navidrome, playcounts, store
from .config import settings

log = logging.getLogger("navidrome_companion.lastfm")

API = "https://ws.audioscrobbler.com/2.0/"
PAGE = 200
# Last.fm asks for no more than about five calls a second averaged over five
# minutes. One request every quarter second is well inside that and still
# fetches a year of listening in a couple of minutes.
PAUSE = 0.25
# Delays between attempts at one page. The final 0 is the last try: there is
# no point sleeping after it. A quarter of an hour of requests should not be
# thrown away by one bad minute at the other end.
RETRY_BACKOFF = (2, 5, 15, 30, 0)

SOURCE = "lastfm"

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")
_FEAT = re.compile(r"\s*[\(\[]?\s*(feat|ft|featuring|with)\.?\s+[^\)\]]*[\)\]]?",
                   re.I)
_AMPERSAND = re.compile(r"\s*[&+]\s*")


def normalise(text: str) -> str:
    """The form both sides are compared in.

    Lowercase, featured artists dropped, punctuation removed - the same shape
    as matcher.normalise. A scrobble and a tag disagree about typography far
    more often than about the words.

    `&` and `+` become "and" before punctuation is stripped, rather than
    vanishing. Otherwise "Simon & Garfunkel" normalises to "simon garfunkel"
    and "Simon and Garfunkel" to "simon and garfunkel", and two spellings of
    one band never match. This is one of the commonest differences between
    what a scrobbler sent and what the tag says.
    """
    text = _FEAT.sub(" ", text or "")
    text = _AMPERSAND.sub(" and ", text.lower())
    text = _PUNCT.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


class LastfmError(RuntimeError):
    """Last.fm could not be asked, or answered with an error."""


# --- talking to Last.fm -----------------------------------------------------

def _call(method: str, secret: str | None = None, **params: str) -> dict:
    """One request, retried while the failure looks temporary.

    Last.fm returns a 500 now and then. Without this, a single one part way
    through a long history threw away every page already fetched - which is
    the same mistake as treating a rate-limited search as "no results", and
    more expensive here because a full history is a quarter of an hour of
    requests.

    A 5xx, a timeout or a dropped connection is worth another go. An error
    *in the response body* is Last.fm telling us the request was wrong, and
    repeating it will not help.
    """
    query = {"method": method, "api_key": params.pop("api_key"), **params}
    if secret:
        # Signed calls hash every parameter, sorted by name, with the shared
        # secret on the end. Only needed to ask who a session key belongs to.
        joined = "".join(f"{k}{query[k]}" for k in sorted(query))
        query["api_sig"] = hashlib.md5(
            (joined + secret).encode("utf-8")).hexdigest()
    query["format"] = "json"

    url = API + "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url, headers={"User-Agent": "navidrome-companion/1.0"})

    last = ""
    for attempt, delay in enumerate(RETRY_BACKOFF, start=1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                raise LastfmError(f"HTTP {exc.code}") from exc
            last = f"HTTP {exc.code}"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"[:120]
        else:
            if "error" in body:
                raise LastfmError(f"{body.get('message', 'unknown error')}")
            return body

        if delay:
            log.warning("last.fm %s (attempt %d), retrying in %ds",
                        last, attempt, delay)
            time.sleep(delay)

    raise LastfmError(f"{last} after {len(RETRY_BACKOFF)} attempts")


def username_for(session_key: str, api_key: str, secret: str) -> str:
    """Who a Navidrome Last.fm session key belongs to.

    Navidrome stores the session key but not the name, and the scrobble
    history endpoint needs the name. One signed call rather than asking
    somebody to remember it.
    """
    body = _call("user.getInfo", secret=secret, api_key=api_key,
                 sk=session_key)
    return body["user"]["name"]


def scrobbles(username: str, api_key: str,
              progress=None) -> list[tuple[str, str, int]]:
    """Every scrobble, as (artist, track, unix time), oldest page last.

    Paged rather than streamed because the total has to be known before
    anything is reported, and a listening history is tens of thousands of
    rows rather than millions.
    """
    out: list[tuple[str, str, int]] = []
    page = 1
    pages = 1
    while page <= pages:
        body = _call("user.getRecentTracks", api_key=api_key, user=username,
                     limit=str(PAGE), page=str(page))
        recent = body.get("recenttracks", {})
        attr = recent.get("@attr", {})
        pages = int(attr.get("totalPages", 1))
        for item in recent.get("track", []):
            # A track playing right now has no date and has not finished.
            if item.get("@attr", {}).get("nowplaying") == "true":
                continue
            date = item.get("date", {}).get("uts")
            if not date:
                continue
            out.append((item.get("artist", {}).get("#text", ""),
                        item.get("name", ""), int(date)))
        if progress and (page % 25 == 0 or page == pages or page == 1):
            progress(page, pages, len(out))
        page += 1
        time.sleep(PAUSE)
    return out


# --- what the library looks like from here ----------------------------------

def library_index(connection: sqlite3.Connection) -> dict[tuple[str, str], list[str]]:
    """(artist, title) -> the track UUIDs that could be meant.

    A list, not a single value: a single and its album appearance are two
    files of one recording, and choosing between them by row order would be
    arbitrary. Where the list has more than one entry the scrobble is
    ambiguous and is reported rather than assigned.
    """
    rows = connection.execute(f"""
        select json_extract(mf.tags, '{playcounts.UUID_TAG}') as track_uuid,
               mf.artist, mf.title
          from media_file mf
         where json_extract(mf.tags, '{playcounts.UUID_TAG}') is not null
    """).fetchall()

    index: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    for track_uuid, artist, title in rows:
        if not artist or not title:
            continue
        index[(normalise(artist), normalise(title))].add(track_uuid)
    return {key: sorted(value) for key, value in index.items()}


# --- putting the two together -----------------------------------------------

def plan(username: str, user_id: str, played: list[tuple[str, str, int]],
         index: dict[tuple[str, str], list[str]],
         before: str | None) -> dict[str, Any]:
    """Work out what would be written, without writing any of it.

    `username` is the *Navidrome* account name, not the Last.fm one. Rows are
    joined on user_id either way, but a snapshot row saying "alex" beside an
    imported row saying "argyle_nz" for the same person reads like two
    different listeners.

    `before` is the first day snapshots cover. Scrobbles from that day
    onwards are dropped: the nightly snapshots already count them, and
    importing both would double every play on the handover day.
    """
    zone = playcounts.zone()
    per_day: dict[tuple[str, str], int] = collections.Counter()
    matched = ambiguous = unmatched = overlapping = 0
    ambiguous_examples: list[str] = []
    unmatched_examples: collections.Counter = collections.Counter()

    for artist, track, when in played:
        day = datetime.fromtimestamp(when, zone).strftime("%Y-%m-%d")
        if before is not None and day >= before:
            overlapping += 1
            continue
        key = (normalise(artist), normalise(track))
        candidates = index.get(key)
        if not candidates:
            unmatched += 1
            unmatched_examples[f"{artist} - {track}"] += 1
            continue
        if len(candidates) > 1:
            ambiguous += 1
            if len(ambiguous_examples) < 10:
                ambiguous_examples.append(
                    f"{artist} - {track} ({len(candidates)} copies)")
            continue
        matched += 1
        per_day[(day, candidates[0])] += 1

    return {
        "username": username,
        "user_id": user_id,
        "scrobbles": len(played),
        "matched": matched,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "after_snapshots_began": overlapping,
        "days": len({day for day, _ in per_day}),
        "rows": [(day, track_uuid, plays)
                 for (day, track_uuid), plays in sorted(per_day.items())],
        "ambiguous_examples": ambiguous_examples,
        "unmatched_examples": unmatched_examples.most_common(10),
        # The whole set, for auditing. "Not in the library" is a claim worth
        # being able to check by hand, and a bare list of names does not
        # support that - what tells you whether a miss is real is whether
        # something close to it is sitting there unmatched.
        "unmatched_all": unmatched_examples,
    }


def audit_unmatched(planned: dict[str, Any],
                    index: dict[tuple[str, str], list[str]],
                    limit: int = 4000) -> list[dict[str, Any]]:
    """For every scrobble that matched nothing, the closest thing in the
    library and how close it is.

    This is what makes "not in the library" checkable. A name on its own
    cannot be judged; a name beside the nearest thing here can. A high score
    means the matcher missed something it should have caught - a different
    apostrophe, a remaster suffix, a translated title - and a low one means
    the track really is absent.
    """
    from rapidfuzz import fuzz, process

    keys = list(index)
    haystack = [f"{artist} {title}" for artist, title in keys]

    out = []
    for label, count in planned["unmatched_all"].most_common(limit):
        artist, _, title = label.partition(" - ")
        needle = f"{normalise(artist)} {normalise(title)}"
        best = process.extractOne(needle, haystack, scorer=fuzz.token_set_ratio)
        nearest, score = (keys[best[2]], best[1]) if best else (("", ""), 0)
        out.append({
            "scrobbles": count,
            "scrobbled": label,
            "nearest": f"{nearest[0]} - {nearest[1]}" if nearest[0] else "",
            "score": round(score),
        })
    return out


def write(planned: dict[str, Any]) -> int:
    """Store the matched rows. Idempotent: the key is (day, track, user,
    source), so running twice replaces rather than doubles anyone's history."""
    db = store.connection()
    rows = [(when, track_uuid, planned["user_id"], planned["username"],
             plays, SOURCE)
            for when, track_uuid, plays in planned["rows"]]
    with store._lock:
        db.executemany(
            "INSERT OR REPLACE INTO play_imported"
            " (played_at, track_uuid, user_id, username, plays, source)"
            " VALUES (?, ?, ?, ?, ?, ?)", rows)
        db.commit()
    return len(rows)


# --- giving the imported history its clock back -----------------------------

def _stamp(when: int) -> str:
    """A scrobble's unix time, in the listener's own zone.

    The same shape `playcounts.local_stamp` produces for a snapshot's
    play_date, so the two sources stay comparable once both are timestamped.
    """
    return datetime.fromtimestamp(when, playcounts.zone()).isoformat(
        timespec="seconds")


def plan_times(user_id: str, played: list[tuple[str, str, int]],
               index: dict[tuple[str, str], list[str]],
               source: str = SOURCE) -> dict[str, Any]:
    """Work out the time each already-imported play actually happened.

    **Not a re-import.** The rows in `play_imported` record decisions, and
    some of them were made by hand: 3,402 scrobbles matched several *files*
    of one recording and were assigned to the copy duplicates.py would keep,
    which no code in this module reproduces. Re-deriving the match would
    quietly drop them. So this joins fresh scrobbles onto the answer already
    stored rather than working the answer out again - for each row saying
    "this track, this day, N plays", it looks for N scrobbles on that day
    whose artist and title resolve to that track, and takes their times.

    A row whose scrobbles cannot be found, or are found in the wrong number,
    is left exactly as it is. Nothing is inferred and nothing is dropped, so
    the total is invariant by construction: a row is only ever replaced by
    the same number of plays at a finer resolution. `write_times` checks
    that before it commits anyway.

    Rows that already carry a timestamp are skipped, which makes this safe
    to run twice.
    """
    # Every scrobble, by the local day it happened on.
    by_day: dict[str, list[tuple[int, tuple[str, str]]]] = (
        collections.defaultdict(list))
    for artist, track, when in played:
        day = _stamp(when)[:10]
        by_day[day].append((when, (normalise(artist), normalise(track))))
    for scrobbles_that_day in by_day.values():
        scrobbles_that_day.sort()

    rows = store.connection().execute(
        "SELECT played_at, track_uuid, plays FROM play_imported"
        "  WHERE user_id = ? AND source = ? AND length(played_at) = 10"
        "  ORDER BY played_at, track_uuid", (user_id, source)).fetchall()

    wanted: dict[str, list[tuple[str, int]]] = collections.defaultdict(list)
    for day, track_uuid, plays in rows:
        wanted[day].append((track_uuid, plays))

    resolved: list[tuple[str, str, int, list[str]]] = []
    unresolved: list[tuple[str, str, int, str]] = []

    for day in sorted(wanted):
        available = by_day.get(day, [])
        # Which scrobbles of that day have been handed to a track already.
        # A normalised artist and title can name several tracks - that is
        # what "ambiguous" meant at import - so without this, two tracks
        # sharing a name would both claim the same scrobble.
        taken: set[int] = set()
        for track_uuid, plays in wanted[day]:
            mine = [position for position, (_when, key)
                    in enumerate(available)
                    if position not in taken
                    and track_uuid in index.get(key, ())]
            if len(mine) < plays:
                unresolved.append((
                    day, track_uuid, plays,
                    f"{len(mine)} scrobble(s) on that day resolve to it, "
                    f"{plays} wanted"))
                continue
            # The earliest, where a track has more scrobbles that day than
            # the row accounts for. Arbitrary, but the alternative is to
            # leave a resolvable row unresolved, and the row's own day is
            # right either way.
            chosen = mine[:plays]
            taken.update(chosen)
            resolved.append((day, track_uuid, plays,
                             [_stamp(available[at][0]) for at in chosen]))

    days_with_no_scrobbles = sum(
        1 for day, _t, _p, why in unresolved if why.startswith("0 "))
    return {
        "user_id": user_id,
        "source": source,
        "day_rows": len(rows),
        "resolved_rows": len(resolved),
        "resolved_plays": sum(plays for _d, _t, plays, _w in resolved),
        "unresolved_rows": len(unresolved),
        "unresolved_plays": sum(plays for _d, _t, plays, _w in unresolved),
        "unresolved_with_nothing_that_day": days_with_no_scrobbles,
        "resolved": resolved,
        "unresolved": unresolved,
    }


def write_times(planned: dict[str, Any], username: str) -> dict[str, Any]:
    """Replace each resolved day row with its plays at the times they happened.

    One transaction, and it refuses to commit if the number of plays would
    change. That is the whole safety property of this operation: it adds
    precision to history and must not be able to add or lose any of it.
    """
    db = store.connection()
    user_id, source = planned["user_id"], planned["source"]

    with store._lock:
        before = db.execute(
            "SELECT COALESCE(SUM(plays), 0) FROM play_imported"
            "  WHERE user_id = ? AND source = ?", (user_id, source)
        ).fetchone()[0]

        try:
            db.execute("BEGIN")
            for day, track_uuid, _plays, times in planned["resolved"]:
                db.execute(
                    "DELETE FROM play_imported WHERE played_at = ?"
                    "  AND track_uuid = ? AND user_id = ? AND source = ?",
                    (day, track_uuid, user_id, source))
                # Counted, not inserted one by one: two scrobbles of the
                # same track in the same second would otherwise collide on
                # the primary key and one play would vanish.
                for when, count in collections.Counter(times).items():
                    db.execute(
                        "INSERT INTO play_imported"
                        " (played_at, track_uuid, user_id, username, plays,"
                        "  source) VALUES (?, ?, ?, ?, ?, ?)",
                        (when, track_uuid, user_id, username, count, source))

            after = db.execute(
                "SELECT COALESCE(SUM(plays), 0) FROM play_imported"
                "  WHERE user_id = ? AND source = ?", (user_id, source)
            ).fetchone()[0]
            if after != before:
                db.execute("ROLLBACK")
                raise RuntimeError(
                    f"refusing to commit: {before} plays before, {after} "
                    f"after. The backfill may only change *when* a play is "
                    f"recorded, never whether it is.")
            db.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                db.execute("ROLLBACK")
            raise

    return {"plays": before, "rows_rewritten": len(planned["resolved"])}


# --- the command ------------------------------------------------------------

def _credentials(username_wanted: str) -> tuple[str, str, str]:
    """API key, shared secret and the session key Navidrome holds.

    Read from the same places the running system already keeps them, rather
    than asking for credentials that exist twice over.
    """
    api_key = settings.lastfm_api_key
    secret = settings.lastfm_secret
    if not api_key or not secret:
        raise SystemExit(
            "lastfm_api_key and lastfm_secret are not set. They are the same "
            "pair Navidrome uses (ND_LASTFM_APIKEY / ND_LASTFM_SECRET).")

    connection = navidrome.open_db()
    with connection:
        row = connection.execute("""
            select p.value from user_props p
              join user u on u.id = p.user_id
             where p.key = 'LastFMSessionKey' and u.user_name = ?
        """, (username_wanted,)).fetchone()
    if not row:
        raise SystemExit(
            f"{username_wanted!r} has no Last.fm session in Navidrome. "
            "Connect the account in Navidrome first.")
    return api_key, secret, row[0]


def _report_times(args: argparse.Namespace, user_id: str,
                  played: list[tuple[str, str, int]],
                  index: dict[tuple[str, str], list[str]]) -> int:
    """The --times half: precision for history that is already imported."""
    planned = plan_times(user_id, played, index)

    print()
    print(f"scrobbles fetched        : {len(played)}")
    print(f"day-granular rows        : {planned['day_rows']}")
    print(f"  resolvable to a time   : {planned['resolved_rows']} rows, "
          f"{planned['resolved_plays']} plays")
    print(f"  left as they are       : {planned['unresolved_rows']} rows, "
          f"{planned['unresolved_plays']} plays")
    print(f"    of which no scrobble : "
          f"{planned['unresolved_with_nothing_that_day']}")

    if planned["unresolved"]:
        print()
        print("left alone, a sample:")
        for day, track_uuid, plays, why in planned["unresolved"][:10]:
            print(f"  {day}  {track_uuid[:8]}  {plays} play(s): {why}")

    if not args.apply:
        print()
        print("DRY RUN. Nothing written. Re-run with --apply.")
        return 0

    result = write_times(planned, args.user)
    print()
    print(f"rewrote {result['rows_rewritten']} day rows as timestamped "
          f"plays. {result['plays']} plays before and after.")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("user", help="the Navidrome account to import for")
    parser.add_argument("--apply", action="store_true",
                        help="write the matched rows; otherwise report only")
    parser.add_argument("--lastfm-user", default=None,
                        help="override the Last.fm name (normally derived)")
    parser.add_argument("--scrobbles", metavar="FILE", default=None,
                        help="read scrobbles from a JSON file of "
                             "[artist, track, unix time] instead of "
                             "fetching them, so that what was checked in a "
                             "dry run is exactly what gets applied")
    parser.add_argument("--times", action="store_true",
                        help="do not import; instead give the plays already "
                             "imported the times they actually happened, by "
                             "joining fresh scrobbles onto the existing rows")
    parser.add_argument("--audit", metavar="FILE", default=None,
                        help="write every unmatched scrobble beside the "
                             "closest thing in the library, for checking by "
                             "hand whether a miss is real")
    args = parser.parse_args()

    store.connect(settings.state_db)
    # Credentials are only needed to fetch. Reading a file needs none, which
    # also means the apply step does not have to be given the key again.
    api_key = secret = session_key = ""
    if not args.scrobbles:
        api_key, secret, session_key = _credentials(args.user)

    connection = navidrome.open_db()
    with connection:
        row = connection.execute(
            "select id from user where user_name = ?", (args.user,)).fetchone()
        if not row:
            raise SystemExit(f"no Navidrome account called {args.user!r}")
        user_id = row[0]
        index = library_index(connection)

    if args.scrobbles:
        name = args.lastfm_user or f"(from {args.scrobbles})"
    else:
        name = args.lastfm_user or username_for(session_key, api_key, secret)
    print(f"Navidrome account: {args.user}")
    print(f"Last.fm account: {name}")
    print(f"library:         {len(index)} distinct artist/title pairs")

    def progress(page, pages, so_far):
        print(f"  fetching page {page}/{pages} ({so_far} scrobbles)",
              end="\r", flush=True)

    if args.scrobbles:
        with open(args.scrobbles, encoding="utf-8") as handle:
            played = [(artist, track, int(when))
                      for artist, track, when in json.load(handle)]
        print(f"read {len(played)} scrobbles from {args.scrobbles}")
    else:
        played = scrobbles(name, api_key, progress)
    print(" " * 60, end="\r")

    if args.times:
        return _report_times(args, user_id, played, index)

    # Snapshots cover from their first day onwards; importing over the top of
    # them would double every play on the handover day.
    first_snapshot = store.connection().execute(
        "select min(taken_on) from play_snapshot").fetchone()[0]
    # Stored under the Navidrome name so both sources label one person the
    # same way; the Last.fm name is reported above and nowhere else.
    planned = plan(args.user, user_id, played, index, first_snapshot)

    print()
    print(f"scrobbles fetched          : {planned['scrobbles']}")
    print(f"  matched to one track     : {planned['matched']}")
    print(f"  ambiguous (>1 candidate) : {planned['ambiguous']}")
    print(f"  no track in the library  : {planned['unmatched']}")
    print(f"  on or after {first_snapshot or 'n/a'} : "
          f"{planned['after_snapshots_began']}  (snapshots already have these)")
    print(f"days covered               : {planned['days']}")
    print(f"rows to write              : {len(planned['rows'])}")

    if planned["ambiguous_examples"]:
        print("\nambiguous, a sample:")
        for line in planned["ambiguous_examples"]:
            print(f"  {line}")
    if planned["unmatched_examples"]:
        print("\nmost-scrobbled tracks with nothing in the library:")
        for line, count in planned["unmatched_examples"]:
            print(f"  {count:>5}x  {line}")

    if args.audit:
        rows = audit_unmatched(planned, index)
        with open(args.audit, "w", encoding="utf-8") as handle:
            handle.write(f"{'plays':>6}  {'score':>5}  scrobbled"
                         f"{' ' * 52}nearest in library (normalised)\n")
            for row in rows:
                handle.write(f"{row['scrobbles']:>6}  {row['score']:>5}  "
                             f"{row['scrobbled'][:58]:60}{row['nearest'][:58]}\n")
        close = [r for r in rows if r["score"] >= 85]
        print(f"\nwrote {len(rows)} unmatched titles to {args.audit}")
        print(f"  {len(close)} of them look close to something here "
              f"(score 85+) - those are the ones worth reading")

    if not args.apply:
        print("\nDRY RUN. Nothing written. Re-run with --apply.")
        return 0

    written = write(planned)
    print(f"\nwrote {written} day/track rows for {args.user}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
