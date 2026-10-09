"""Credit a song's old listening to its current identity.

A track re-downloaded or refiled can come back with a new track UUID. The
song is the same, but the history - kept per UUID - stays with the old one,
which Navidrome now lists as a file it can no longer find: Home's top tracks
count the song from the day it changed, and the old plays read as music that
left the library. On the Pi, 219 plays across 35 songs were in that state
(2026-10-09).

This finds each missing Navidrome row whose UUID is no longer live and whose
song *is* - same library, same artist and title, the same length to within
`LENGTH_SLACK` seconds where both are known - and records an alias from the
old UUID to the live one (`play_alias`, read by `playcounts.increments`). An
old UUID matching more than one live track is left alone: guessing which
copy gets the plays is how a history quietly stops being true.

Nothing in the history is rewritten, and Navidrome is only read.

    python -m app.relink            # what would be credited where
    python -m app.relink --apply
    python -m app.relink --undo     # remove every alias this wrote
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import cli, navidrome, playcounts, registry, store

log = logging.getLogger("navidrome_companion.relink")

REASON = "relink"
LENGTH_SLACK = 10.0


@dataclass
class Pair:
    old_uuid: str
    new_uuid: str
    song: str            # "Artist - Title", for the report
    plays: dict[str, int] = field(default_factory=dict)   # username -> plays


@dataclass
class Plan:
    pairs: list[Pair] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)

    @property
    def plays(self) -> int:
        return sum(sum(p.plays.values()) for p in self.pairs)


def _rows() -> tuple[list[tuple], set[str]]:
    """Every media_file row with its UUID, and the ids Navidrome can find."""
    connection = navidrome.open_db()
    with connection:
        live_ids: set[str] = set()
        for (library_id,) in connection.execute("select id from library"):
            clause = navidrome.live_clause(connection, [library_id])
            live_ids |= {row[0] for row in connection.execute(
                f"select mf.id from media_file mf where {clause}")}
        rows = connection.execute(f"""
            select mf.id, mf.library_id, coalesce(mf.artist, ''),
                   coalesce(mf.title, ''), mf.duration,
                   json_extract(mf.tags, '{navidrome.UUID_TAG}')
              from media_file mf""").fetchall()
    return rows, live_ids


def build() -> Plan:
    rows, live_ids = _rows()
    live_uuids = {row[5] for row in rows if row[0] in live_ids and row[5]}
    live_by_song: dict[tuple, set[tuple[str, float | None]]] = collections.defaultdict(set)
    names: dict[str, str] = {}
    for _id, library_id, artist, title, duration, uuid in rows:
        if _id in live_ids and uuid:
            live_by_song[(library_id, registry.recording_key(artist, title))].add(
                (uuid, duration))
            names[uuid] = f"{artist} - {title}"

    known = playcounts.aliases()
    plan = Plan()
    seen: set[str] = set()
    for _id, library_id, artist, title, duration, uuid in rows:
        if (_id in live_ids or not uuid or uuid in live_uuids
                or uuid in known or uuid in seen):
            continue
        seen.add(uuid)
        candidates = {
            new for new, length in live_by_song.get(
                (library_id, registry.recording_key(artist, title)), ())
            if not (duration and length) or abs(duration - length) <= LENGTH_SLACK}
        if len(candidates) > 1:
            plan.ambiguous.append(f"{artist} - {title}: {len(candidates)} live copies")
        elif candidates:
            new = candidates.pop()
            plan.pairs.append(Pair(old_uuid=uuid, new_uuid=new, song=names[new]))
    _count_plays(plan)
    plan.pairs.sort(key=lambda p: -sum(p.plays.values()))
    return plan


def _count_plays(plan: Plan) -> None:
    """How many recorded plays each pair would move, per person."""
    if not plan.pairs:
        return
    by_old = {pair.old_uuid: pair for pair in plan.pairs}
    db = store.connection()
    users = dict(db.execute(
        "select user_id, max(username) from (select user_id, username from play_snapshot"
        " union all select user_id, username from play_imported) group by user_id"
    ).fetchall())
    for user_id, username in users.items():
        for _when, track_uuid, n in playcounts._read_increments_raw(user_id):
            pair = by_old.get(track_uuid)
            if pair:
                pair.plays[username or user_id] = pair.plays.get(username or user_id, 0) + n


def apply_plan(plan: Plan) -> int:
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with store.transaction() as tx:
        tx.executemany(
            "INSERT OR IGNORE INTO play_alias (old_uuid, new_uuid, reason, created_at)"
            " VALUES (?, ?, ?, ?)",
            [(p.old_uuid, p.new_uuid, REASON, stamp) for p in plan.pairs])
    return len(plan.pairs)


def undo() -> int:
    with store.transaction() as tx:
        return tx.execute("DELETE FROM play_alias WHERE reason = ?",
                          (REASON,)).rowcount


def report(plan: Plan, limit: int = 40) -> str:
    with_plays = [p for p in plan.pairs if p.plays]
    out = [f"{len(plan.pairs)} old track UUID(s) match a live song; "
           f"{len(with_plays)} carry plays, {plan.plays} in all"]
    for pair in with_plays[:limit]:
        who = ", ".join(f"{name} {n}" for name, n in sorted(pair.plays.items()))
        out.append(f"  {sum(pair.plays.values()):4} plays  {pair.song}  ({who})")
    if len(with_plays) > limit:
        out.append(f"  ... and {len(with_plays) - limit} more")
    if plan.ambiguous:
        out.append(f"{len(plan.ambiguous)} left alone, matching more than one live copy:")
        out.extend(f"  {line}" for line in plan.ambiguous[:20])
    return "\n".join(out)


def main() -> int:
    cli.not_as_root("relink")
    import argparse

    from .config import settings

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="record the aliases (default: say what would move)")
    parser.add_argument("--undo", action="store_true",
                        help="remove every alias this command recorded")
    args = parser.parse_args()

    store.connect(settings.state_db)
    if args.undo:
        print(f"removed {undo()} alias(es)")
        return 0
    plan = build()
    print(report(plan))
    if not args.apply:
        print("DRY RUN. Nothing recorded. Re-run with --apply.")
        return 0
    print(f"recorded {apply_plan(plan)} alias(es)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
